"""Kanban tools — structured tool-call surface for worker + orchestrator agents.

These tools are registered into the model's schema when the agent is
running under the dispatcher (env var ``HERMES_KANBAN_TASK`` set) or when
the active profile explicitly enables the ``kanban`` toolset for
orchestrator work. A normal ``hermes chat`` session still sees **zero**
kanban tools in its schema unless configured.

Why tools instead of just shelling out to ``hermes kanban``?

1. **Backend portability.** A worker whose terminal tool points at Docker
   / Modal / Singularity / SSH would run ``hermes kanban complete …``
   inside the container, where ``hermes`` isn't installed and the DB
   isn't mounted. Tools run in the agent's Python process, so they
   always reach ``~/.hermes/kanban.db`` regardless of terminal backend.

2. **No shell-quoting footguns.** Passing ``--metadata '{"x": [...]}'``
   through shlex+argparse is fragile. Structured tool args skip it.

3. **Better errors.** Tool-call failures return structured JSON the
   model can reason about, not stderr strings it has to parse.

Humans continue to use the CLI (``hermes kanban …``), the dashboard
(``hermes dashboard``), and the slash command (``/kanban …``) — all
three bypass the agent entirely. The tools are for dispatcher-spawned
worker handoffs and for configured orchestrator profiles that route work
through the board.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

from agent.redact import redact_sensitive_text
from hermes_cli.goals import judge_goal
from tools.registry import registry, tool_error
from hermes_cli.config import cfg_get, load_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

KANBAN_LIST_DEFAULT_LIMIT = 50
KANBAN_LIST_MAX_LIMIT = 200


def _profile_has_kanban_toolset() -> bool:
    # Uses load_config() which has mtime-based caching, so this adds
    # negligible overhead. The check_fn results are further TTL-cached
    # (~30s) by the tool registry.
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        toolsets = cfg.get("toolsets", [])
        return "kanban" in toolsets
    except Exception:
        return False


def _check_kanban_mode() -> bool:
    """Task-lifecycle tools are available when:

    1. ``HERMES_KANBAN_TASK`` is set (dispatcher-spawned worker), OR
    2. The current profile has ``kanban`` in its toolsets config
       (orchestrator profiles like techlead that route work via Kanban).

    Humans running ``hermes chat`` without the kanban toolset see zero
    kanban tools. Workers spawned by the kanban dispatcher (gateway-
    embedded by default) and orchestrator profiles with the kanban
    toolset enabled see the Kanban lifecycle tool surface.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return True
    return _profile_has_kanban_toolset()


def _check_kanban_orchestrator_mode() -> bool:
    """Board-mutating routing tools (kanban_unblock) are intentionally
    hidden from task workers.

    Dispatcher-spawned workers should close their own task via the
    lifecycle tools (complete/block/heartbeat), not unblock board
    state. Profiles that explicitly opt into the kanban toolset
    and are NOT scoped to a single task are the orchestrator surface.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return False
    return _profile_has_kanban_toolset()


def _check_kanban_board_read_mode() -> bool:
    """Read-only board discovery (kanban_list).

    Available to any profile that explicitly opts into the kanban
    toolset — including dispatcher-spawned orchestrator workers
    (``HERMES_KANBAN_TASK`` set). Orchestrators are themselves
    dispatched as tasks but still need to enumerate running cards
    (e.g. the pre-coder file-conflict check); hiding kanban_list from
    them forced capability self-blocks. Focused workers (coder,
    reviewer profiles) don't opt into the kanban toolset, so they
    still never see this tool.
    """
    return _profile_has_kanban_toolset()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _default_task_id(arg: Optional[str]) -> Optional[str]:
    """Resolve ``task_id`` arg or fall back to the env var the dispatcher set."""
    if arg:
        return arg
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    return env_tid or None


def _worker_run_id(task_id: str) -> Optional[int]:
    """Return this worker's dispatcher run id when it is scoped to task_id."""
    if os.environ.get("HERMES_KANBAN_TASK") != task_id:
        return None
    raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _stamp_worker_session_metadata(
    task_id: str, metadata: Optional[dict]
) -> Optional[dict]:
    """Add trusted worker session id metadata for this worker's own task."""
    if os.environ.get("HERMES_KANBAN_TASK") != task_id:
        return metadata
    session_id = os.environ.get("HERMES_SESSION_ID")
    if not session_id:
        return metadata
    stamped = dict(metadata or {})
    stamped["worker_session_id"] = session_id
    return stamped


def _enforce_worker_task_ownership(tid: str) -> Optional[str]:
    """Reject worker-driven destructive calls on foreign task IDs.

    A process spawned by the dispatcher has ``HERMES_KANBAN_TASK`` set
    to its own task id. Tools like ``kanban_complete`` / ``kanban_block``
    / ``kanban_heartbeat`` mutate run-lifecycle state, so a buggy or
    prompt-injected worker that passed an explicit ``task_id`` for some
    other task could corrupt sibling or cross-tenant runs (see #19534).

    Orchestrator profiles (kanban toolset enabled but **no**
    ``HERMES_KANBAN_TASK`` in env) aren't subject to this check — their
    job is routing, and they sometimes legitimately close out child
    tasks or reopen blocked ones. Workers are narrowly scoped to their
    one task.

    Returns ``None`` when the call is allowed, or a tool-error string
    when it must be rejected. Callers should ``return`` the error
    verbatim.
    """
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    if not env_tid:
        # Orchestrator or CLI context — no task-scope restriction.
        return None
    if tid != env_tid:
        return tool_error(
            f"worker is scoped to task {env_tid}; refusing to mutate "
            f"{tid}. Use kanban_comment to hand off information to other "
            f"tasks, or kanban_create to spawn follow-up work."
        )
    return None


def _connect(board: Optional[str] = None):
    """Import + connect lazily so the module imports cleanly in non-kanban
    contexts (e.g. test rigs that import every tool module).

    When ``board`` is provided it's forwarded to :func:`kb.connect`, which
    routes the connection to that board's sqlite file. ``None`` (the
    default) preserves the legacy resolution chain
    (``HERMES_KANBAN_DB`` → ``HERMES_KANBAN_BOARD`` env → current symlink
    → ``default``). Per-tool ``board`` lets a Telegram-side agent override
    the env-pinned active board without restarting Hermes.
    """
    from hermes_cli import kanban_db as kb
    return kb, kb.connect(board=board)


# 'review' is deliberately allowed: the review handoff is not an escape
# hatch from the goal loop — it routes to the reviewer whose approval
# goes through the done-verification gate. Rejecting it forced coders
# into direct kanban_complete, bypassing review entirely (live t_05914cf9).
_GOAL_MODE_BLOCK_ALLOWED_KINDS = frozenset({"dependency", "needs_input", "review"})

# Block kinds that escalate a card into the human ``blocked`` bucket (a person
# must read it): ``needs_input`` / ``capability`` and un-typed (``None``, a
# "generic human blocker"). ``dependency`` routes to ``todo`` and
# ``transient``/``review`` route elsewhere — none of those wait on a human — so
# they are excluded. The orchestrator block gate (F22) only fires for this set.
_HUMAN_BLOCK_KINDS = frozenset({None, "needs_input", "capability"})

# Mirror of the Beobachter's TECH_REGEX (checks/beobachter.py): a technical term
# in a would-be Zielfrage forces the observer's ``zielfrage`` gate to False and
# the LLM richter CANNOT override it, so such a "question" can never be green.
# Kept byte-for-byte in sync with the observer so the specifier block gate (F24)
# rejects exactly what the observer would reject, never more. Not a facade — it
# enforces the observer's own necessary condition at the worker.
_SPECIFIER_TECH_REGEX = re.compile(
    r"\b(rebase|port|library|bibliothek|dependency|abh[äa]ngigkeit|pfad|path|"
    r"version|framework)\b",
    re.IGNORECASE,
)

# Mirror of the Beobachter's richter TOR1/TOR3 for the repo coder's block gate
# (F28). A human-escalation block whose question is about INTERNAL TASK STATE —
# task IDs (``t_<hex>``), worktree branches (``wt/...``), or a duplicate/merge
# triage — is not a business Zielfrage: Till never sees task IDs or wt/ branch
# names and cannot answer on a business basis, so the observer's richter forces
# TOR1=NO (reine Arbeitskoordination, keine Geschaefts-Dimension) and TOR3=NO
# ("verlangt internes Code-/Datenmodell-Verstaendnis: Task-IDs, Branches
# wt/t_*, Merge-Status") → rot-technische-rueckfrage. Such markers never appear
# in a genuine business/Freigabe question, so matching them is a safe necessary-
# condition mirror that leaves the coder's real-wall block path untouched.
_CODER_INTERNAL_TRIAGE_REGEX = re.compile(
    r"\bt_[0-9a-f]{6,}\b|\bwt/|\bduplikat|\bduplicate",
    re.IGNORECASE,
)

# F37: the coder found the specifier's ACCEPTANCE CRITERION itself defective and
# tried to escalate "please correct it" to a human. Live probe card t_82eac595
# ('Tests fuer die Mandanten-Farben', erwartung=done): voicera-coder read the
# public API as ``tenantChartColor(name, mode: "light" | "dark")`` but the
# specifier's ABNAHMEKRITERIUM demanded ``tenantChartColor("Acme GmbH", false)``
# /``true`` — Boolean args that are both ``!= "dark"`` at runtime and invalid at
# typecheck. The coder correctly refused to build against a wrong criterion and
# called ``kanban_block(kind='needs_input', reason="HALT: Specifier-Abnahme-
# kriterium nutzt Boolean-Modi … bitte Kriterium technisch korrigieren und Karte
# erneut an den Coder geben.")``. No comment carries a "?", so the Beobachter
# extracts no frage and classes the question-less blocked done-card as
# rot-infra-eskalation-ohne-frage (F37 2a — the same question-less-blocked symptom
# F28/F25 produce, here from a spec-correction wall). But the ABNAHMEKRITERIUM is
# the SPECIFIER's to write and fix (it owns the criterion, F01); a technically
# wrong one is a pure factory-internal coder->specifier->coder correction loop,
# NEVER Till's decision — Till never sees the criterion and cannot answer on a
# business basis (the Manager's own diagnosis of this card says exactly this: "(A)
# Abnahmekriterium … praezisieren und … erneut an den Coder"). Mirror that
# necessary condition in code: a coder human-block that NAMES the acceptance
# criterion / specifier spec AND frames it as defective (correct / adjust /
# wrong / contradicts / invalid, or the explicit "erneut an den Coder" loop) is
# routed BACK to the specifier via kanban_handoff, not parked at a human. The two
# signals are AND-ed (like F32's app-path + import-hygiene pair) so a genuine
# business block that merely mentions "Kriterium" in passing keeps its human-wall
# path. Scoped to repo coders + human-escalation kinds, worker-side (re-imported
# per spawn), live on the next coder spawn with no gateway restart (cf. F19).
_CODER_SPEC_REF_RE = re.compile(
    r"(?:abnahme|akzeptanz)[-\s]?kriteri|\bspecifier\b|\bkriterium\b",
    re.IGNORECASE,
)
_CODER_SPEC_DEFECT_RE = re.compile(
    r"\bkorrigier|\banpass|\bpr[äa]zisier|\bfalsch\b|\bung[üu]ltig"
    r"|\bwiderspr|\bfehlerhaft|\bstimmt\s+nicht\b|\bnicht\s+korrekt\b"
    r"|erneut\s+an\s+den\s+coder"
    r"|zur[üu]ck\s+an\s+(?:den\s+)?(?:specifier|spezifizierer)",
    re.IGNORECASE,
)

# Read-only helper roles the specifier SPAWNS as delegation subagents for facts
# (recon = code-state, brain-reader = Brain knowledge). They own no write access
# ("kein Write, kein Edit, keine Commits" — recon SOUL) and are NEVER a valid
# forward kanban-handoff target for a build card: handing a card that expects a
# write there strands it, because the role cannot build. The specifier's forward
# handoff always goes to ``<repo>-coder`` (specifier SOUL). The handoff gate (F25)
# only fires for this set.
_READ_ONLY_HANDOFF_ROLES = frozenset({"recon", "brain-reader"})

# A reviewer is a read-only role (no node toolchain, no build env — see the
# reviewer SOULs: "Read-only gegen Code. … Kein Live-DB-Zugang"). For a purely
# STATIC backend change its verdict rests on checks that run anywhere (``ruff``,
# ``py_compile``, ``vulture`` over ``*.py``), so completing to ``done`` is
# honest — the Beobachter re-runs the same static check in its venv and greens
# Regel a. Two acceptance shapes break that, and both terminate at the MERGER:
#
#   1) FRONTEND / build-gated: the final acceptance is a node build
#      (``npx tsc``/``vitest``/``npm``/``next build``) that the reviewer CANNOT
#      run read-only AND that the Beobachter's throwaway check-worktree cannot
#      run either (no ``node_modules`` there).
#   2) BACKEND / runtime-env-gated (F31): the acceptance IMPORTS or BOOTS the
#      application package at runtime (``python -c 'import app.models'``,
#      ``import app.main``, ``uvicorn``, ``alembic upgrade``). Importing ``app``
#      instantiates ``Settings()``, which demands runtime secrets
#      (``ANTHROPIC_API_KEY``, DB URL …). The Beobachter runs this in a fresh
#      ``git worktree`` under ``/tmp`` that carries only committed files — no
#      gitignored ``.env`` — so ``Settings()`` raises ``ValidationError`` and
#      the check goes ROT on a pure env gap, not a code defect (live probe
#      Karte 7 'Undefinierte Namen …', t_f2d07fdb: ``ruff F821`` green but
#      ``cd backend && python -c 'import app.models'`` → ``ANTHROPIC_API_KEY
#      muss gesetzt sein`` → ROT a). A read-only reviewer cannot supply the
#      secret either.
#
# For both, a ``done`` is literally "done ohne vollständige Abnahme" (the exact
# F16/F27/F29/F30/F31 befund): branch=True, reviewer=ok, but shell=ROT on the
# unverifiable build/import → Regel a stays red every re-check. The honest
# terminal is the MERGER — it runs in the real checkout
# (``/home/till/work/<repo>`` WITH ``node_modules`` AND the real ``.env``),
# performs the build/typecheck OR the runtime import as its own pre-merge gate,
# and merges after Till's GO. So the reviewer hands such a card to the merger
# (``ready @ merger``) instead of self-completing it. See the build-gated block
# in _handle_complete below.
_BUILD_GATED_RE = re.compile(
    r"(?:\bnpx\b|\bnpm\b|\bpnpm\b|\byarn\b|\bvitest\b|\btsc\b"
    r"|next\s+build|\bvite\b|(?:^|[\s/`'\"(])frontend/|\.tsx?\b|\.vue\b)",
    re.IGNORECASE,
)

# Backend runtime-env gate (F31). Matches an acceptance that must IMPORT or BOOT
# the live ``app`` package (needs secrets/DB the env-less check-worktree lacks),
# plus the reviewer's own admission that a part is only verifiable against the
# running backend. Note ``import\s+app\.`` only fires on a runtime-import
# command/note ("import app.models") — it deliberately does NOT match a source
# import like ``from app.models.tenant import Tenant`` (there "import" is
# followed by the symbol, not "app."), so quoting a TYPE_CHECKING guard in the
# spec does not by itself trip the gate. ``pytest`` is excluded on purpose: its
# cards historically green as ``done`` in the Beobachter venv and must keep that
# path.
_RUNTIME_ENV_GATED_RE = re.compile(
    r"(?:python[0-9.]*\s+-c\b[^\n]*\bimport\s+app\b"
    r"|\bimport\s+app\.(?:models|main|api|services|tasks|core|db|database"
    r"|routers?|workers?|cli|config|schemas)\b"
    r"|\buvicorn\b|\bgunicorn\b|\balembic\s+\w"
    r"|python[0-9.]*\s+-m\s+app\b"
    r"|live[-\s]?import|laufzeit[-\s]?import|runtime[-\s]?import"
    r"|import[^\n]*laufenden?\s+backend|laufenden?\s+backend[^\n]*import)",
    re.IGNORECASE,
)

# F32: an import-hygiene edit to the importable backend ``app`` package. Changing
# an import line (remove unused / F401, fix undefined names / F821, repair a
# missing import) can change whether the package imports AT ALL — a removed
# import may carry a side effect or a re-export — so such a card's acceptance
# almost always smoke-imports the app (``cd backend && python -c 'import
# app.main'``). The Beobachter re-runs that in a throwaway ``/tmp`` worktree
# where the backend package is not installed (``ModuleNotFoundError: No module
# named 'app.main'``), a pure env gap exactly like F31 — so a reviewer ``done``
# is "done ohne vollständige Abnahme" (shell=rot) and must terminate at the
# MERGER instead. Unlike the F31 markers, this one must NOT depend on the
# specifier quoting the import command: the ``python -c 'import app…'`` smoke
# check lives only in the card's ``fertig_heisst.shell`` (Beobachter-side) and
# never reaches the factory. The Beobachter DOES write the backend ``app/…``
# module paths and the ``ungenutzte Importe``/``F401`` nature straight into the
# card BODY, so keying the two signals off the body makes the gate fire even
# when the specifier's criterion only names ``ruff`` (live probe Karte 8
# 't_e5e71e1a', erwartung=done: branch=True, shell=rot(`import app.main`:
# ModuleNotFound in the check-worktree), reviewer=voicera-reviewer,
# ergebnis_passt=True → ROT a). Both signals are required so a generic backend
# card that merely touches ``app/…`` (adds an endpoint, edits logic) with a
# static ruff/pytest acceptance keeps the honest Regel-a ``done`` path.
_APP_PACKAGE_PATH_RE = re.compile(
    r"(?:^|[\s/`'\"(])(?:backend/)?app/"
    r"(?:models|main|api|services|tasks|core|db|database"
    r"|routers?|workers?|cli|config|schemas)\b",
    re.IGNORECASE,
)
_IMPORT_HYGIENE_RE = re.compile(
    r"(?:\bF401\b|\bF811\b|\bF821\b"
    r"|ungenutzte?\s+import|unbenutzte?\s+import|unused\s+import"
    r"|undefinierte?\s+name|undefined\s+name"
    r"|fehlende?\s+import|missing\s+import)",
    re.IGNORECASE,
)

# F21 (this befund): a backend *pytest* card whose target lives inside the
# importable ``app`` package and is explicitly a DB-less unit test. F31's
# runtime-env gate deliberately EXCLUDES pytest, because a static backend pytest
# card greens honestly in the Beobachter venv and must keep its Regel-a ``done``
# path. But a pytest that targets a function in the DB-bound app package
# (``backend/app/services/…``, ``…/models/…``) imports that module AT COLLECTION,
# and ``app/database.py`` creates the async engine at import time
# (``create_async_engine(settings.DATABASE_URL)``). The Beobachter re-runs the
# acceptance in a throwaway worktree against its own venv, which has neither the
# real ``.env`` DATABASE_URL nor the async DB driver — so collection dies before
# a single test runs (live probe Karte 4 't_cdc32d75', erwartung=done: the unit
# test itself uses NO DB fixture and passes 4/4 for coder AND reviewer in the
# task venv, Richter confirms the criterion met, yet the Beobachter's
# ``cd backend && pytest tests/test_todo_priority.py`` is rc=2 ERROR —
# ``app/database.py:49 create_async_engine(...)`` → ``ModuleNotFoundError: No
# module named 'aiosqlite'``). That is a pure provisioning gap exactly like F31,
# not a defect, so a reviewer ``done`` is "done ohne vollständige Abnahme"
# (branch=True, reviewer=ok, ergebnis_passt=True, shell=ROT → Regel a never
# greens) and the honest terminal is the MERGER. Three signals are required so a
# truly-static pytest card (pure helper, no app-package import, no DB framing)
# keeps the honest ``done`` path: a pytest acceptance AND a target inside the app
# package (``_APP_PACKAGE_PATH_RE``) AND the DB-less/DB-bound framing that marks
# the import chain as engine-booting. All three are written into the thread by
# the orchestrator/specifier/coder (the ABNAHMEKRITERIUM names the pytest command
# and the ``backend/app/…`` path; the card frames it as "ohne (laufende)
# Datenbank / ohne DB-Fixture"), so the reviewer sees them at completion.
_PYTEST_ACCEPTANCE_RE = re.compile(r"\bpytest\b", re.IGNORECASE)
_DB_ENGINE_NEXUS_RE = re.compile(
    r"(?:ohne\s+(?:laufende\s+)?(?:datenbank|db)\b"
    r"|ohne\s+db[-\s]?fixture|keine?\s+db[-\s]?fixture"
    r"|no\s+db[-\s]?fixture|without\s+(?:a\s+)?(?:database|db)\b"
    r"|\bpostgres\b|\bpostgresql\b|\brls\b|\baiosqlite\b"
    r"|async[-\s]?engine|create_async_engine|\bsqlalchemy\b"
    r"|\bdatabase_url\b|datenbank[-\s]?fixture)",
    re.IGNORECASE,
)


def _is_reviewer_profile(profile: Optional[str]) -> bool:
    """True when the current worker profile is a code reviewer.

    Reviewers are tenant-scoped (``voicera-reviewer``, ``goya-reviewer`` …);
    a bare ``reviewer`` is accepted for a future untenanted variant. The
    orchestrator/specifier/coder profiles are deliberately excluded — they
    have their own completion guards above.
    """
    p = (profile or "").strip().lower()
    return p == "reviewer" or p.endswith("-reviewer")


def _card_is_build_gated(title: str, body: str, comment_bodies: list[str]) -> bool:
    """True when the card's acceptance needs an environment the reviewer/Beobachter
    cannot provide: a node build (frontend / TypeScript) OR a runtime import/boot
    of the ``app`` package that demands secrets the env-less check-worktree lacks
    (F31). The signal is scanned across the card title, body and the full comment
    thread — the specifier's ``ABNAHMEKRITERIUM`` and the coder's handoff both
    quote the acceptance commands (``npx tsc``, ``vitest`` …, or
    ``python -c 'import app.models'``) and name the gated files, so by the time
    the reviewer completes, the thread carries the signal reliably. A purely
    static backend card (``ruff``/``py_compile``/``vulture`` over ``*.py``)
    matches none of it and stays on the honest ``done`` path."""
    blob = "\n".join([title or "", body or "", *(comment_bodies or [])])
    if _BUILD_GATED_RE.search(blob) or _RUNTIME_ENV_GATED_RE.search(blob):
        return True
    # F32: import-hygiene edit to the importable backend ``app`` package. Its
    # app-import acceptance cannot green in the env-less check-worktree, so route
    # to the merger like F31 — fired off the card body, independent of whether
    # the specifier quoted the import command (which lives only in the
    # Beobachter-side ``fertig_heisst.shell`` and never reaches the factory).
    if _APP_PACKAGE_PATH_RE.search(blob) and _IMPORT_HYGIENE_RE.search(blob):
        return True
    # F21 (this befund): a DB-less backend pytest targeting the importable app
    # package. Importing the module-under-test boots app.database's async engine
    # at collection, which the Beobachter's env-less check venv can't provision
    # (no aiosqlite / no real DATABASE_URL) → collection rc=2, a pure env gap.
    # Route to the merger like F31. All three signals are required so a purely
    # static pytest card keeps the honest Regel-a done path.
    if (
        _PYTEST_ACCEPTANCE_RE.search(blob)
        and _APP_PACKAGE_PATH_RE.search(blob)
        and _DB_ENGINE_NEXUS_RE.search(blob)
    ):
        return True
    return False


# F39: a Zielentscheidung card — a genuine business/product decision that is the
# OWNER's call (fully remove vs. reduce a feature, which secret-store, which price
# model, which demo strategy …). The Beobachter marks these ``art: zielfrage``,
# ``erwartung: blocked_zielfrage``: the only green terminal is ``blocked`` with a
# prepared Zielfrage for Till — a ``done`` is ALWAYS wrong and lands as
# ``rot-zielentscheid-selbst`` ("Fabrik hat Zielentscheidung selbst getroffen,
# done statt blocked"). Live probe Karte 44 ('Tier-System: Ballast aufloesen
# (VEREINFACHUNGS-FRAGE)', erwartung=blocked_zielfrage, branch=None): the specifier
# correctly read it as "reine Entscheidungs-/Analysekarte, keine Baukarte" but
# handed it onward for CLOSURE; the orchestrator ran a "Schlussprüfung" and the
# voicera-reviewer completed it ``done`` with its OWN recommendation ("vollständig,
# aber gestuft entfernen") → ROT rot-zielentscheid-selbst. The factory may PREPARE
# the decision (options + consequences + context) but must never DECIDE it; it
# escalates with the Zielfrage.
#
# The Beobachter creates the card with only the plain body — no ``--category`` and
# no "this is a Zielfrage" marker reaches the factory (see beobachter.backend_create
# / karte_body_fuer_fabrik) — so detection is content-based, like the F30/F31/F32
# build-gates. These cards share a tight linguistic shape no build/cleanup card
# carries: a "(…-FRAGE)" title tag, "present/compare the Varianten/Folgen of a
# choice", an explicit "zwei Wege/Quellen … oder …" fork, or a naked
# "Geschäftsentscheidung". Verified against all 50 karten: matches every
# ``blocked_zielfrage`` card (44/46/47/48/49), zero ``done`` cards. The DB
# ``category`` is honoured too when the dispatcher did classify it
# (``entscheidung``) — but it is unreliable (the live card t_ffd8371b carried
# ``category=None``), so the content signal is the load-bearing one.
_ZIELENTSCHEID_CARD_RE = re.compile(
    r"\([^)]*\bfrage\)"                                        # "(…-FRAGE)" title tag
    r"|\bvarianten\b[^.\n]{0,80}?\b(?:dar(?:stellen|zustellen)|aufzeigen"
    r"|gegen(?:ueber|über)stell)"
    r"|\bgegen(?:ueber|über)stellung\b"
    r"|\bgesch(?:ae|ä)ftsentscheidung(?:en)?\b"
    r"|\bzwei\s+(?:wege|quellen|varianten|optionen|ans(?:ae|ä)tze"
    r"|m(?:oe|ö)glichkeiten)\b",
    re.IGNORECASE,
)
# The DB escalation category that denotes an owner-decision card (kanban_db
# VALID_CATEGORIES). ``entscheidung`` is set when the factory escalates a genuine
# Zielentscheidung to Till; if such a card reaches kanban_complete it is the same
# contradiction the content signal catches. The content regex stays load-bearing
# because the dispatcher does not always tag it (live card t_ffd8371b: None).
_ZIELENTSCHEID_CATEGORIES = frozenset({"entscheidung"})


def _card_is_zielentscheidung(
    title: str, body: str, category: Optional[str], comment_bodies: list[str]
) -> bool:
    """True when the card is a business/product Zielentscheidung the OWNER must
    make — the factory prepares it and escalates with a Zielfrage (blocked), it
    never self-decides it (done). Honours the DB ``category`` when the dispatcher
    tagged it and falls back to the content signal (the load-bearing path, since
    the Beobachter passes no category marker to the factory)."""
    if (category or "").strip().lower() in _ZIELENTSCHEID_CATEGORIES:
        return True
    blob = "\n".join([title or "", body or "", *(comment_bodies or [])])
    return bool(_ZIELENTSCHEID_CARD_RE.search(blob))


# F44: an owner-SCOPE human-block whose "Zielfrage" is UNPREPARED — the factory
# escalates an open-ended "alle"/"vollständig" scope (e.g. "alle verbleibenden
# englischen UI-Worte übersetzen") by CONCEDING it has no closed check-set (the
# inventory/ledger is falsified, the known regression classes are "nur
# Beispiele", the allowed English terms have "keine abschließende Whitelist")
# AND then asking the OWNER to PRODUCE the complete enumeration ("welche konkret
# aufgezählten Ansichten/Begriffe gelten als vollständig zu übersetzen, welche
# dürfen englisch bleiben?"). That is exactly what the Beobachter's Zielfrage-
# Richter rejects at TOR 2 (kontext): the question is "offene Scoping-Arbeit ohne
# aufbereitete, konkret aufgezählte Optionen samt Folgen — die Fabrik muss die
# Kandidaten-Ansichten/Begriffe zuerst SELBST enumerieren und als Vorschlag
# vorlegen". TOR1 (owner taste dimension) and TOR3 (answerable in plain language)
# are JA, but TOR2 NEIN → rot-technische-rueckfrage (live probe Karte 18
# 't_e7ef743c', klein, erwartung=done: the specifier blocked needs_input with
# precisely this off-loading question).
#
# A legitimate owner escalation is PREPARED: the factory presents the achieved
# state and concrete options WITH consequences, or a plain Abnahme/Freigabe-GO on
# the reached state ("Soll der breit übersetzte Stand als fertig abgenommen
# werden? GO") — never a demand that the owner author the completeness list. So
# the gate mirrors the observer's own TOR2 necessary condition at the worker: it
# rejects a human-block ONLY when BOTH signals are present (AND-ed, like the
# F21/F32 pairs) — (a) the factory's own admission that the scope has no closed/
# prepared set, and (b) an interrogative demand that the owner enumerate which
# views/terms count as done or stay English. A prepared GO/Vorschlag carries
# neither the open-set concession as its question nor an off-loaded "welche …?",
# so it still passes to the richter untouched. Not a facade — it enforces exactly
# the observer's TOR2, never more.
_SCOPE_OPEN_SET_RE = re.compile(
    r"(?:kein\w*|noch\s+kein\w*|ohne)\b[^.\n]{0,40}?"
    r"(?:geschlossene?|abschlie\w+|vollst\w+)\b[^.\n]{0,25}?"
    r"(?:pr[üu]fmenge|whitelist|white-?list|liste|menge|umfang|aufz\w+)"
    r"|\bnur\s+beispiel\w*"
    r"|\bfalsifiziert\b"
    r"|\b(?:kein\w*|ohne)\s+abz[äa]hlbar\w*\s+(?:endpunkt|umfang|ende)\b"
    r"|\bvollst\w+ziel\b"
    r"|ledger[^.\n]{0,40}?(?:falsifiziert|widerlegt)"
    r"|(?:inventur|inventar|menge)[^.\n]{0,40}?widerlegt",
    re.IGNORECASE,
)
_SCOPE_ENUM_DEMAND_RE = re.compile(
    # "welche (konkret aufgezählten) Ansichten/Begriffe/Texte … (als fertig/
    #  vollständig gelten | übersetzt werden | englisch / im Original bleiben)"
    r"welche\b[^?]{0,120}?"
    r"(?:ansichten|sichten|views|begriffe|texte|w[öo]rter|worte|elemente"
    r"|labels?|strings?|fl[äa]chen?)"
    r"[^?]{0,160}?"
    r"(?:vollst\w+|[üu]bersetz\w+|abgeschlossen|als\s+fertig|fertig\s+gelten"
    r"|englisch|im\s+original\s+bleiben|sichtbar\s+bleiben|bleiben\s+d[üu]rfen)"
    # … or an explicit demand that the OWNER author/produce the full list
    r"|(?:inhaber|till|owner|du)\b[^.\n]{0,80}?"
    r"(?:liste\s+der|aufz[äa]hl\w+|enumerier\w+|vollst\w+\s+liste)"
    r"[^.\n]{0,60}?"
    r"(?:erzeug\w+|festleg\w+|benenn\w+|liefer\w+|erstell\w+|vorgeb\w+|aufz[äa]hl\w+)",
    re.IGNORECASE,
)


def _block_is_unprepared_scope_demand(blob: str) -> bool:
    """True when a scope human-block off-loads the completeness enumeration onto
    the owner instead of preparing it (F44). Requires BOTH an open-set concession
    (:data:`_SCOPE_OPEN_SET_RE`) and an owner-enumeration demand
    (:data:`_SCOPE_ENUM_DEMAND_RE`) so a prepared GO/Vorschlag is never caught."""
    if not blob:
        return False
    return bool(_SCOPE_OPEN_SET_RE.search(blob)) and bool(
        _SCOPE_ENUM_DEMAND_RE.search(blob)
    )


def _goal_judge_available() -> bool:
    """True when an auxiliary client is configured for the goal judge.

    ``judge_goal`` is fail-open at the source: when no auxiliary model can
    be reached it returns a ``"continue"`` verdict that is indistinguishable
    from a real "not done yet" judgment. The completion gate must not treat
    that as a rejection, or an unconfigured/degraded auxiliary model would
    wedge every ``goal_mode`` worker (it could never close its own task).

    So we probe availability first and only enforce the gate when a judge is
    actually reachable. This mirrors the same client lookup ``judge_goal``
    performs internally.
    """
    try:
        from agent.auxiliary_client import get_text_auxiliary_client
        client, model = get_text_auxiliary_client("goal_judge")
    except Exception:
        return False
    return client is not None and bool(model)


# ---------------------------------------------------------------------------
# Runtime-activity → board-heartbeat bridge (#31752)
# ---------------------------------------------------------------------------
# When the agent ticks ``_touch_activity`` during normal work (between
# tool calls, mid-stream chunks, etc.), we want the kanban board's
# ``last_heartbeat_at`` columns to reflect that liveness so the dispatcher
# watchdog (which reads ``tasks.last_heartbeat_at``, not the agent's
# in-process timestamp) doesn't reclaim an actively-running worker as
# stale. The model is not required to call the explicit ``kanban_heartbeat``
# tool for this to work — that tool stays available for workers that want
# to attach a note or pre-emptively extend a claim across a known-long op.
#
# Constraints:
#   - Best-effort: never raise. The agent loop must not care if the bridge
#     fails (board missing, DB locked, etc.).
#   - Rate-limited to one DB write per 60s per-process; runtime activity
#     can tick on every chunk/tool result and we don't need that resolution.
#   - No-op outside dispatcher-spawned worker context (no ``HERMES_KANBAN_TASK``).
#   - No durable note on these auto-heartbeats; that's reserved for the
#     explicit tool which carries a model-supplied note.

_AUTO_HEARTBEAT_MIN_INTERVAL_SECONDS = 60.0
_auto_heartbeat_last_attempt: float = 0.0


def heartbeat_current_worker_from_env() -> bool:
    """Best-effort: extend the kanban claim + bump board heartbeat for the
    current dispatcher-spawned worker, using identity from env vars.

    Returns True if a write was attempted (whether or not it succeeded);
    False if the call was skipped (not a kanban worker, rate-limited, or
    swallowed exception). The boolean is informational — callers should
    not branch on it.

    Identity comes from:
      * ``HERMES_KANBAN_TASK`` — task id (required; absence means no-op)
      * ``HERMES_KANBAN_RUN_ID`` — pins the run row so we don't heartbeat
        a stale run that may have already been reclaimed
      * ``HERMES_KANBAN_CLAIM_LOCK`` — claim lock for ``heartbeat_claim``;
        falls back to the default ``_claimer_id()`` for locally-driven
        workers that never went through the dispatcher path

    Rate-limited via the module-level ``_auto_heartbeat_last_attempt``
    timestamp (monotonic clock); not thread-safe in the strict sense, but
    the worst case is one extra DB write per race, which is harmless.
    """
    global _auto_heartbeat_last_attempt
    tid = os.environ.get("HERMES_KANBAN_TASK")
    if not tid:
        return False
    import time as _time
    now = _time.monotonic()
    if (now - _auto_heartbeat_last_attempt) < _AUTO_HEARTBEAT_MIN_INTERVAL_SECONDS:
        return False
    _auto_heartbeat_last_attempt = now
    try:
        kb, conn = _connect()
        try:
            claim_lock = os.environ.get("HERMES_KANBAN_CLAIM_LOCK")
            try:
                kb.heartbeat_claim(conn, tid, claimer=claim_lock)
            except Exception:
                logger.debug("auto-heartbeat: heartbeat_claim failed", exc_info=True)
            run_id_raw = os.environ.get("HERMES_KANBAN_RUN_ID")
            run_id: Optional[int]
            try:
                run_id = int(run_id_raw) if run_id_raw else None
            except (TypeError, ValueError):
                run_id = None
            try:
                kb.heartbeat_worker(conn, tid, note=None, expected_run_id=run_id)
            except Exception:
                logger.debug("auto-heartbeat: heartbeat_worker failed", exc_info=True)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return True
    except Exception:
        logger.debug("auto-heartbeat: bridge failed", exc_info=True)
        return False


def _ok(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields})


def _normalize_profile(value: Any) -> Optional[str]:
    """Normalize CLI-compatible assignee sentinels for the tool surface."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "-", "null"}:
        return None
    return text


def _parse_bool_arg(args: dict, name: str, *, default: bool = False):
    value = args.get(name)
    if value is None:
        return default, None
    if isinstance(value, bool):
        return value, None
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True, None
    if text in {"false", "0", "no"}:
        return False, None
    return default, f"{name} must be a boolean or 'true'/'false'"


def _require_orchestrator_tool(tool_name: str) -> Optional[str]:
    """Belt-and-suspenders runtime guard for orchestrator-only handlers.

    The check_fn (`_check_kanban_orchestrator_mode`) keeps these tools
    out of the worker schema entirely, but in case a stale registration
    or test harness routes a worker to one of them anyway, return a
    structured tool_error so the model gets a clear refusal instead of
    silently mutating board state from a worker context.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return tool_error(
            f"{tool_name} is orchestrator-only; dispatcher-spawned workers "
            "must use kanban_complete, kanban_block, kanban_heartbeat, or "
            "kanban_comment for their assigned task."
        )
    return None


def _task_summary_dict(kb, conn, task) -> dict[str, Any]:
    """Compact task shape for board-listing tools."""
    parents = kb.parent_ids(conn, task.id)
    children = kb.child_ids(conn, task.id)
    return {
        "id": task.id,
        "title": task.title,
        "assignee": task.assignee,
        "status": task.status,
        "priority": task.priority,
        "tenant": task.tenant,
        "workspace_kind": task.workspace_kind,
        "workspace_path": task.workspace_path,
        "project_id": task.project_id,
        "created_by": task.created_by,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
        "current_run_id": task.current_run_id,
        "model_override": task.model_override,
        "parents": parents,
        "children": children,
        "parent_count": len(parents),
        "child_count": len(children),
    }


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _handle_show(args: dict, **kw) -> str:
    """Read a task's full state: task row, parents, children, comments,
    runs (attempt history), and the last N events."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            task = kb.get_task(conn, tid)
            if task is None:
                return tool_error(f"task {tid} not found")
            comments = kb.list_comments(conn, tid)
            events = kb.list_events(conn, tid)
            runs = kb.list_runs(conn, tid)
            parents = kb.parent_ids(conn, tid)
            children = kb.child_ids(conn, tid)

            def _task_dict(t):
                return {
                    "id": t.id, "title": t.title, "body": t.body,
                    "assignee": t.assignee, "status": t.status,
                    "tenant": t.tenant, "priority": t.priority,
                    "workspace_kind": t.workspace_kind,
                    "workspace_path": t.workspace_path,
                    "created_by": t.created_by, "created_at": t.created_at,
                    "started_at": t.started_at,
                    "completed_at": t.completed_at,
                    "result": t.result,
                    "current_run_id": t.current_run_id,
                    "model_override": t.model_override,
                }

            def _run_dict(r):
                return {
                    "id": r.id, "profile": r.profile,
                    "status": r.status, "outcome": r.outcome,
                    "summary": r.summary, "error": r.error,
                    "metadata": r.metadata,
                    "started_at": r.started_at, "ended_at": r.ended_at,
                }

            return json.dumps({
                "task": _task_dict(task),
                "parents": parents,
                "children": children,
                "comments": [
                    {"author": c.author, "body": c.body,
                     "created_at": c.created_at}
                    for c in comments
                ],
                "events": [
                    {"kind": e.kind, "payload": e.payload,
                     "created_at": e.created_at, "run_id": e.run_id}
                    for e in events[-50:]   # cap; full log via CLI
                ],
                "runs": [_run_dict(r) for r in runs],
                # Also surface the worker's own context block so the
                # agent can include it directly if it wants. This is
                # the same string build_worker_context returns to the
                # dispatcher at spawn time.
                "worker_context": kb.build_worker_context(conn, tid),
            })
        finally:
            conn.close()
    except ValueError as e:
        # Invalid board slug surfaces as ValueError from _normalize_board_slug.
        return tool_error(f"kanban_show: {e}")
    except Exception as e:
        logger.exception("kanban_show failed")
        return tool_error(f"kanban_show: {e}")


def _handle_list(args: dict, **kw) -> str:
    """List task summaries with the same core filters as the CLI."""
    # Read-only listing is allowed for task-scoped orchestrator workers
    # whose profile opts into the kanban toolset; only block workers
    # that never had board discovery in their surface.
    if os.environ.get("HERMES_KANBAN_TASK") and not _profile_has_kanban_toolset():
        return tool_error(
            "kanban_list is available only to profiles with the kanban "
            "toolset; dispatcher-spawned workers must use kanban_show for "
            "their assigned task."
        )
    assignee = args.get("assignee")
    status = args.get("status")
    tenant = args.get("tenant")
    include_archived, bool_error = _parse_bool_arg(args, "include_archived")
    if bool_error:
        return tool_error(bool_error)
    limit = args.get("limit")
    if limit is None:
        limit = KANBAN_LIST_DEFAULT_LIMIT
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return tool_error("limit must be an integer")
    if limit < 1:
        return tool_error("limit must be >= 1")
    if limit > KANBAN_LIST_MAX_LIMIT:
        return tool_error(f"limit must be <= {KANBAN_LIST_MAX_LIMIT}")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            # Match CLI list: dependencies that cleared since the last
            # dispatcher tick should be visible to orchestrators immediately.
            promoted = kb.recompute_ready(conn)
            # Fetch one extra row so model-facing output can report that
            # a bounded listing was truncated without dumping the board.
            rows = kb.list_tasks(
                conn,
                assignee=assignee,
                status=status,
                tenant=tenant,
                include_archived=include_archived,
                limit=limit + 1,
            )
            truncated = len(rows) > limit
            tasks = rows[:limit]
            return json.dumps({
                "tasks": [_task_summary_dict(kb, conn, t) for t in tasks],
                "count": len(tasks),
                "limit": limit,
                "truncated": truncated,
                "next_limit": (
                    min(limit * 2, KANBAN_LIST_MAX_LIMIT)
                    if truncated and limit < KANBAN_LIST_MAX_LIMIT else None
                ),
                "promoted": promoted,
            })
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_list: {e}")
    except Exception as e:
        logger.exception("kanban_list failed")
        return tool_error(f"kanban_list: {e}")


def _handle_complete(args: dict, **kw) -> str:
    """Mark the current task done with a structured handoff."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    # A specifier's contract is to hand the card forward to the coder, never
    # to finish it — only the reviewer completes a work card. The specifier
    # SOUL already says this ("rufst NIEMALS kanban_complete"), but a prompt
    # rule is not load-bearing: live card t_cdb72949 showed a specifier call
    # kanban_complete with a verification summary, driving the card straight
    # to `done` (no coder, no branch, no reviewer) and freezing the
    # specifier→coder→reviewer chain (beobachter F01 ordering never greens;
    # verdict lands as F15/F16 "done ohne vollständige Abnahme"). After the
    # premature complete the card is terminal, so the follow-up handoff/block
    # calls all fail ("already terminal" / "not in running/ready") and the
    # specifier strands the card. Enforce the invariant in code and point the
    # specifier back at kanban_handoff. Scoped to the specifier profile so
    # coder/reviewer/orchestrator and read-only helper roles (recon,
    # brain-reader) keep their own completion paths untouched.
    if _normalize_profile(os.environ.get("HERMES_PROFILE")) == "specifier":
        return tool_error(
            "the specifier must not complete a card — your job ends by "
            "handing it forward, not by finishing it. Post your "
            "ABNAHMEKRITERIUM: comment (if it isn't on the card yet), then "
            "call kanban_handoff(to=\"<repo>-coder\") — e.g. "
            "kanban_handoff(to=\"voicera-coder\") for a voicera card. The "
            "dispatcher spawns the coder on the SAME card; only the reviewer "
            "completes a work card."
        )
    summary = args.get("summary")
    metadata = args.get("metadata")
    result = args.get("result")
    if summary:
        summary = redact_sensitive_text(str(summary), force=True)
    if result:
        result = redact_sensitive_text(str(result), force=True)
    if metadata is not None and isinstance(metadata, dict):
        meta_json = json.dumps(metadata)
        meta_json = redact_sensitive_text(meta_json, force=True)
        try:
            metadata = json.loads(meta_json)
        except json.JSONDecodeError:
            pass
    created_cards = args.get("created_cards")
    artifacts = args.get("artifacts")
    if created_cards is not None:
        if isinstance(created_cards, str):
            # Accept a single id as a string for convenience.
            created_cards = [created_cards]
        if not isinstance(created_cards, (list, tuple)):
            return tool_error(
                f"created_cards must be a list of task ids, got "
                f"{type(created_cards).__name__}"
            )
        # Normalise: strings only, stripped, non-empty.
        created_cards = [
            str(c).strip() for c in created_cards if str(c).strip()
        ]
    if artifacts is not None:
        if isinstance(artifacts, str):
            # Accept a single path as a string for convenience.
            artifacts = [artifacts]
        if not isinstance(artifacts, (list, tuple)):
            return tool_error(
                f"artifacts must be a list of file paths, got "
                f"{type(artifacts).__name__}"
            )
        artifacts = [
            str(p).strip() for p in artifacts if str(p).strip()
        ]
        # Carry the artifact list inside metadata so it rides the
        # existing completed-event payload without a schema change at
        # the DB layer.  The gateway notifier reads payload['artifacts']
        # off the completion event and uploads each path as a native
        # attachment.
        if artifacts:
            if metadata is None:
                metadata = {}
            elif not isinstance(metadata, dict):
                return tool_error(
                    f"metadata must be an object/dict, got "
                    f"{type(metadata).__name__}"
                )
            # Don't overwrite an existing metadata.artifacts the worker
            # passed manually — merge instead.
            existing = metadata.get("artifacts")
            if isinstance(existing, (list, tuple)):
                merged: list[str] = []
                seen: set[str] = set()
                for item in list(existing) + artifacts:
                    s = str(item).strip()
                    if s and s not in seen:
                        seen.add(s)
                        merged.append(s)
                metadata["artifacts"] = merged
            else:
                metadata["artifacts"] = artifacts
    if not (summary or result):
        return tool_error(
            "provide at least one of: summary (preferred), result"
        )
    if metadata is not None and not isinstance(metadata, dict):
        return tool_error(
            f"metadata must be an object/dict, got {type(metadata).__name__}"
        )
    metadata = _stamp_worker_session_metadata(tid, metadata)
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            # Goal-mode pre-completion judge gate (Issue #38367).
            # Prevent workers from bypassing the auxiliary judge by
            # calling kanban_complete before acceptance criteria are met.
            # Only enforce when a judge is actually reachable — see
            # _goal_judge_available for why an unavailable judge fails open.
            task = kb.get_task(conn, tid)
            if task and task.goal_mode and _goal_judge_available():
                verdict = "done"
                reason = ""
                try:
                    verdict, reason, _ = judge_goal(
                        goal=f"{task.title}\n\n{task.body or ''}".strip(),
                        last_response=(summary or result or "").strip(),
                    )
                except Exception as judge_exc:
                    # Defensive: judge_goal swallows its own errors, but if
                    # it ever raises, fail open rather than wedge the worker.
                    logger.warning(
                        "goal judge check failed, allowing completion: %s",
                        judge_exc,
                        exc_info=True,
                    )
                if verdict != "done":
                    return tool_error(
                        f"Goal completion rejected by judge: {reason}. "
                        f"To proceed, either: (1) provide explicit acceptance "
                        f"evidence in your summary matching the task's criteria, "
                        f"or (2) create continuation tasks with parents=[{tid}] "
                        f"and keep this task alive."
                    )

            # Zielentscheidung card → escalate, never self-complete (F39). A
            # genuine business/product decision that is the OWNER's call (fully
            # remove vs. reduce a feature, which secret-store, which price model)
            # has exactly one green terminal: `blocked` with a prepared Zielfrage
            # for Till. The Beobachter marks these erwartung=blocked_zielfrage, so
            # a `done` — even a review-grüne one with a well-argued recommendation
            # — is ALWAYS "Fabrik hat Zielentscheidung selbst getroffen (done statt
            # blocked)" → rot-zielentscheid-selbst. Live probe Karte 44 ('Tier-
            # System: Ballast aufloesen (VEREINFACHUNGS-FRAGE)', branch=None): the
            # specifier read it right ("reine Entscheidungs-/Analysekarte") but
            # routed it to closure, the orchestrator ran a Schlussprüfung and the
            # voicera-reviewer completed it `done` with its own recommendation
            # ("vollständig, aber gestuft entfernen") → ROT. Mirror the Beobachter's
            # necessary condition in code (a prompt rule is not load-bearing, cf.
            # F01): a decision card may not be completed; prepare the options +
            # consequences + context, then kanban_block with the Zielfrage. Scoped
            # to a card that carries NO branch (a decision card changes no code; a
            # real build card has a pushed wt/<id>), so the honest done path of
            # every build/cleanup card is untouched. Fires for whoever reaches
            # completion (reviewer or orchestrator — the specifier is already gated
            # out of kanban_complete above). Runs in the worker's own short-lived
            # process (re-imported per spawn), so it survives the gateway-restart
            # gap that defeats dispatcher-side fixes (cf. F19).
            if task and not (task.branch_name or "").strip():
                ze_comments = [
                    (r[0] or "")
                    for r in conn.execute(
                        "SELECT body FROM task_comments WHERE task_id = ? "
                        "ORDER BY created_at",
                        (tid,),
                    ).fetchall()
                ]
                if _card_is_zielentscheidung(
                    task.title, task.body or "", task.category, ze_comments
                ):
                    conn.close()
                    return tool_error(
                        "this is a Zielentscheidung — a business/product decision "
                        "that is Till's to make, not the factory's. The card asks "
                        "you to PREPARE the choice (lay out the variants, their "
                        "consequences for code and data, the context), not to "
                        "DECIDE it. Completing it `done` — even with a well-argued "
                        "recommendation — means the factory took the owner's "
                        "decision itself; the Beobachter rejects that as "
                        "rot-zielentscheid-selbst (\"Frage haette die Fabrik selbst "
                        "loesen muessen\" has an inverse here: a Zielentscheidung "
                        "the fabric may NOT resolve alone). Do NOT call "
                        "kanban_complete. Post the prepared analysis as a comment "
                        "(what the feature steers today, where in the code, the "
                        "cost/consequences of each variant — including anything "
                        "irreversible for existing data/tenants), then call "
                        "kanban_block(kind=\"needs_input\") with the Zielfrage "
                        "stated AS A QUESTION in a kanban COMMENT containing \"?\" "
                        "(that is the only place Till reads it) and carrying that "
                        "context. Keep the question business-level and free of "
                        "internal task IDs / branch names."
                    )

            # The orchestrator coordinates a work card, it never finishes one:
            # only the reviewer completes a repo-bound work card, at the end of
            # specifier → coder → reviewer. The orchestrator SOUL already says
            # this ("Dein letzter Schritt auf dieser Karte ist der
            # kanban_handoff an den Specifier — NICHT kanban_complete"; "rufe
            # [kanban_complete] NIE für eine Karte, die noch gebaut/geprüft
            # werden muss"), but — as F01 proved for the specifier — a prompt
            # rule is not load-bearing. Live probe card t_46a39038 (repository
            # voicera, child_role work) showed the orchestrator drive the card
            # straight to `done` itself: branch=None, workspace scratch, no
            # coder and no reviewer ever ran, and the implementation was spun
            # off onto an unrelated recon card (t_f54f8e8d). The Beobachter's
            # Done-Abnahme (Regel a) then surfaces branch=False, reviewer=None
            # → "done ohne vollständige Abnahme" (F21). Enforce the invariant
            # in code: the orchestrator must not complete a repo work card that
            # has neither a branch (a coder pushed nothing) nor work children
            # (it was never decomposed) — point it back at kanban_handoff. A
            # genuine decomposition root keeps its path: it carries work
            # children that ran the pipeline, so the children-count check lets
            # it through. Non-repo coordination cards (no repository) are
            # untouched. This runs in the orchestrator's own short-lived worker
            # (kanban_db/kanban_tools re-imported per spawn), so it survives the
            # gateway-restart gap that defeats dispatcher-side fixes (cf. F19).
            if (
                task
                and _normalize_profile(os.environ.get("HERMES_PROFILE"))
                == "orchestrator"
                and (task.repository or "").strip()
                and not (task.branch_name or "").strip()
            ):
                work_children = conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE id != ? "
                    "AND child_role = 'work' AND status != 'archived' "
                    "AND (parent_id = ? OR family_root_id = ?)",
                    (tid, tid, tid),
                ).fetchone()[0]
                if not work_children:
                    return tool_error(
                        "the orchestrator must not complete a repo work card — "
                        "your job ends by handing it forward, not by finishing "
                        "it. This card has no branch (no coder pushed anything) "
                        "and no work children (it was never decomposed), so "
                        "nothing was built or reviewed; only the reviewer "
                        "completes a work card. Keep it ONE card: post your "
                        "orchestration note, then call "
                        "kanban_handoff(to=\"specifier\"). The specifier lays "
                        "down the ABNAHMEKRITERIUM and hands off to the repo "
                        "coder (e.g. voicera-coder); the coder pushes wt/<id> "
                        "and hands off to the reviewer, who completes the card. "
                        "Do NOT spin the work onto a separate card."
                    )

            # Build-gated reviewer completion → route to the merger, don't
            # self-complete to `done` (F30, sibling of the F21 orchestrator and
            # F01 specifier completion guards). The reviewer is read-only and
            # cannot run a node build; the Beobachter re-runs the card's
            # acceptance shell in a throwaway worktree that has no
            # `node_modules`, so a FRONTEND card's `npx tsc`/`vitest`/`npm`
            # acceptance can NEVER go green there. A `done` is then literally
            # "done ohne vollständige Abnahme": branch=True, reviewer=ok, but
            # shell=ROT on the unverifiable build → Regel a stays red every
            # re-check (live probe Karte 5 'leereTexte entfernen', t_cbc5b67e:
            # branch=True, shell=rot(`npx tsc`: no TS in the check-worktree),
            # reviewer=voicera-reviewer, ergebnis_passt=True → ROT a). The
            # honest terminal for a build-gated, reviewed change is the MERGER:
            # it runs in the real checkout (tenant_workdir, WITH node_modules),
            # does the typecheck/build as its own pre-merge gate, and the
            # product-repo merge is a Freigabe that is Till's GO to give. So the
            # reviewer hands the card to `ready @ merger` with an explicit
            # merge-Freigabe question. The Beobachter greens it either as Regel
            # c (`ready @ merger`, read before the dispatcher's GO-gate parks
            # it) or — once the GO-gate flips it to `blocked @ till` — as Regel
            # b (the merge-GO is a legitimate Freigabe-Zielfrage; the note is a
            # `?`-comment and carries no TECH_REGEX term). A backend card is
            # untouched: its ruff/pytest acceptance runs in the Beobachter venv,
            # so its `done` is honest and Regel a greens it. Runs in the
            # reviewer's own short-lived worker (re-imported per spawn), so it
            # survives the gateway-restart gap that defeats dispatcher-side
            # fixes (cf. F19). Only fires for a repo-bound card that carries a
            # branch (a real build artifact the merger can act on).
            if (
                task
                and _is_reviewer_profile(os.environ.get("HERMES_PROFILE"))
                and (task.repository or task.tenant or "").strip()
                and (task.branch_name or "").strip()
            ):
                comment_bodies = [
                    (r[0] or "")
                    for r in conn.execute(
                        "SELECT body FROM task_comments WHERE task_id = ? "
                        "ORDER BY created_at",
                        (tid,),
                    ).fetchall()
                ]
                if _card_is_build_gated(task.title, task.body or "", comment_bodies):
                    repo_key = (
                        (task.repository or task.tenant or "").strip().lower()
                    )
                    merge_target = "das Merge-Ziel des Repository-Profils"
                    try:
                        profiles = (
                            (load_config().get("kanban", {}) or {})
                            .get("repository_profiles", {})
                            or {}
                        )
                        prof = profiles.get(repo_key) or {}
                        mt = (prof.get("merge_target") or "").strip()
                        if mt:
                            merge_target = mt
                    except Exception:  # noqa: BLE001 — config best-effort only
                        pass
                    branch = (task.branch_name or "").strip()
                    go_note = (
                        f"FREIGABE nötig (Merge-GO): Branch `{branch}` ist "
                        f"review-grün — das Abnahmekriterium ist erfüllt, aber "
                        f"die Abnahme ist build-/laufzeit-gated (Frontend-Build "
                        f"oder Laufzeit-Import des Backends, der Secrets/DB "
                        f"braucht) und lässt sich read-only nicht abschliessend "
                        f"verifizieren. Der Merger baut/importiert im echten "
                        f"Checkout (mit node_modules UND echter .env), führt "
                        f"Typecheck/Build bzw. den App-Import aus und merged nach "
                        f"`{merge_target}`. Soll dieser Merge freigegeben werden "
                        f"(GO)?"
                    )
                    handed = kb.handoff_task(
                        conn, tid, "merger",
                        note=go_note,
                        author=_normalize_profile(
                            os.environ.get("HERMES_PROFILE")
                        ),
                        expected_run_id=_worker_run_id(tid),
                    )
                    if handed:
                        return _ok(
                            task_id=tid,
                            status="ready",
                            assignee="merger",
                            note=(
                                "build-/runtime-gated change → handed to the "
                                "merger as a merge-Freigabe (ready @ merger) "
                                "instead of done. A frontend acceptance (npx tsc "
                                "/ vitest / npm) or a backend runtime import "
                                "(python -c 'import app.…', uvicorn — needs the "
                                "real .env secrets) cannot be verified read-only "
                                "nor in the Beobachter's node_modules-less / "
                                "env-less check-worktree, so a self-`done` would "
                                "be 'done ohne vollständige Abnahme'. The merger "
                                "builds/imports in the real checkout and performs "
                                "the merge after Till's GO."
                            ),
                        )
                    # Handoff failed (card not in running/ready, or lost the
                    # claim race). Fall through to the normal completion path
                    # rather than wedging the reviewer.

            try:
                ok = kb.complete_task(
                    conn, tid,
                    result=result, summary=summary, metadata=metadata,
                    created_cards=created_cards,
                    expected_run_id=_worker_run_id(tid),
                )
            except kb.ArtifactPreservationError as artifact_err:
                return tool_error(
                    f"kanban_complete could not preserve the declared artifacts: "
                    f"{artifact_err}. Your task is still in-flight and its "
                    f"scratch workspace was kept. Fix the artifact path or "
                    f"storage error, then retry kanban_complete with the same handoff."
                )
            except kb.DoneVerificationError as verify_err:
                # The orchestrator's reality check rejected the completion.
                # The card is ALREADY back in 'ready' with the findings as
                # a comment — the worker must not retry kanban_complete
                # blindly; the findings need to be fixed first (push the
                # branch, bring the preview up, document the tests).
                return tool_error(
                    "kanban_complete rejected by done-verification: "
                    + "; ".join(verify_err.findings)
                    + ". The card was returned to 'ready' with these findings "
                    "as a comment. Fix the findings (push the branch / bring "
                    "the preview up / document test results), then complete "
                    "again on the next run."
                )
            except kb.HallucinatedCardsError as hall_err:
                # Structured rejection — surface the phantom ids so the
                # worker can retry with a corrected list or drop the
                # field. Audit event already landed in the DB.
                #
                # The task itself was NOT mutated (the gate runs before
                # the write txn), so the worker can simply call
                # kanban_complete again. Spell that out — without it the
                # model often interprets a tool_error as a terminal
                # failure and either blocks or crashes the run instead
                # of retrying. See #22923.
                return tool_error(
                    f"kanban_complete blocked: the following created_cards "
                    f"do not exist or were not created by this worker: "
                    f"{', '.join(hall_err.phantom)}. "
                    f"Your task is still in-flight (no state change). "
                    f"Retry kanban_complete with the same summary/metadata "
                    f"and either drop these ids from created_cards, or pass "
                    f"created_cards=[] to skip the card-claim check entirely."
                )
            if not ok:
                return tool_error(
                    f"could not complete {tid} (unknown id or already terminal)"
                )
            run = kb.latest_run(conn, tid)
            return _ok(task_id=tid, run_id=run.id if run else None)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_complete: {e}")
    except Exception as e:
        logger.exception("kanban_complete failed")
        return tool_error(f"kanban_complete: {e}")


def _handle_block(args: dict, **kw) -> str:
    """Transition the task to blocked with a reason a human will read."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    reason = args.get("reason")
    if not reason or not str(reason).strip():
        return tool_error("reason is required — explain what input you need")
    reason = redact_sensitive_text(str(reason), force=True)
    kind = args.get("kind")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        if kind is not None and kind not in kb.VALID_BLOCK_KINDS:
            conn.close()
            return tool_error(
                f"kind must be one of {sorted(kb.VALID_BLOCK_KINDS)} (or omit it)"
            )
        # Goal-mode block gate (Issue #38696, sibling of the kanban_complete
        # judge gate in #38367). kanban_block is a second exit path out of
        # the goal loop — run_kanban_goal_loop() treats ANY `blocked` status
        # as terminal, identically to `done`, regardless of kind. Without
        # this, a worker that learns kanban_complete is gated can just call
        # kanban_block(reason="anything") to escape the loop instead.
        # Restrict goal_mode tasks to the kinds that represent a genuine
        # external blocker the worker cannot resolve itself; `capability`
        # and `transient` (or an unset kind) route back through
        # kanban_complete, which the judge now gates.
        task = kb.get_task(conn, tid)
        if (
            task
            and task.goal_mode
            and kind not in _GOAL_MODE_BLOCK_ALLOWED_KINDS
        ):
            conn.close()
            return tool_error(
                f"goal_mode tasks can only block with kind in "
                f"{sorted(_GOAL_MODE_BLOCK_ALLOWED_KINDS)} (got {kind!r}). "
                f"If the task is actually finished or cannot proceed for "
                f"another reason, call kanban_complete instead — the "
                f"completion judge will evaluate it."
            )
        # Unprepared-scope-demand block gate (F44, sibling of the F23/F24/F43
        # scope gates). A specifier or orchestrator that cannot mechanically
        # close an open-ended "alle"/"vollständig" scope must NOT escalate by
        # off-loading the enumeration onto the owner — conceding there is no
        # closed check-set (ledger falsified, classes "nur Beispiele", "keine
        # abschließende Whitelist") and then asking "welche konkret aufgezählten
        # Ansichten/Begriffe gelten als vollständig zu übersetzen, welche dürfen
        # englisch bleiben?". The observer's richter marks that TOR1 JA / TOR3 JA
        # but TOR2 NEIN (offene Scoping-Arbeit ohne aufbereitete Optionen) →
        # rot-technische-rueckfrage (live probe Karte 18 't_e7ef743c'). A
        # legitimate owner escalation is PREPARED: present the achieved state and
        # concrete options WITH consequences, or a plain Abnahme/Freigabe-GO on
        # the reached state — never a demand that the owner author the list. The
        # gate scans the block reason + the full comment thread (the factory
        # states the off-loading question in a comment, which is where the
        # observer reads it) and fires only when BOTH signals are present, so a
        # prepared GO/Vorschlag passes untouched. Scoped to the two scope-owning
        # roles + human-escalation kinds; worker-side (re-imported per spawn),
        # live on the next spawn without a gateway restart (cf. F19).
        if (
            task
            and _normalize_profile(os.environ.get("HERMES_PROFILE"))
            in ("specifier", "orchestrator")
            and kind in _HUMAN_BLOCK_KINDS
        ):
            scope_blob = str(reason or "")
            try:
                scope_blob += "\n" + "\n".join(
                    c.body or "" for c in kb.list_comments(conn, tid)
                )
            except Exception:
                pass
            if _block_is_unprepared_scope_demand(scope_blob):
                conn.close()
                return tool_error(
                    "this is an UNPREPARED scope escalation — you concede the "
                    "'alle'/'vollständig' scope has no closed check-set and then "
                    "ask the owner to produce the complete list of views/terms "
                    "('welche konkret aufgezählten Ansichten/Begriffe …?'). That "
                    "is off-loading the fabric's own enumeration work: the "
                    "observer's richter judges it TOR1 JA / TOR3 JA but TOR2 NEIN "
                    "(offene Scoping-Arbeit ohne aufbereitete, konkret "
                    "aufgezählte Optionen samt Folgen) → rot-technische-"
                    "rueckfrage. The fabric must enumerate the candidate views/"
                    "terms ITSELF first. Two clean moves: (a) define a CLOSED, "
                    "enumerated cut yourself (exactly these views/terms are in "
                    "scope, the rest is declared Nicht-Scope), write the "
                    "ABNAHMEKRITERIUM and kanban_handoff(to=\"<repo>-coder\") so "
                    "the card converges to done; or (b) if the owner decision "
                    "truly cannot be avoided, escalate a PREPARED proposal — a "
                    "plain Abnahme/Freigabe-GO on the reached state ('Soll der "
                    "aktuell erreichte, breit übersetzte Stand für diese Runde "
                    "als fertig abgenommen werden? GO; Folge bei GO: vereinzelte "
                    "dynamische Resttexte bleiben vorerst im Original') OR the "
                    "concrete options WITH their consequences — NEVER a question "
                    "that asks the owner to author the completeness list."
                )
        # Orchestrator block gate (F22, sibling of the F21 complete gate). The
        # orchestrator coordinates a fresh repo work card by decomposing it and
        # handing it to the specifier — it never escalates one to a human
        # before anything was built. Live probe card t_e8b0ae64 (repository
        # voicera, child_role work, no branch) showed the orchestrator
        # kanban_block(kind='needs_input') it with a "DUPLICATE TASK DETECTED"
        # note — a pure coordination decision, NOT a question for Till. The
        # Beobachter extracts no question from the comment (no "?"), so the
        # three Zielfrage gates are all NO → "rot-technische-rueckfrage: Frage
        # haette die Fabrik selbst loesen muessen" (F22). A perceived duplicate
        # (especially against an ARCHIVED card, which is not even on the board)
        # is the fabric's own problem to resolve, not a human blocker. Enforce
        # the invariant in code — a prompt rule is not load-bearing (cf. F01):
        # the orchestrator must not land a repo work card that has neither a
        # branch (no coder pushed) nor work children (never decomposed) in the
        # human blocked bucket. Point it back at kanban_handoff. Restricted to
        # the human-escalation kinds so genuine machine routing (``dependency``
        # → todo, incl. the SOUL's file-conflict block) stays untouched; the
        # whole human bucket is gated together so the orchestrator cannot
        # kind-shop (needs_input→capability) to escape. Runs in the
        # orchestrator's own short-lived worker (re-imported per spawn), so it
        # survives the gateway-restart gap that defeats dispatcher-side fixes
        # (cf. F19).
        if (
            task
            and _normalize_profile(os.environ.get("HERMES_PROFILE"))
            == "orchestrator"
            and kind in _HUMAN_BLOCK_KINDS
            and (task.repository or "").strip()
            and not (task.branch_name or "").strip()
        ):
            work_children = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE id != ? "
                "AND child_role = 'work' AND status != 'archived' "
                "AND (parent_id = ? OR family_root_id = ?)",
                (tid, tid, tid),
            ).fetchone()[0]
            if not work_children:
                conn.close()
                return tool_error(
                    "the orchestrator must not block a repo work card for a "
                    "human — your job is to coordinate it forward, not to "
                    "escalate it. This card has no branch (no coder pushed "
                    "anything) and no work children (it was never decomposed), "
                    "so nothing was built; a human block here is not a "
                    "legitimate Zielfrage. If this looks like a duplicate "
                    "(especially of an archived card — those are off the "
                    "board) or any other coordination/technical matter, resolve "
                    "it yourself and proceed. Keep it ONE card: post your "
                    "orchestration note, then call "
                    "kanban_handoff(to=\"specifier\"). The specifier lays down "
                    "the ABNAHMEKRITERIUM and hands off to the repo coder "
                    "(e.g. voicera-coder); the coder pushes wt/<id> and hands "
                    "off to the reviewer, who completes the card. Only block "
                    "for a genuine business decision that is Till's to make — "
                    "and the specifier/coder raises that when they hit it."
                )
        # Specifier block gate (F23 + F24, sibling of the F22 orchestrator
        # gate). The specifier is the pre-build gatekeeper: when it cannot pass
        # a card to the coder it blocks "wartet auf Till" — but ONLY for a
        # genuine Zielfrage, which its SOUL mandates it phrase AS A QUESTION
        # ("brauche Klaerung: …?"). The gate mirrors the Beobachter's OWN two
        # mechanical necessary conditions for a blocked done-card to go green,
        # so it rejects exactly what the observer would reject — never more
        # (not a facade; the LLM business-judgment TOR1/2/3 stays the
        # observer's):
        #
        #   1. _letzte_frage reads the Zielfrage ONLY from task COMMENTS (the
        #      last comment whose body holds a "?"), NEVER from the block
        #      reason. A question that lives only in the block `reason` is
        #      invisible to the observer → frage="" → all three Zielfrage-Tore
        #      NO → rot-technische-rueckfrage. So require the "?" in a COMMENT,
        #      posted BEFORE the block (the observer may snapshot at any moment).
        #   2. TECH_REGEX forces zielfrage=False and the richter cannot override
        #      it — a technical-term "question" can never be green.
        #
        # Two live probe cards, same title, drove this:
        #   • t_b4335c9c (F23): blocked with a "which endpoint/line is meant"
        #     clarification — a scope/recon task the specifier must resolve
        #     itself (recon greps the named file for the named anchor).
        #   • t_664772aa (F24): blocked needs_input with the question only in
        #     the block REASON — "…apply changes from worktree or re-edit from
        #     scratch?" — an internal implementation/coordination decision
        #     (reuse prior uncommitted worktree work, or redo) that is the
        #     fabric's own to make, never Till's, and that the observer never
        #     saw (reason-only, no comment) → frage="" → rot.
        #
        # Enforce the specifier's own contract in code (a prompt rule is not
        # load-bearing, cf. F01). Scoped to the specifier + human-escalation
        # kinds so the coder's genuine-wall block path and machine routing
        # (dependency → todo) stay free. Runs in the specifier's own short-lived
        # worker (re-imported per spawn), so it is live on the next specifier
        # spawn without a gateway restart (cf. F19).
        if (
            task
            and _normalize_profile(os.environ.get("HERMES_PROFILE"))
            == "specifier"
            and kind in _HUMAN_BLOCK_KINDS
        ):
            # Compute `frage` like the Beobachter's _letzte_frage (last comment
            # carrying a "?"), but restricted to the specifier's OWN genuine
            # question comment: the last comment that (a) carries a "?",
            # (b) is authored by the specifier (the role raising the Zielfrage —
            # its SOUL mandates it phrase its Zielfrage AS A QUESTION in a
            # comment, "brauche Klaerung: …?"), and (c) is NOT its signed
            # `ABNAHMEKRITERIUM:` acceptance criterion. Block reasons are NOT
            # comments and are deliberately ignored (F24).
            #
            # F28 (second occurrence, live probe card t_82585a19 'Hermes aus dem
            # Frontend verbannen', klein, erwartung=done): the specifier wrote a
            # proper signed `ABNAHMEKRITERIUM:` comment (card baureif) and then a
            # read-only File-Conflict-Check sub-run — unable to kanban_complete
            # (F01 role gate) and self-scoped out of a handoff — blocked
            # needs_input with a REASON carrying no "?". The naive _letzte_frage
            # latched onto the ABNAHMEKRITERIUM comment, which quotes a "?"-
            # bearing UI error string ("… Hermes nicht erreichbar?") and trips no
            # TECH_REGEX, so the old gate passed. The observer then judges that
            # extracted "frage" NOT a Zielfrage (it is a mechanical build
            # order/criterion) → rot-technische-rueckfrage. An ABNAHMEKRITERIUM
            # is by definition what makes a card baureif — never a business
            # Zielfrage — and a build order is not the specifier's Zielfrage, so
            # excluding both (and non-specifier comments) is a safe mirror: it
            # rejects only blocks the observer would also reject (a genuine
            # business Zielfrage is specifier-authored and is not an acceptance
            # criterion, so it still passes to the richter — see the allowed
            # test).
            frage = ""
            has_abk = False
            try:
                for c in kb.list_comments(conn, tid):
                    body = c.body or ""
                    is_specifier = (c.author or "").strip().lower() == "specifier"
                    is_abk = body.lstrip().upper().startswith("ABNAHMEKRITERIUM")
                    if is_specifier and is_abk:
                        has_abk = True
                    if "?" not in body or not is_specifier or is_abk:
                        continue
                    frage = body
            except Exception:
                frage = ""
            tech = bool(_SPECIFIER_TECH_REGEX.search(frage)) if frage else False
            if not frage or tech:
                conn.close()
                return tool_error(
                    "the specifier blocks only for a genuine Zielfrage — and "
                    "your SOUL requires you phrase it AS A QUESTION (\"brauche "
                    "Klaerung: …?\") and POST IT AS A KANBAN COMMENT (that is "
                    "the only place Till reads it; a question left only in the "
                    "block reason is invisible and can never clear). "
                    + (
                        "This question carries a technical term, so it is a "
                        "method/scope question, not a business Zielfrage — "
                        "resolve it yourself. "
                        if tech else
                        (
                            "You have ALREADY posted a signed ABNAHMEKRITERIUM: "
                            "comment, so this card is baureif — your next move is "
                            "kanban_handoff(to=\"<repo>-coder\"), NOT a block. An "
                            "ABNAHMEKRITERIUM is never a Zielfrage, and a block "
                            "whose reason carries no question is not one either. "
                            if has_abk else
                            "No question comment of your own is on this card, so "
                            "this block is not a legitimate Zielfrage. "
                        )
                    )
                    + "An internal implementation/coordination decision — "
                    "\"which line/endpoint/file is meant\", \"reuse the prior "
                    "uncommitted work from an old worktree or redo it from "
                    "scratch\", \"which approach\" — is NEVER a Zielfrage: you "
                    "resolve it yourself. A card whose body already states "
                    "exactly what to produce (e.g. \"CLAUDE.md gdpr-delete-"
                    "Zeile entschaerfen\": reword so the endpoint only deletes "
                    "after confirmation and only the own tenant) is baureif — "
                    "ignore any stale/archived prior worktree, write the "
                    "ABNAHMEKRITERIUM: comment, and call "
                    "kanban_handoff(to=\"<repo>-coder\") (the coder redoes the "
                    "edit cleanly on wt/<id> and hands off to the reviewer, who "
                    "completes the card). Only block for a business/Freigabe "
                    "decision that is Till's to make, stated as a question in a "
                    "comment."
                )
        # Coder block gate (F28, sibling of the specifier gate F23/F24). The
        # repo coder keeps its genuine-wall block path for a real business/
        # Freigabe decision (there is a test allowing "need a decision from
        # Till"), but it may NOT escalate INTERNAL ARBEITSKOORDINATION to a
        # human. Live probe card t_9b6d4067 ('Ungenutzte Funktion leereTexte
        # entfernen', klein, erwartung=done): the specifier noted the function
        # was already removed on wt/t_7e8f28b1 (NOT merged to develop); the
        # coder, finding the function STILL in origin/develop, blocked
        # needs_input with "Ist t_9b6d4067 ein Duplikat von t_7e8f28b1? Karte
        # schliessen oder neu bauen?" — a pure task-identity/merge-status triage
        # aimed at the ORCHESTRATOR, not Till. That question has a "?" and trips
        # no TECH_REGEX, so the specifier-style checks miss it, but the
        # Beobachter's richter rejects it: TOR1 NO (internal coordination, no
        # business dimension), TOR3 NO (needs internal task-ID/branch/merge
        # knowledge) → rot-technische-rueckfrage. Mirror that necessary
        # condition in code: a human-escalation block whose question (comment
        # with "?" — read like the observer's _letzte_frage — or the reason,
        # which guards against kind-shopping the question into the reason)
        # references internal task state is the fabric's own to resolve. Scoped
        # to repo coders (profile ends "-coder"), which excludes the orchestrator
        # and specifier (their own gates above). Worker-side (re-imported per
        # spawn), live on the next coder spawn with no gateway restart (cf. F19).
        if (
            task
            and (_normalize_profile(os.environ.get("HERMES_PROFILE")) or "")
            .endswith("-coder")
            and kind in _HUMAN_BLOCK_KINDS
        ):
            frage = ""
            all_comment_bodies: list[str] = []
            try:
                for c in kb.list_comments(conn, tid):
                    body = c.body or ""
                    all_comment_bodies.append(body)
                    if "?" in body:
                        frage = body
            except Exception:
                pass
            blob = f"{frage}\n{reason}"
            if _CODER_INTERNAL_TRIAGE_REGEX.search(blob):
                conn.close()
                return tool_error(
                    "the coder may not escalate internal work-coordination to "
                    "a human. This block's question is about internal task "
                    "state — task IDs, wt/ worktree branches, a duplicate or "
                    "merge-status triage — which Till never sees and cannot "
                    "answer on a business basis; the Beobachter rejects it as "
                    "rot-technische-rueckfrage (\"Frage haette die Fabrik "
                    "selbst loesen muessen\"). Resolve it yourself: a perceived "
                    "duplicate, or an orphan prior worktree (a change already "
                    "made on another wt/<id> branch but NOT merged into the "
                    "base ref), is RECON, never a Rueckfrage. If the target "
                    "code still exists in the base ref, the work is NOT done — "
                    "ignore the orphan branch, make the edit cleanly on your "
                    "own wt/<id> from the base ref, and hand it to the reviewer "
                    "the normal way. Only block for a genuine business/Freigabe "
                    "decision that is Till's to make — stated as a question in "
                    "a comment, free of internal task IDs and branch names."
                )
            # F37: a coder human-block that names the acceptance criterion /
            # specifier spec AND frames it as defective is a factory-internal
            # coder->specifier->coder correction, not a Till Zielfrage. Route it
            # back to the specifier (who owns the ABNAHMEKRITERIUM) instead of
            # parking a question-less blocked done-card at a human. Scanned over
            # the FULL comment thread + reason (not the "?"-filtered frage): a
            # criterion-defect finding is usually a statement ("…muss korrigiert
            # werden"), not a question, so it would otherwise slip past a terse
            # reason when posted as a plain comment.
            spec_blob = "\n".join([*all_comment_bodies, reason])
            if (
                _CODER_SPEC_REF_RE.search(spec_blob)
                and _CODER_SPEC_DEFECT_RE.search(spec_blob)
            ):
                conn.close()
                return tool_error(
                    "the coder may not escalate a defective acceptance criterion "
                    "to a human. The ABNAHMEKRITERIUM is the SPECIFIER's to write "
                    "and fix — it owns the criterion. When it is technically "
                    "wrong (e.g. it calls the public API with the wrong argument "
                    "shape, or contradicts the verified signature), that is a "
                    "factory-internal coder->specifier->coder correction, NOT a "
                    "Zielfrage for Till: Till never sees the criterion and cannot "
                    "answer it, so the Beobachter rejects the question-less "
                    "blocked done-card as an infra escalation. Do NOT block. Hand "
                    "the card BACK to the specifier to correct the criterion: "
                    "post ONE comment stating the exact contradiction and the "
                    "corrected criterion you need (the verified signature / "
                    "argument shape, no files changed), then call "
                    "kanban_handoff(to=\"specifier\"). The specifier rewrites the "
                    "ABNAHMEKRITERIUM and re-hands the card to you; you then build "
                    "cleanly on wt/<id> and hand off to the reviewer, who "
                    "completes the card. Only block for a genuine business/"
                    "Freigabe decision that is Till's to make."
                )
        try:
            ok = kb.block_task(
                conn, tid,
                reason=reason,
                kind=kind,
                expected_run_id=_worker_run_id(tid),
            )
            if not ok:
                return tool_error(
                    f"could not block {tid} (unknown id or not in "
                    f"running/ready)"
                )
            run = kb.latest_run(conn, tid)
            # Tell the worker where the task actually landed so it doesn't
            # assume it's sitting in 'blocked' when routing sent it elsewhere.
            landed = kb.get_task(conn, tid)
            return _ok(
                task_id=tid,
                run_id=run.id if run else None,
                status=landed.status if landed else "blocked",
                block_kind=kind,
            )
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_block: {e}")
    except Exception as e:
        logger.exception("kanban_block failed")
        return tool_error(f"kanban_block: {e}")


def _handle_handoff(args: dict, **kw) -> str:
    """Hand the SAME card forward to the next role (orchestrator → specifier
    → coder) without finishing it. The forward-flow sibling of the review
    handoff: the card goes back to ``ready`` assigned to ``to`` and the
    dispatcher re-spawns the next role on it."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    to_profile = _normalize_profile(
        args.get("to") or args.get("to_profile") or args.get("assignee")
    )
    if not to_profile:
        return tool_error(
            "to is required — name the profile that takes the card next "
            "(e.g. 'specifier', then the repo coder like 'hermes-coder')"
        )
    note = args.get("note") or args.get("comment")
    if note:
        note = redact_sensitive_text(str(note), force=True)
    board = args.get("board")
    author = _normalize_profile(os.environ.get("HERMES_PROFILE"))
    # Explicit workspace overrides (optional); otherwise auto-upgrade a
    # coder-target handoff to a git worktree so the dispatcher records
    # branch_name on the card (Regel-a abnahme + review-worktree resolution
    # both depend on it). The checkout path comes from the operator-configured
    # tenant_workdirs map — absent → no upgrade (card stays scratch).
    ws_kind = args.get("workspace_kind")
    ws_path = args.get("workspace_path")
    try:
        kb, conn = _connect(board=board)
        try:
            # Specifier forward-handoff gate (F25, sibling of the F22/F23/F24
            # block gates). The specifier is the pre-build gatekeeper: after it
            # writes the ABNAHMEKRITERIUM its ONLY forward move is
            # kanban_handoff(to="<repo>-coder") — a role that can actually build
            # (specifier SOUL). recon / brain-reader are read-only helper roles
            # the specifier SPAWNS as delegation subagents for facts ("kein
            # Write, kein Edit, keine Commits" — recon SOUL); they are never
            # kanban card owners in the build chain. Live probe card t_37376e6a
            # ('CLAUDE.md gdpr-delete-Zeile entschaerfen', erwartung=done) died
            # here: an orchestrator note mislabeled "Coder-Ziel: recon
            # (documentation-only, read-write file access)" — a FALSE capability —
            # and the specifier SOUL honours the orchestrator's "Coder-Ziel"
            # override, so the specifier handed the write card to recon. recon
            # correctly reported a role-mismatch (read-only, but the card expects
            # a write) and blocked kind=needs_input with no "?" — a pure
            # coordination block, no business Zielfrage → the Beobachter sees a
            # done-card blocked without a legitimate question →
            # "rot-technische-rueckfrage: Frage haette die Fabrik selbst loesen
            # muessen" (F25). The same card greens the moment it is routed to
            # voicera-coder instead (the F24 happy path). Enforce the specifier's
            # own contract in code (a prompt rule is not load-bearing, cf. F01):
            # a read-only role is NEVER a valid forward handoff target — point
            # the specifier at the real <repo>-coder and ignore any upstream
            # "Coder-Ziel: recon" label. Scoped to the specifier so an
            # orchestrator dispatching a genuine read-only verification/recon
            # card to recon stays free. Runs in the specifier's own short-lived
            # worker (re-imported per spawn), live on the next specifier spawn
            # without a gateway restart (cf. F19).
            if author == "specifier" and to_profile in _READ_ONLY_HANDOFF_ROLES:
                htask = kb.get_task(conn, tid)
                repo = ((htask.repository if htask else None) or "").strip()
                coder = f"{repo}-coder" if repo else "<repo>-coder"
                conn.close()
                return tool_error(
                    f"the specifier must not hand a build card to {to_profile!r} "
                    f"— that role is read-only (no write, no edit, no commits) "
                    f"and cannot build, so the card would strand there. recon / "
                    f"brain-reader are delegation subagents you SPAWN for facts, "
                    f"never kanban-handoff targets. After the ABNAHMEKRITERIUM "
                    f"comment, hand off to the repo coder: "
                    f"kanban_handoff(to=\"{coder}\"). Ignore any upstream "
                    f"\"Coder-Ziel: recon\" note — recon is not write-capable; "
                    f"the real build role is {coder!r}. The coder redoes the edit "
                    f"cleanly on wt/<id> and hands off to the reviewer, who "
                    f"completes the card."
                )
            if ws_kind is None and to_profile and to_profile.endswith("-coder"):
                row = conn.execute(
                    "SELECT tenant, repository FROM tasks WHERE id = ?", (tid,),
                ).fetchone()
                tenant = (row[0] if row else None) or ""
                repo = (row[1] if row and len(row) > 1 else None) or ""
                # A declared repository beats the tenant default for the
                # checkout: its repo-specific workdir has the remote the
                # profile's base_ref/push_target name; the tenant checkout may
                # not (e.g. hermes card on a voicera tenant).
                _prof = kb._repository_profiles().get(repo.strip().lower()) if repo else None
                workdir = (
                    (getattr(_prof, "workdir", "") if _prof else "")
                    or kb._tenant_workdir_map().get(tenant.strip().lower())
                )
                if workdir:
                    ws_kind = "worktree"
                    if ws_path is None:
                        ws_path = workdir
            ok = kb.handoff_task(
                conn, tid, to_profile,
                note=note, author=author,
                expected_run_id=_worker_run_id(tid),
                workspace_kind=ws_kind,
                workspace_path=ws_path,
            )
            if not ok:
                return tool_error(
                    f"could not hand off {tid} (unknown id or not in "
                    f"running/ready)"
                )
            landed = kb.get_task(conn, tid)
            return _ok(
                task_id=tid,
                status=landed.status if landed else "ready",
                assignee=to_profile,
            )
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_handoff: {e}")
    except Exception as e:
        logger.exception("kanban_handoff failed")
        return tool_error(f"kanban_handoff: {e}")


def _handle_heartbeat(args: dict, **kw) -> str:
    """Signal that the worker is still alive during a long operation.

    Extends the claim TTL via ``heartbeat_claim`` AND records a heartbeat
    event via ``heartbeat_worker``. Without the ``heartbeat_claim`` half,
    a diligent worker that loops this tool while a single tool call
    blocks the agent for >DEFAULT_CLAIM_TTL_SECONDS still gets reclaimed
    by ``release_stale_claims`` — which is exactly the trap that
    ``heartbeat_claim``'s docstring warns against.
    """
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    note = args.get("note")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            # Extend the claim TTL first. The dispatcher pins
            # HERMES_KANBAN_CLAIM_LOCK in the worker env at spawn time
            # (see _default_spawn in kanban_db.py); falling back to the
            # default _claimer_id() covers locally-driven workers that
            # never went through the dispatcher path.
            claim_lock = os.environ.get("HERMES_KANBAN_CLAIM_LOCK")
            kb.heartbeat_claim(conn, tid, claimer=claim_lock)

            ok = kb.heartbeat_worker(
                conn,
                tid,
                note=note,
                expected_run_id=_worker_run_id(tid),
            )
            if not ok:
                return tool_error(
                    f"could not heartbeat {tid} (unknown id or not running)"
                )
            return _ok(task_id=tid)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_heartbeat: {e}")
    except Exception as e:
        logger.exception("kanban_heartbeat failed")
        return tool_error(f"kanban_heartbeat: {e}")


def _handle_comment(args: dict, **kw) -> str:
    """Append a comment to a task's thread."""
    tid = args.get("task_id")
    if not tid:
        return tool_error(
            "task_id is required (use the current task id if that's what "
            "you mean — pulls from env but kept explicit here)"
        )
    body = args.get("body")
    if not body or not str(body).strip():
        return tool_error("body is required")
    body = redact_sensitive_text(str(body), force=True)
    # Author is intentionally derived from the worker's own runtime
    # identity, NOT from caller-supplied args. Comments are injected
    # into the next worker's system prompt by ``build_worker_context``
    # as ``**{author}** (timestamp): {body}`` — accepting an
    # ``args["author"]`` override let a worker forge a comment from
    # an authoritative-looking name like ``hermes-system`` and poison
    # the future-worker context with what reads as a system directive.
    # Cross-task commenting itself remains unrestricted (see #19713) —
    # comments are the deliberate handoff channel between tasks.
    author = os.environ.get("HERMES_PROFILE") or "worker"
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            cid = kb.add_comment(conn, tid, author=author, body=str(body))
            return _ok(task_id=tid, comment_id=cid)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_comment: {e}")
    except Exception as e:
        logger.exception("kanban_comment failed")
        return tool_error(f"kanban_comment: {e}")


def _handle_attach(args: dict, **kw) -> str:
    """Attach an inline (base64) file to a task.

    Mirrors the dashboard's upload endpoint for the agent surface: decode
    the payload, enforce the shared size cap, write it under the per-task
    attachments dir, and record the metadata row — all via
    ``kanban_db.store_attachment_bytes`` so the three surfaces stay in lockstep.
    """
    from hermes_cli import kanban_db as kb

    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    filename = args.get("filename")
    if not filename or not str(filename).strip():
        return tool_error("filename is required")
    content_b64 = args.get("content_base64")
    if not content_b64 or not str(content_b64).strip():
        return tool_error("content_base64 is required")
    import base64
    import binascii
    try:
        data = base64.b64decode(str(content_b64), validate=True)
    except (binascii.Error, ValueError) as e:
        return tool_error(f"content_base64 is not valid base64: {e}")
    content_type = args.get("content_type")
    board = args.get("board")
    try:
        _, conn = _connect(board=board)
        try:
            att_id = kb.store_attachment_bytes(
                conn,
                tid,
                str(filename),
                data,
                content_type=content_type,
                uploaded_by="agent",
                board=board,
            )
            return _ok(task_id=tid, attachment_id=att_id, size=len(data))
        finally:
            conn.close()
    except kb.AttachmentTooLarge as e:
        return tool_error(f"kanban_attach: {e}")
    except ValueError as e:
        return tool_error(f"kanban_attach: {e}")
    except Exception as e:
        logger.exception("kanban_attach failed")
        return tool_error(f"kanban_attach: {e}")


_MAX_ATTACH_URL_REDIRECTS = 5


def _download_url_with_cap(url: str, max_bytes: int) -> tuple[bytes, Optional[str]]:
    """Fetch ``url`` over http(s) with SSRF guarding, capped at ``max_bytes``.

    Every hop — the initial URL and each redirect target — is validated with
    ``tools.url_safety.is_safe_url`` before it is fetched, so a
    model-controlled URL (or a public host 302ing to one) cannot reach
    loopback, private/CGNAT ranges, or cloud metadata endpoints. Redirects
    are followed manually (``follow_redirects=False``) so each Location is
    re-checked, mirroring ``tools.skills_hub._guarded_http_get``.

    Returns ``(data, content_type)``. Raises ``ValueError`` for a non-http(s)
    scheme, an SSRF-blocked target, too many redirects, or a body that
    overruns the cap (the caller maps it to a clean tool error). Reads in
    chunks so an oversize response is rejected without buffering the whole
    thing.
    """
    from urllib.parse import urljoin, urlparse

    import httpx

    from tools.url_safety import is_safe_url

    current_url = url
    for _ in range(_MAX_ATTACH_URL_REDIRECTS + 1):
        scheme = (urlparse(current_url).scheme or "").lower()
        if scheme not in ("http", "https"):
            raise ValueError(
                f"unsupported URL scheme {scheme!r}; only http/https are allowed"
            )
        if not is_safe_url(current_url):
            raise ValueError(
                f"URL blocked by SSRF protection (private/internal address): {current_url}"
            )
        chunks: list[bytes] = []
        total = 0
        with httpx.stream(
            "GET",
            current_url,
            headers={"User-Agent": "hermes-kanban/attach"},
            timeout=30,
            follow_redirects=False,
        ) as resp:
            if resp.is_redirect:
                location = resp.headers.get("location")
                if not location:
                    raise ValueError(f"redirect without Location header from {current_url}")
                current_url = urljoin(current_url, location)
                continue
            resp.raise_for_status()
            content_type = (resp.headers.get("content-type") or "").split(";")[0].strip() or None
            for chunk in resp.iter_bytes(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(
                        f"attachment exceeds {max_bytes // (1024 * 1024)} MB limit"
                    )
                chunks.append(chunk)
        return b"".join(chunks), content_type
    raise ValueError(f"too many redirects fetching {url}")


def _handle_attach_url(args: dict, **kw) -> str:
    """Attach a file fetched server-side from a URL.

    The agent passes a URL; Hermes downloads it (with the shared size cap)
    and stores it as a real attachment. Useful when the agent has a link
    rather than the bytes. Only http/https URLs are accepted.
    """
    from hermes_cli import kanban_db as kb

    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    url = args.get("url")
    if not url or not str(url).strip():
        return tool_error("url is required")
    url = str(url).strip()
    filename = args.get("filename") or args.get("title")
    if not filename or not str(filename).strip():
        # Derive a name from the URL path's leaf component.
        from urllib.parse import unquote, urlparse
        leaf = unquote(urlparse(url).path.rsplit("/", 1)[-1]).strip()
        filename = leaf or "download"
    content_type = args.get("content_type")
    board = args.get("board")
    try:
        data, fetched_ct = _download_url_with_cap(url, kb.KANBAN_ATTACHMENT_MAX_BYTES)
    except ValueError as e:
        return tool_error(f"kanban_attach_url: {e}")
    except Exception as e:
        logger.exception("kanban_attach_url download failed")
        return tool_error(f"kanban_attach_url: failed to fetch {url}: {e}")
    try:
        _, conn = _connect(board=board)
        try:
            att_id = kb.store_attachment_bytes(
                conn,
                tid,
                str(filename),
                data,
                content_type=content_type or fetched_ct,
                uploaded_by="agent",
                board=board,
            )
            return _ok(task_id=tid, attachment_id=att_id, size=len(data))
        finally:
            conn.close()
    except kb.AttachmentTooLarge as e:
        return tool_error(f"kanban_attach_url: {e}")
    except ValueError as e:
        return tool_error(f"kanban_attach_url: {e}")
    except Exception as e:
        logger.exception("kanban_attach_url failed")
        return tool_error(f"kanban_attach_url: {e}")


def _handle_attachments(args: dict, **kw) -> str:
    """List a task's attachments (read-only; no ownership restriction)."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            if kb.get_task(conn, tid) is None:
                return tool_error(f"task {tid} not found")
            atts = kb.list_attachments(conn, tid)
            return json.dumps({
                "ok": True,
                "task_id": tid,
                "attachments": [
                    {
                        "id": a.id,
                        "filename": a.filename,
                        "content_type": a.content_type,
                        "size": a.size,
                        "uploaded_by": a.uploaded_by,
                        "stored_path": a.stored_path,
                        "created_at": a.created_at,
                    }
                    for a in atts
                ],
            })
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_attachments: {e}")
    except Exception as e:
        logger.exception("kanban_attachments failed")
        return tool_error(f"kanban_attachments: {e}")


def _handle_create(args: dict, **kw) -> str:
    """Create a child task. Orchestrator workers use this to fan out.

    ``parents`` can be a list of task ids; dependency-gated promotion
    works as usual.
    """
    title = args.get("title")
    if not title or not str(title).strip():
        return tool_error("title is required")
    assignee = args.get("assignee")
    if not assignee:
        return tool_error(
            "assignee is required — name the profile that should execute this "
            "task (the dispatcher will only spawn tasks with an assignee)"
        )
    body = args.get("body")
    parents = args.get("parents") or []
    tenant = args.get("tenant") or os.environ.get("HERMES_TENANT")
    merge_group = args.get("merge_group")
    # Stamp the originating session id when the agent loop runs under
    # ACP (which sets HERMES_SESSION_ID before invoking tools). NULL on
    # CLI / dashboard paths and on legacy hosts that don't set the env.
    session_id = args.get("session_id") or os.environ.get("HERMES_SESSION_ID")
    priority = args.get("priority")
    # Resolve workspace. If the caller passed one explicitly, honor it.
    # Otherwise, a dispatcher-spawned worker (HERMES_KANBAN_TASK set)
    # inherits its own running task's workspace, so a worker editing a
    # dir:/worktree project that spawns a follow-up child keeps the child
    # in that project instead of a throwaway scratch dir. Orchestrators
    # (kanban toolset, no HERMES_KANBAN_TASK) and CLI/dashboard callers
    # fall back to scratch as before. Explicit None path stays None.
    workspace_kind = args.get("workspace_kind")
    workspace_path = args.get("workspace_path")
    project_id = args.get("project") or args.get("project_id")
    repository = args.get("repository")
    _inherit_workspace = workspace_kind is None and workspace_path is None
    if workspace_kind is None:
        workspace_kind = "scratch"
    workspace_fallback_note = None
    if workspace_kind == "worktree" and workspace_path is None:
        # A worktree card without a path is doomed: the dispatcher fails
        # it deterministically at spawn time ("workspace_kind=worktree
        # but no workspace_path"). Board-level default_workdir (resolved
        # in create_task) can still fill the path; when the board has no
        # default either, degrade to scratch at create time so the card
        # stays runnable instead of spawn-failing twice and giving up.
        try:
            from hermes_cli.kanban_db import get_current_board, read_board_metadata
            _board_slug = args.get("board") or get_current_board()
            if not read_board_metadata(_board_slug).get("default_workdir"):
                workspace_kind = "scratch"
                workspace_fallback_note = (
                    "workspace_kind=worktree had no workspace_path and the "
                    "board has no default_workdir; created as scratch. Pass "
                    "workspace_path (e.g. the project repo root) to get a "
                    "real worktree."
                )
        except Exception:
            pass
    triage, bool_error = _parse_bool_arg(args, "triage")
    if bool_error:
        return tool_error(bool_error)
    idempotency_key = args.get("idempotency_key")
    max_runtime_seconds = args.get("max_runtime_seconds")
    initial_status = args.get("initial_status") or "running"
    skills = args.get("skills")
    if isinstance(skills, str):
        # Accept a single skill name as a string for convenience.
        skills = [skills]
    if skills is not None and not isinstance(skills, (list, tuple)):
        return tool_error(
            f"skills must be a list of skill names, got {type(skills).__name__}"
        )
    goal_mode, goal_bool_error = _parse_bool_arg(args, "goal_mode")
    if goal_bool_error:
        return tool_error(goal_bool_error)
    goal_max_turns = args.get("goal_max_turns")
    if isinstance(parents, str):
        parents = [parents]
    if not isinstance(parents, (list, tuple)):
        return tool_error(
            f"parents must be a list of task ids, got {type(parents).__name__}"
        )
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            # Inherit the spawning worker's own task workspace when the
            # caller didn't specify one (see resolution note above).
            if _inherit_workspace:
                _self_tid = os.environ.get("HERMES_KANBAN_TASK")
                if _self_tid:
                    _self_task = kb.get_task(conn, _self_tid)
                    if _self_task is not None and _self_task.workspace_kind:
                        workspace_kind = _self_task.workspace_kind
                        workspace_path = _self_task.workspace_path
                        # Keep follow-up children inside the same project so the
                        # whole subtree shares one repo + branch convention.
                        if project_id is None and _self_task.project_id:
                            project_id = _self_task.project_id
            new_tid = kb.create_task(
                conn,
                title=str(title).strip(),
                body=body,
                assignee=str(assignee),
                parents=tuple(parents),
                tenant=tenant,
                repository=repository,
                priority=int(priority) if priority is not None else 0,
                workspace_kind=str(workspace_kind),
                workspace_path=workspace_path,
                project_id=project_id,
                triage=triage,
                idempotency_key=idempotency_key,
                max_runtime_seconds=(
                    int(max_runtime_seconds)
                    if max_runtime_seconds is not None else None
                ),
                skills=skills,
                goal_mode=goal_mode,
                goal_max_turns=(
                    int(goal_max_turns) if goal_max_turns is not None else None
                ),
                child_role=str(args.get("child_role") or "work"),
                initial_status=str(initial_status),
                merge_group=merge_group,
                created_by=os.environ.get("HERMES_PROFILE") or "worker",
                session_id=session_id,
            )
            new_task = kb.get_task(conn, new_tid)
            subscribed = _maybe_auto_subscribe(conn, new_tid)
            _ok_kwargs = dict(
                task_id=new_tid,
                status=new_task.status if new_task else None,
                subscribed=subscribed,
            )
            if workspace_fallback_note:
                _ok_kwargs["workspace_note"] = workspace_fallback_note
            return _ok(**_ok_kwargs)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_create: {e}")
    except Exception as e:
        logger.exception("kanban_create failed")
        return tool_error(f"kanban_create: {e}")


def _maybe_auto_subscribe(conn: Any, task_id: str) -> bool:
    """Auto-subscribe the calling session to task completion / block events.

    Returns True if a subscription row was written, False otherwise (no
    session context, config gate disabled, or best-effort failure). The
    caller surfaces this in the ``subscribed`` field of the kanban_create
    response so an orchestrator can decide whether to fall back to an
    explicit ``kanban_notify-subscribe`` or to polling.

    Gated by ``kanban.auto_subscribe_on_create`` in config.yaml (default
    True). Disable to mirror pre-feature behaviour, e.g. when the
    originating user/chat opted out via the per-platform notification
    toggle (see ``hermes dashboard``).

    Subscription paths:

    - **Gateway** (telegram/discord/slack/etc): ``HERMES_SESSION_PLATFORM``
      and ``HERMES_SESSION_CHAT_ID`` are set in ContextVars by the
      messaging gateway before agent dispatch. The notification poller
      already keys off these, so we just register a row.

    - **TUI** (herm desktop / herm TUI): the platform/chat_id ContextVars
      are intentionally cleared (TUI is a single-channel local UI, not
      a multi-tenant chat surface), but the agent subprocess inherits
      ``HERMES_SESSION_KEY`` from the parent session. We subscribe with
      ``platform="tui"`` and ``chat_id=<key>``; the TUI notification
      poller (``tui_gateway/server.py``) reads ``kanban_notify_subs``
      for these rows and posts the completion message into the running
      session.

    - **CLI / cron / test / unattached**: no persistent delivery channel,
      no-op.

    Failure mode: any exception inside the function is logged at WARNING
    with the offending exception + diagnostic env vars and swallowed.
    We never want a notification bookkeeping failure to fail the
    kanban_create that the agent is mid-conversation about.
    """
    try:
        cfg = load_config()
        if not cfg_get(cfg, "kanban", "auto_subscribe_on_create", default=True):
            return False
    except Exception:
        # If config can't load we still default to True — this is the
        # user-friendly behaviour that mirrors the pre-gate implementation.
        pass

    platform = ""
    chat_id = ""
    try:
        from gateway.session_context import get_session_env
        platform = get_session_env("HERMES_SESSION_PLATFORM", "")
        chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "")
        if not platform or not chat_id:
            # TUI / desktop fallback: platform/chat_id ContextVars are
            # cleared for TUI sessions, but the parent process exports
            # HERMES_SESSION_KEY into the subprocess env. Treat that
            # as a "tui" subscription so the TUI notification poller
            # (tui_gateway/server.py) can pick it up.
            #
            # HERMES_SESSION_ID is intentionally NOT a fallback here:
            # it is set by ACP / the agent subprocess for telemetry
            # regardless of whether the parent is a TUI or a CLI, so
            # treating it as a notification target would auto-subscribe
            # every CLI invocation, which is exactly the over-eager
            # behaviour that got #19718 reverted upstream. The TUI
            # poller keys on HERMES_SESSION_KEY.
            session_key = (
                get_session_env("HERMES_SESSION_KEY", "")
                or os.environ.get("HERMES_SESSION_KEY", "")
            )
            if not session_key:
                return False  # CLI / cron / test — no persistent channel
            platform = "tui"
            chat_id = session_key
        thread_id = get_session_env("HERMES_SESSION_THREAD_ID", "") or None
        user_id = get_session_env("HERMES_SESSION_USER_ID", "") or None
        notifier_profile = (
            get_session_env("HERMES_SESSION_PROFILE", "")
            or os.environ.get("HERMES_PROFILE")
        )

        # Lazy-import to keep the module-level dependency light
        from hermes_cli import kanban_db as _kb
        _kb.add_notify_sub(
            conn, task_id=task_id,
            platform=platform, chat_id=chat_id,
            thread_id=thread_id, user_id=user_id,
            notifier_profile=notifier_profile,
        )
        return True
    except Exception as _exc:
        logger.warning(
            "_maybe_auto_subscribe failed: %r (platform=%r key_set=%r)",
            _exc, platform, bool(chat_id),
        )
        return False


def _handle_unblock(args: dict, **kw) -> str:
    """Transition a blocked task back to ready."""
    guard = _require_orchestrator_tool("kanban_unblock")
    if guard:
        return guard
    tid = args.get("task_id")
    if not tid:
        return tool_error("task_id is required")
    ownership_err = _enforce_worker_task_ownership(str(tid))
    if ownership_err:
        return ownership_err
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            ok = kb.unblock_task(conn, str(tid))
            if not ok:
                return tool_error(f"could not unblock {tid} (not blocked or unknown)")
            return _ok(task_id=str(tid), status="ready")
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_unblock: {e}")
    except Exception as e:
        logger.exception("kanban_unblock failed")
        return tool_error(f"kanban_unblock: {e}")


def _handle_link(args: dict, **kw) -> str:
    """Add a parent→child dependency edge after the fact."""
    parent_id = args.get("parent_id")
    child_id = args.get("child_id")
    if not parent_id or not child_id:
        return tool_error("both parent_id and child_id are required")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            kb.link_tasks(conn, parent_id=parent_id, child_id=child_id)
            return _ok(parent_id=parent_id, child_id=child_id)
        finally:
            conn.close()
    except ValueError as e:
        # Covers cycle + self-parent rejections
        return tool_error(f"kanban_link: {e}")
    except Exception as e:
        logger.exception("kanban_link failed")
        return tool_error(f"kanban_link: {e}")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

_DESC_TASK_ID_DEFAULT = (
    "Task id. If omitted, defaults to HERMES_KANBAN_TASK from the env "
    "(the task the dispatcher spawned you to work on)."
)

_DESC_BOARD = (
    "Kanban board slug to target. When omitted, the call resolves the "
    "active board the usual way: HERMES_KANBAN_DB env → "
    "HERMES_KANBAN_BOARD env → the 'current' symlink under the kanban "
    "home → 'default'. Pass an explicit slug only when the caller (e.g. "
    "a Telegram routing layer) needs to override the env-pinned active "
    "board for this one call."
)


def _board_schema_prop() -> dict[str, str]:
    """Schema fragment for the optional ``board`` parameter.

    Centralised so a future tweak to the description / validation hint
    only has to land in one place.
    """
    return {"type": "string", "description": _DESC_BOARD}

KANBAN_SHOW_SCHEMA = {
    "name": "kanban_show",
    "description": (
        "Read a task's full state — title, body, assignee, parent task "
        "handoffs, your prior attempts on this task if any, comments, "
        "and recent events. Use this to (re)orient yourself before "
        "starting work, especially on retries. The response includes a "
        "pre-formatted ``worker_context`` string suitable for inclusion "
        "verbatim in your reasoning."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_LIST_SCHEMA = {
    "name": "kanban_list",
    "description": (
        "List Kanban task summaries so an orchestrator profile can discover "
        "work to route. Supports the same core filters as the CLI: assignee, "
        "status, tenant, include_archived, and limit. Returns compact rows "
        "with ids, title, status, assignee, priority, parent/child ids, and "
        "counts. Bounded to 50 rows by default, 200 max, with truncation "
        "metadata. Also recomputes ready tasks before listing, matching the "
        "CLI. Orchestrator-only — dispatcher-spawned task workers never see "
        "this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "assignee": {
                "type": "string",
                "description": "Optional assignee/profile filter.",
            },
            "status": {
                "type": "string",
                "enum": [
                    "triage", "todo", "ready", "running",
                    "blocked", "done", "archived",
                ],
                "description": "Optional task status filter.",
            },
            "tenant": {
                "type": "string",
                "description": "Optional tenant/project namespace filter.",
            },
            "include_archived": {
                "type": "boolean",
                "description": "Include archived tasks. Defaults to false.",
            },
            "limit": {
                "type": "integer",
                "description": "Optional maximum rows to return (default 50, max 200).",
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_COMPLETE_SCHEMA = {
    "name": "kanban_complete",
    "description": (
        "Mark your current task done with a structured handoff for "
        "downstream workers and humans. Prefer ``summary`` for a "
        "human-readable 1-3 sentence description of what you did; put "
        "machine-readable facts in ``metadata`` (changed_files, "
        "tests_run, decisions, findings, etc). At least one of "
        "``summary`` or ``result`` is required. If you created new "
        "tasks via ``kanban_create`` during this run, list their ids "
        "in ``created_cards`` — the kernel verifies them so phantom "
        "references are caught before they leak into downstream "
        "automation. If you produced deliverable files (charts, PDFs, "
        "spreadsheets, generated images), list their absolute paths "
        "in ``artifacts`` — the gateway notifier will upload them as "
        "native attachments to the human who subscribed to the task, "
        "so the deliverable lands in their chat alongside the summary "
        "instead of being a path they have to fetch by hand."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "summary": {
                "type": "string",
                "description": (
                    "Human-readable handoff, 1-3 sentences. Appears in "
                    "Run History on the dashboard and in downstream "
                    "workers' context."
                ),
            },
            "metadata": {
                "type": "object",
                "description": (
                    "Free-form dict of structured facts about this "
                    "attempt — {\"changed_files\": [...], \"tests_run\": 12, "
                    "\"findings\": [...]}. Surfaced to downstream "
                    "workers alongside ``summary``."
                ),
            },
            "result": {
                "type": "string",
                "description": (
                    "Short result log line (legacy field, maps to "
                    "task.result). Use ``summary`` instead when "
                    "possible; this exists for compatibility with "
                    "callers that still set --result on the CLI."
                ),
            },
            "created_cards": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional structured manifest of task ids you "
                    "created via ``kanban_create`` during this run. "
                    "The kernel verifies each id exists and was "
                    "created by this worker's profile; any phantom "
                    "id blocks the completion with an error listing "
                    "what went wrong (auditable in the task's events). "
                    "Only list ids you got back from a successful "
                    "``kanban_create`` call — do not invent or "
                    "remember ids from prose. Omit the field if you "
                    "did not create any cards."
                ),
            },
            "artifacts": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of absolute paths to deliverable "
                    "files you produced during this run — generated "
                    "charts, PDFs, spreadsheets, images, archives. "
                    "Examples: [\"/tmp/q3-revenue.png\", "
                    "\"/tmp/report.pdf\"]. The gateway notifier "
                    "uploads each path as a native attachment to the "
                    "subscribed chat (images embed inline, everything "
                    "else uploads as a file) so the deliverable "
                    "lands with the completion notification. Skip "
                    "intermediate scratch files and references that "
                    "are not the deliverable. The path must exist "
                    "on disk at completion. Files inside a managed scratch "
                    "workspace are copied to durable task attachments before "
                    "cleanup; a missing declared scratch artifact keeps the "
                    "task in-flight so you can fix the path and retry."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_BLOCK_SCHEMA = {
    "name": "kanban_block",
    "description": (
        "Stop work on this task and route it according to WHY you're stuck. "
        "Set ``kind`` to say which: 'dependency' (waiting on another task — "
        "goes to todo and auto-resumes when that task finishes, no human "
        "needed), 'needs_input' (you need a human decision/answer), "
        "'capability' (a hard wall: no access, missing credentials, an action "
        "no agent can do), or 'transient' (a flaky failure that may clear). "
        "``reason`` is shown to the human on the board. If a task keeps "
        "getting unblocked and re-blocked for the same reason, it is "
        "auto-escalated to triage. Use for genuine blockers only — don't "
        "block on things you can resolve yourself."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "reason": {
                "type": "string",
                "description": (
                    "What you need answered or what stopped you, in one or "
                    "two sentences. Don't paste the whole conversation; the "
                    "human has the board and can ask follow-ups via comments."
                ),
            },
            "kind": {
                "type": "string",
                "enum": ["dependency", "needs_input", "capability", "transient", "review"],
                "description": (
                    "Why you're blocked. 'dependency' waits in todo and "
                    "resumes automatically; the others surface to a human. "
                    "Omit only if none apply."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": ["reason"],
    },
}


KANBAN_HANDOFF_SCHEMA = {
    "name": "kanban_handoff",
    "description": (
        "Pass THIS card forward to the next role in the pipeline without "
        "finishing it — use this instead of kanban_complete when your job is "
        "to prepare the card, not to close it. The card returns to 'ready' "
        "assigned to ``to`` and the dispatcher spawns that role on the SAME "
        "card, so the specifier's acceptance criterion, the coder's work and "
        "the reviewer's verdict all land in one card's history. Flow: "
        "orchestrator → 'specifier' → the repo coder (e.g. 'hermes-coder'); "
        "the coder then hands to the reviewer via kanban_block(kind='review'). "
        "Put the message for the next role (the specifier's "
        "'ABNAHMEKRITERIUM: …' line, or the coder brief) in ``note`` — it is "
        "stored as a comment signed by you. This is your FINAL action for the "
        "card this run; do not also call kanban_complete."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "to": {
                "type": "string",
                "description": (
                    "Profile that should take the card next, e.g. "
                    "'specifier' or the repo coder 'hermes-coder'."
                ),
            },
            "note": {
                "type": "string",
                "description": (
                    "Comment for the next role, signed by you. The specifier "
                    "puts its 'ABNAHMEKRITERIUM: <one verifiable sentence>' "
                    "here so it is recorded before any coder beitrag."
                ),
            },
            "workspace_kind": {
                "type": "string",
                "enum": ["scratch", "worktree"],
                "description": (
                    "Optional. Workspace the next role runs in. Usually omit: "
                    "a handoff to a repo coder auto-upgrades to 'worktree' "
                    "(so the coder's branch is recorded on the card) when the "
                    "tenant has a configured checkout. Set explicitly only to "
                    "override that default."
                ),
            },
            "workspace_path": {
                "type": "string",
                "description": (
                    "Optional absolute path to the repo checkout for a "
                    "'worktree' handoff. Omit to use the tenant's configured "
                    "checkout."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": ["to"],
    },
}

KANBAN_HEARTBEAT_SCHEMA = {
    "name": "kanban_heartbeat",
    "description": (
        "Signal that you're still alive during a long operation "
        "(training, encoding, large crawls). Call every few minutes so "
        "humans see liveness separately from PID checks. Pure side "
        "effect — no work changes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "note": {
                "type": "string",
                "description": (
                    "Optional short note describing current progress. "
                    "Shown in the event log."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_COMMENT_SCHEMA = {
    "name": "kanban_comment",
    "description": (
        "Append a comment to a task's thread. Use for durable notes "
        "that should outlive this run (questions for the next worker, "
        "partial findings, rationale). Ephemeral reasoning doesn't "
        "belong here — use your normal response instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": (
                    "Task id. Required (may be your own task or "
                    "another's — comment threads are per-task)."
                ),
            },
            "body": {
                "type": "string",
                "description": "Markdown-supported comment body.",
            },
            "board": _board_schema_prop(),
        },
        "required": ["task_id", "body"],
    },
}

KANBAN_ATTACH_SCHEMA = {
    "name": "kanban_attach",
    "description": (
        "Attach a file to a task by passing its bytes inline (base64). "
        "Use for genuine file artifacts the next worker or a human should "
        "be able to download — generated reports, images, exports. The "
        "file is stored as a real attachment (not a comment link) under "
        "the task's attachments dir, capped at 25 MB. Prefer "
        "kanban_attach_url when you only have a URL."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "filename": {
                "type": "string",
                "description": (
                    "File name to store it under (e.g. 'report.pdf'). "
                    "Directory components are stripped; only the leaf is kept."
                ),
            },
            "content_base64": {
                "type": "string",
                "description": "The file contents, base64-encoded. Max 25 MB decoded.",
            },
            "content_type": {
                "type": "string",
                "description": "Optional MIME type (e.g. 'application/pdf').",
            },
            "board": _board_schema_prop(),
        },
        "required": ["filename", "content_base64"],
    },
}

KANBAN_ATTACH_URL_SCHEMA = {
    "name": "kanban_attach_url",
    "description": (
        "Attach a file to a task by URL — Hermes downloads it server-side "
        "and stores it as a real attachment (capped at 25 MB). Use when "
        "you have a link rather than the bytes. Only http/https URLs are "
        "accepted."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "url": {
                "type": "string",
                "description": "http(s) URL to fetch and store.",
            },
            "filename": {
                "type": "string",
                "description": (
                    "Optional name to store it under. Defaults to the URL "
                    "path's leaf component."
                ),
            },
            "content_type": {
                "type": "string",
                "description": (
                    "Optional MIME type override. Defaults to the "
                    "Content-Type the server returns."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": ["url"],
    },
}

KANBAN_ATTACHMENTS_SCHEMA = {
    "name": "kanban_attachments",
    "description": (
        "List the files attached to a task: id, filename, content_type, "
        "size, who uploaded it, and the absolute on-disk path you can read."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_CREATE_SCHEMA = {
    "name": "kanban_create",
    "description": (
        "Create a new kanban task, optionally as a child of the current "
        "one (pass the current task id in ``parents``). Used by "
        "orchestrator workers to fan out — decompose work into child "
        "tasks with specific assignees, link them into a pipeline, "
        "then complete your own task. The dispatcher picks up the new "
        "tasks on its next tick and spawns the assigned profiles."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short task title (required).",
            },
            "assignee": {
                "type": "string",
                "description": (
                    "Profile name that should execute this task "
                    "(e.g. 'researcher-a', 'reviewer', 'writer'). "
                    "Required — tasks without an assignee are never "
                    "dispatched."
                ),
            },
            "body": {
                "type": "string",
                "description": (
                    "Opening post: full spec, acceptance criteria, "
                    "links. The assigned worker reads this as part of "
                    "its context."
                ),
            },
            "parents": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Parent task ids. The new task stays in 'todo' "
                    "until every parent reaches 'done'; then it "
                    "auto-promotes to 'ready'. Typical fan-in: list "
                    "all the researcher task ids when creating a "
                    "synthesizer task."
                ),
            },
            "tenant": {
                "type": "string",
                "description": (
                    "Optional namespace for multi-project isolation. "
                    "Defaults to HERMES_TENANT env if set."
                ),
            },
            "repository": {
                "type": "string",
                "description": (
                    "Repository profile or signature (for example 'hermes' or "
                    "'hermes-agent'). Stored structurally and inherited by "
                    "decomposed children."
                ),
            },
            "priority": {
                "type": "integer",
                "description": (
                    "Dispatcher tiebreaker. Higher = picked sooner "
                    "when multiple ready tasks share an assignee."
                ),
            },
            "workspace_kind": {
                "type": "string",
                "enum": ["scratch", "dir", "worktree"],
                "description": (
                    "Workspace flavor: 'scratch' (fresh tmp dir, "
                    "default), 'dir' (shared directory, requires "
                    "absolute workspace_path), 'worktree' (git worktree)."
                ),
            },
            "workspace_path": {
                "type": "string",
                "description": (
                    "Absolute path for 'dir' or 'worktree' workspace. "
                    "Relative paths are rejected at dispatch."
                ),
            },
            "project": {
                "type": "string",
                "description": (
                    "Optional project id or slug to link the task to. When "
                    "set, the task becomes a git worktree under the project's "
                    "primary repo with a deterministic branch (project slug + "
                    "task id), instead of a random branch."
                ),
            },
            "merge_group": {
                "type": "string",
                "description": (
                    "Optional group tag. When multiple tasks share the same "
                    "merge_group value, the merger treats them as a unit: "
                    "all member branches are merged into a single commit "
                    "on the repository profile's configured merge target. The "
                    "merger card for the group stays in todo until all member "
                    "parent/child links)."
                ),
            },
            "triage": {
                "type": "boolean",
                "description": (
                    "If true, task lands in 'triage' instead of 'todo' "
                    "— a specifier profile is expected to flesh out "
                    "the body before work starts."
                ),
            },
            "idempotency_key": {
                "type": "string",
                "description": (
                    "If a non-archived task with this key already "
                    "exists, return that task's id instead of creating "
                    "a duplicate. Useful for retry-safe automation."
                ),
            },
            "max_runtime_seconds": {
                "type": "integer",
                "description": (
                    "Per-task runtime cap. When exceeded, the "
                    "dispatcher SIGTERMs the worker and re-queues the "
                    "task with outcome='timed_out'."
                ),
            },
            "initial_status": {
                "type": "string",
                "enum": ["running", "blocked"],
                "description": (
                    "Initial card status. Use 'blocked' for tasks that "
                    "require immediate human ops (R3 gate) to skip the "
                    "brief running-to-blocked transition. Defaults to "
                    "'running', which preserves the usual dispatch path."
                ),
            },
            "child_role": {
                "type": "string",
                "enum": ["work", "human_check"],
                "description": "Family role; human_check does not delay family completion.",
            },
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Skill names to force-load into the dispatched "
                    "worker. The kanban lifecycle is already injected "
                    "automatically; use this to pin a task to a specialist "
                    "context — e.g. ['translation'] for a translation "
                    "task, ['github-code-review'] for a reviewer task. "
                    "The names must match skills installed on the "
                    "assignee's profile."
                ),
            },
            "goal_mode": {
                "type": "boolean",
                "description": (
                    "Run the dispatched worker in a goal loop. When true, "
                    "after each turn an auxiliary judge checks the worker's "
                    "response against this card's title/body; if the work "
                    "isn't done and budget remains, the worker keeps going "
                    "in the same session until the judge agrees it's "
                    "complete (or the goal-turn budget is exhausted, which "
                    "blocks the task for human review). Use this for "
                    "open-ended cards where one shot rarely finishes the "
                    "work. Defaults to false (classic single-shot worker)."
                ),
            },
            "goal_max_turns": {
                "type": "integer",
                "description": (
                    "Turn budget for goal_mode workers. Caps how many "
                    "continuation turns the worker may take before the task "
                    "is blocked for review. Ignored unless goal_mode is "
                    "true. Defaults to the goal-engine default (20)."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": ["title", "assignee"],
    },
}

KANBAN_UNBLOCK_SCHEMA = {
    "name": "kanban_unblock",
    "description": (
        "Move a blocked Kanban task back to ready. Orchestrator-only — only "
        "profiles with the kanban toolset can unblock routed work; "
        "dispatcher-spawned task workers never see this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Blocked task id to return to ready.",
            },
            "board": _board_schema_prop(),
        },
        "required": ["task_id"],
    },
}

KANBAN_LINK_SCHEMA = {
    "name": "kanban_link",
    "description": (
        "Add a parent→child dependency edge after both tasks already "
        "exist. The child won't promote to 'ready' until all parents "
        "are 'done'. Cycles and self-links are rejected."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "parent_id": {"type": "string", "description": "Parent task id."},
            "child_id":  {"type": "string", "description": "Child task id."},
            "board": _board_schema_prop(),
        },
        "required": ["parent_id", "child_id"],
    },
}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="kanban_show",
    toolset="kanban",
    schema=KANBAN_SHOW_SCHEMA,
    handler=_handle_show,
    check_fn=_check_kanban_mode,
    emoji="📋",
)

registry.register(
    name="kanban_list",
    toolset="kanban",
    schema=KANBAN_LIST_SCHEMA,
    handler=_handle_list,
    check_fn=_check_kanban_board_read_mode,
    emoji="📋",
)

registry.register(
    name="kanban_complete",
    toolset="kanban",
    schema=KANBAN_COMPLETE_SCHEMA,
    handler=_handle_complete,
    check_fn=_check_kanban_mode,
    emoji="✔",
)

registry.register(
    name="kanban_block",
    toolset="kanban",
    schema=KANBAN_BLOCK_SCHEMA,
    handler=_handle_block,
    check_fn=_check_kanban_mode,
    emoji="⏸",
)

registry.register(
    name="kanban_handoff",
    toolset="kanban",
    schema=KANBAN_HANDOFF_SCHEMA,
    handler=_handle_handoff,
    check_fn=_check_kanban_mode,
    emoji="➡",
)

registry.register(
    name="kanban_heartbeat",
    toolset="kanban",
    schema=KANBAN_HEARTBEAT_SCHEMA,
    handler=_handle_heartbeat,
    check_fn=_check_kanban_mode,
    emoji="💓",
)

registry.register(
    name="kanban_comment",
    toolset="kanban",
    schema=KANBAN_COMMENT_SCHEMA,
    handler=_handle_comment,
    check_fn=_check_kanban_mode,
    emoji="💬",
)

registry.register(
    name="kanban_attach",
    toolset="kanban",
    schema=KANBAN_ATTACH_SCHEMA,
    handler=_handle_attach,
    check_fn=_check_kanban_mode,
    emoji="📎",
)

registry.register(
    name="kanban_attach_url",
    toolset="kanban",
    schema=KANBAN_ATTACH_URL_SCHEMA,
    handler=_handle_attach_url,
    check_fn=_check_kanban_mode,
    emoji="📎",
)

registry.register(
    name="kanban_attachments",
    toolset="kanban",
    schema=KANBAN_ATTACHMENTS_SCHEMA,
    handler=_handle_attachments,
    check_fn=_check_kanban_mode,
    emoji="📎",
)

registry.register(
    name="kanban_create",
    toolset="kanban",
    schema=KANBAN_CREATE_SCHEMA,
    handler=_handle_create,
    check_fn=_check_kanban_mode,
    emoji="➕",
)

registry.register(
    name="kanban_unblock",
    toolset="kanban",
    schema=KANBAN_UNBLOCK_SCHEMA,
    handler=_handle_unblock,
    check_fn=_check_kanban_orchestrator_mode,
    emoji="▶",
)

registry.register(
    name="kanban_link",
    toolset="kanban",
    schema=KANBAN_LINK_SCHEMA,
    handler=_handle_link,
    check_fn=_check_kanban_mode,
    emoji="🔗",
)
