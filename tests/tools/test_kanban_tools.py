"""Tests for the Kanban tool surface (tools/kanban_tools.py).

Verifies:
  - Tools are gated on HERMES_KANBAN_TASK: a normal chat session sees
    zero kanban tools in its schema; a worker session sees the kanban set.
  - Each handler's happy path.
  - Error paths (missing required args, bad metadata type, etc).
"""
from __future__ import annotations

import json
import os

import pytest


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

def test_kanban_tools_hidden_without_env_var(monkeypatch, tmp_path):
    """Normal `hermes chat` sessions (no HERMES_KANBAN_TASK) must have
    zero kanban_* tools in their schema."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import tools.kanban_tools  # ensure registered
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {s["function"].get("name") for s in schema if "function" in s}
    kanban = {n for n in names if n and n.startswith("kanban_")}
    assert kanban == set(), (
        f"kanban tools leaked into normal chat schema: {kanban}"
    )


def test_kanban_tools_visible_with_env_var(monkeypatch, tmp_path):
    """Worker sessions get task lifecycle tools, not board-routing tools."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fake")
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import tools.kanban_tools  # ensure registered
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {s["function"].get("name") for s in schema if "function" in s}
    kanban = {n for n in names if n and n.startswith("kanban_")}
    expected = {
        "kanban_show", "kanban_complete", "kanban_block", "kanban_heartbeat",
        "kanban_comment", "kanban_create", "kanban_link",
        "kanban_attach", "kanban_attach_url", "kanban_attachments",
    }
    assert kanban == expected, f"expected {expected}, got {kanban}"


def test_kanban_worker_env_overrides_profile_toolset_filter(monkeypatch, tmp_path):
    """Dispatcher-spawned workers must get lifecycle tools even when the
    assignee profile restricts enabled toolsets and does not list kanban.
    """
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fake")
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import tools.kanban_tools  # ensure registered
    from model_tools import _clear_tool_defs_cache, get_tool_definitions
    from tools.registry import invalidate_check_fn_cache

    invalidate_check_fn_cache()
    _clear_tool_defs_cache()
    schema = get_tool_definitions(
        enabled_toolsets=["terminal"],
        quiet_mode=True,
    )
    names = {s["function"].get("name") for s in schema if "function" in s}
    assert "kanban_show" in names
    assert "kanban_complete" in names
    assert "kanban_block" in names
    assert "kanban_list" not in names


def test_worker_with_kanban_toolset_keeps_read_only_board_discovery(monkeypatch, tmp_path):
    """Task scope hides mutating board routing, but keeps kanban_list.

    A dispatcher-spawned worker whose profile explicitly opts into the
    kanban toolset is the orchestrator surface (orchestrators are
    themselves dispatched as tasks). It needs read-only board discovery
    (kanban_list) for the pre-coder file-conflict check, but must never
    see kanban_unblock — unblocking is for the out-of-task surface.
    """
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fake")
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("toolsets:\n  - kanban\n")
    monkeypatch.setenv("HERMES_HOME", str(home))

    import tools.kanban_tools  # ensure registered
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {s["function"].get("name") for s in schema if "function" in s}
    kanban = {n for n in names if n and n.startswith("kanban_")}
    assert "kanban_list" in kanban, (
        f"kanban_list missing from task-scoped orchestrator schema: {kanban}"
    )
    assert "kanban_unblock" not in kanban, (
        "kanban_unblock leaked into task-scoped worker schema"
    )


def test_kanban_tools_visible_with_toolset_config(monkeypatch, tmp_path):
    """Orchestrator profiles with toolsets: [kanban] see all kanban tools."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("toolsets:\n  - kanban\n")
    monkeypatch.setenv("HERMES_HOME", str(home))

    import tools.kanban_tools  # ensure registered
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {s["function"].get("name") for s in schema if "function" in s}
    kanban = {n for n in names if n and n.startswith("kanban_")}
    expected = {
        "kanban_list",
        "kanban_show", "kanban_complete", "kanban_block", "kanban_heartbeat",
        "kanban_comment", "kanban_create", "kanban_link",
        "kanban_unblock",
        "kanban_attach", "kanban_attach_url", "kanban_attachments",
    }
    assert kanban == expected, f"expected {expected}, got {kanban}"


# ---------------------------------------------------------------------------
# Handler happy paths
# ---------------------------------------------------------------------------

@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    """Simulate being a worker: HERMES_HOME isolated, HERMES_KANBAN_TASK set
    after we've created the task."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from tests.support.kanban_routing import seed_canonical
    kb._INITIALIZED_PATHS.clear()
    seed_canonical(home)
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="worker-test", assignee="test-worker")
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    return tid


def test_show_defaults_to_env_task_id(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_show({})
    d = json.loads(out)
    assert "task" in d
    assert d["task"]["id"] == worker_env
    assert d["task"]["status"] == "running"
    assert "worker_context" in d
    assert "runs" in d


def test_show_explicit_task_id(worker_env):
    """Peek at a different task than the one in env."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="other task", assignee="peer")
    finally:
        conn.close()
    from tools import kanban_tools as kt
    out = kt._handle_show({"task_id": other})
    d = json.loads(out)
    assert d["task"]["id"] == other


def test_list_filters_tasks(monkeypatch, worker_env):
    """kanban_list gives orchestrators filtered board discovery."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="alpha", assignee="factory", priority=5)
        b = kb.create_task(conn, title="beta", assignee="reviewer")
        c = kb.create_task(conn, title="gamma", assignee="factory", tenant="other")
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_list({"assignee": "factory", "status": "ready", "limit": 10})
    d = json.loads(out)
    ids = [t["id"] for t in d["tasks"]]
    assert ids == [a, c]
    assert d["count"] == 2
    assert d["tasks"][0]["title"] == "alpha"
    assert d["tasks"][0]["parent_count"] == 0
    assert b not in ids

    tenant_out = kt._handle_list({
        "assignee": "factory",
        "status": "ready",
        "tenant": "other",
    })
    tenant_ids = [t["id"] for t in json.loads(tenant_out)["tasks"]]
    assert tenant_ids == [c]


def test_list_rejects_invalid_status(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from tools import kanban_tools as kt
    out = kt._handle_list({"status": "not-a-state"})
    assert "status must be one of" in json.loads(out).get("error", "")


def test_list_rejects_bad_limit(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from tools import kanban_tools as kt
    assert json.loads(kt._handle_list({"limit": "nope"})).get("error")
    assert json.loads(kt._handle_list({"limit": 0})).get("error")


def test_list_parses_include_archived_string_false(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        live = kb.create_task(conn, title="live task", assignee="factory")
        archived = kb.create_task(conn, title="archived task", assignee="factory")
        assert kb.archive_task(conn, archived)
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_list({
        "assignee": "factory",
        "include_archived": "false",
    })
    ids = [t["id"] for t in json.loads(out)["tasks"]]
    assert live in ids
    assert archived not in ids


def test_list_parses_include_archived_string_true(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        live = kb.create_task(conn, title="live task", assignee="factory")
        archived = kb.create_task(conn, title="archived task", assignee="factory")
        assert kb.archive_task(conn, archived)
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_list({
        "assignee": "factory",
        "include_archived": "true",
    })
    ids = [t["id"] for t in json.loads(out)["tasks"]]
    assert live in ids
    assert archived in ids


def test_list_rejects_bad_include_archived(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from tools import kanban_tools as kt
    out = kt._handle_list({"include_archived": "sometimes"})
    assert "include_archived must be" in json.loads(out).get("error", "")


def test_complete_happy_path(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_complete({
        "summary": "got the thing done",
        "metadata": {"files": 2},
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["task_id"] == worker_env
    # Verify via kernel
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        run = kb.latest_run(conn, worker_env)
        assert run.outcome == "completed"
        assert run.summary == "got the thing done"
        assert run.metadata == {"files": 2}
    finally:
        conn.close()


def test_complete_rejected_for_specifier_profile(monkeypatch, worker_env):
    """A specifier must hand off, never complete — only the reviewer finishes
    a work card. Guards against the live failure where a specifier called
    kanban_complete, froze the card at `done` with no coder/branch/reviewer,
    and broke the specifier→coder→reviewer chain (beobachter F01/F15/F16)."""
    monkeypatch.setenv("HERMES_PROFILE", "specifier")
    from tools import kanban_tools as kt
    out = kt._handle_complete({"summary": "spec looks fine"})
    d = json.loads(out)
    assert "error" in d
    assert "must not complete" in d["error"]
    assert "kanban_handoff" in d["error"]
    # The card must stay in-flight (still running), not slip to done.
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        task = kb.get_task(conn, worker_env)
        assert task.status == "running"
    finally:
        conn.close()


def test_complete_allowed_for_non_specifier_profile(monkeypatch, worker_env):
    """The specifier guard must not leak to other roles: a coder/reviewer
    worker completes its card normally."""
    monkeypatch.setenv("HERMES_PROFILE", "voicera-reviewer")
    from tools import kanban_tools as kt
    out = kt._handle_complete({"summary": "review green"})
    d = json.loads(out)
    assert d["ok"] is True
    assert d["task_id"] == worker_env


def test_complete_rejected_for_orchestrator_repo_work_card(monkeypatch, worker_env):
    """The orchestrator coordinates a repo work card, it never finishes one:
    only the reviewer completes a work card, at the end of
    specifier→coder→reviewer. A card with a repository but no branch (no coder
    pushed anything) and no work children (never decomposed) was neither built
    nor reviewed. Guards against the live failure where the orchestrator drove
    probe card t_46a39038 (repository voicera) straight to `done` — branch=None,
    no coder, no reviewer — surfacing as beobachter F21 'done ohne vollständige
    Abnahme'."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="CLAUDE.md gdpr-delete-Zeile entschaerfen",
            assignee="orchestrator", repository="voicera",
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    from tools import kanban_tools as kt
    out = kt._handle_complete({"summary": "looks done to me"})
    d = json.loads(out)
    assert "error" in d
    assert "must not complete" in d["error"]
    assert "kanban_handoff" in d["error"]
    # The card must stay in-flight (still running), not slip to done.
    conn = kb.connect()
    try:
        task = kb.get_task(conn, tid)
        assert task.status == "running"
    finally:
        conn.close()


def test_complete_allowed_for_orchestrator_decomposition_root(monkeypatch, worker_env):
    """The orchestrator guard must not block a genuine decomposition root: a
    repo card that carries a work child ran the pipeline through that child, so
    the orchestrator may close it out."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        root = kb.create_task(
            conn, title="voicera cleanup initiative",
            assignee="orchestrator", repository="voicera",
        )
        kb.create_task(
            conn, title="child work card", assignee="voicera-coder",
            repository="voicera", parent_task_id=root,
        )
        kb.claim_task(conn, root)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", root)
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    from tools import kanban_tools as kt
    out = kt._handle_complete({"summary": "children shipped and reviewed"})
    d = json.loads(out)
    assert d["ok"] is True
    assert d["task_id"] == root


def test_complete_allowed_for_orchestrator_non_repo_card(monkeypatch, worker_env):
    """The orchestrator guard is scoped to repo work cards: a pure
    coordination card (no repository) completes normally."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="coordinate the sweep", assignee="orchestrator",
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    from tools import kanban_tools as kt
    out = kt._handle_complete({"summary": "routing done"})
    d = json.loads(out)
    assert d["ok"] is True
    assert d["task_id"] == tid


def test_complete_rejected_for_zielentscheidung_card(monkeypatch, worker_env):
    """F39: a Zielentscheidung is the OWNER's call — the factory prepares it and
    escalates with a Zielfrage (blocked), it never self-decides it (done). Live
    probe Karte 44 ('Tier-System: Ballast aufloesen (VEREINFACHUNGS-FRAGE)',
    erwartung=blocked_zielfrage, branch=None): the voicera-reviewer completed it
    `done` with its own recommendation ('vollständig, aber gestuft entfernen') →
    beobachter rot-zielentscheid-selbst ('Fabrik hat Zielentscheidung selbst
    getroffen, done statt blocked'). The gate rejects the completion; the reviewer
    must block with the prepared Zielfrage instead."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="Tier-System: Ballast aufloesen (VEREINFACHUNGS-FRAGE)",
            assignee="voicera-reviewer", repository="voicera",
            body=("Das Tier-System traegt Komplexitaet, die niemand nutzt. "
                  "Entweder ganz entfernen oder auf zwei Stufen reduzieren. "
                  "Bitte die Folgen beider Varianten fuer Code und Daten "
                  "darstellen."),
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "voicera-reviewer")
    from tools import kanban_tools as kt
    out = kt._handle_complete({
        "summary": "Review grün — Empfehlung: vollständig, aber gestuft entfernen."
    })
    d = json.loads(out)
    assert "error" in d
    assert "Zielentscheidung" in d["error"]
    assert "kanban_block" in d["error"]
    # The card must stay in-flight (still running), not slip to done.
    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


def test_complete_rejected_for_zielentscheidung_by_category(monkeypatch, worker_env):
    """F39: the DB escalation category 'entscheidung' is honoured too, so a
    decision card the dispatcher DID tag is caught even if its wording is plain.
    (The content signal stays the load-bearing path — the live card carried
    category=None — but the structural signal is a free extra.)"""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="Produktweiche festlegen",
            assignee="voicera-reviewer", repository="voicera",
            body="Bitte den weiteren Weg klären.",
        )
        conn.execute(
            "UPDATE tasks SET category = 'entscheidung' WHERE id = ?", (tid,)
        )
        conn.commit()
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "voicera-reviewer")
    from tools import kanban_tools as kt
    out = kt._handle_complete({"summary": "Entschieden: Variante A."})
    d = json.loads(out)
    assert "error" in d
    assert "Zielentscheidung" in d["error"]
    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


def test_complete_allowed_for_non_decision_no_branch_card(monkeypatch, worker_env):
    """F39 must not leak to an ordinary no-branch analysis/recon card: a recon
    card ('Recon: leereTexte usage') carries none of the Zielentscheidung signals
    (no '(…-FRAGE)' tag, no 'Varianten darstellen', no 'Geschäftsentscheidung',
    no 'zwei Wege/Quellen') and no category, so it completes normally. Guards the
    gate's precision — the done path of every build/cleanup/recon card is
    untouched."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="Recon: leereTexte usage in voicera-os",
            assignee="recon", repository="voicera",
            body="grep nach Aufrufen der Funktion und Ergebnis berichten.",
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "recon")
    from tools import kanban_tools as kt
    out = kt._handle_complete({"summary": "0 Aufrufe gefunden; nur die Definition."})
    d = json.loads(out)
    assert d["ok"] is True
    assert d["task_id"] == tid


def test_block_rejected_for_orchestrator_repo_work_card(monkeypatch, worker_env):
    """F22, sibling of the F21 complete gate: the orchestrator must not escalate
    a fresh repo work card into the human blocked bucket. A card with a
    repository but no branch (no coder pushed) and no work children (never
    decomposed) was never built — a kanban_block(kind='needs_input') on it is a
    coordination decision (e.g. a perceived duplicate), not a Zielfrage. Guards
    against live probe card t_e8b0ae64 (repository voicera), which the
    orchestrator blocked with a 'DUPLICATE TASK DETECTED' note carrying no
    question → beobachter 'rot-technische-rueckfrage'."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="CLAUDE.md gdpr-delete-Zeile entschaerfen",
            assignee="orchestrator", repository="voicera",
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    from tools import kanban_tools as kt
    out = kt._handle_block({"reason": "DUPLICATE TASK DETECTED", "kind": "needs_input"})
    d = json.loads(out)
    assert "error" in d
    assert "must not block a repo work card" in d["error"]
    assert "kanban_handoff" in d["error"]
    # The card must stay in-flight (still running), not slip to blocked.
    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


def test_block_rejected_for_orchestrator_untyped_repo_work_card(monkeypatch, worker_env):
    """The gate covers the whole human bucket, including an un-typed block
    (kind=None, a 'generic human blocker') — so the orchestrator cannot drop
    the kind to escape the needs_input gate."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="entschaerfe die gdpr-Zeile",
            assignee="orchestrator", repository="voicera",
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    from tools import kanban_tools as kt
    out = kt._handle_block({"reason": "this is a duplicate, superseded"})
    d = json.loads(out)
    assert "error" in d
    assert "must not block a repo work card" in d["error"]
    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


def test_block_allowed_for_orchestrator_decomposition_root(monkeypatch, worker_env):
    """The block gate must not trap a genuine decomposition root: a repo card
    that carries a work child ran the pipeline through that child, so the
    orchestrator may still block it on a real external blocker."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        root = kb.create_task(
            conn, title="voicera cleanup initiative",
            assignee="orchestrator", repository="voicera",
        )
        kb.create_task(
            conn, title="child work card", assignee="voicera-coder",
            repository="voicera", parent_task_id=root,
        )
        kb.claim_task(conn, root)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", root)
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    from tools import kanban_tools as kt
    out = kt._handle_block({"reason": "need a real decision", "kind": "needs_input"})
    d = json.loads(out)
    assert d.get("ok") is True


def test_block_allowed_for_orchestrator_dependency_kind(monkeypatch, worker_env):
    """`dependency` is machine routing (→ todo), not a human escalation — the
    SOUL's file-conflict block relies on it. It stays ungated even on a fresh
    repo work card."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="touches overlapping files",
            assignee="orchestrator", repository="voicera",
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    from tools import kanban_tools as kt
    out = kt._handle_block({"reason": "file-conflict with running t_x", "kind": "dependency"})
    d = json.loads(out)
    assert d.get("ok") is True
    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "todo"
    finally:
        conn.close()


def test_block_allowed_for_orchestrator_non_repo_card(monkeypatch, worker_env):
    """The block gate is scoped to repo work cards: a pure coordination card
    (no repository) still blocks normally."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="coordinate the sweep", assignee="orchestrator",
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    from tools import kanban_tools as kt
    out = kt._handle_block({"reason": "need Till's call", "kind": "needs_input"})
    d = json.loads(out)
    assert d.get("ok") is True


def test_block_allowed_for_non_orchestrator_repo_work_card(monkeypatch, worker_env):
    """The gate is scoped to the orchestrator profile: a coder/reviewer on the
    same card shape keeps its own block path (it may hit a genuine wall)."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="coder hit a wall",
            assignee="voicera-coder", repository="voicera",
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "voicera-coder")
    from tools import kanban_tools as kt
    out = kt._handle_block({"reason": "need a decision from Till", "kind": "needs_input"})
    d = json.loads(out)
    assert d.get("ok") is True


def test_block_rejected_for_specifier_without_question(monkeypatch, worker_env):
    """F23: the specifier may block only for a genuine Zielfrage, which its SOUL
    requires it phrase AS A QUESTION. A human-escalation block whose reason (and
    comments) carry no '?' is a scope/recon ambiguity the fabric must resolve
    itself — not a decision for Till. Guards against live probe card t_b4335c9c
    ('CLAUDE.md gdpr-delete-Zeile entschaerfen'), which the specifier blocked
    with a 'which endpoint/line is meant' clarification and no question →
    beobachter 'rot-technische-rueckfrage'."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="CLAUDE.md gdpr-delete-Zeile entschaerfen",
            assignee="specifier", repository="voicera",
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "specifier")
    from tools import kanban_tools as kt
    out = kt._handle_block({
        "reason": "Blockiere fuer Klaerung, welche Zeile gemeint ist.",
        "kind": "needs_input",
    })
    d = json.loads(out)
    assert "error" in d
    assert "genuine Zielfrage" in d["error"]
    assert "kanban_handoff" in d["error"]
    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


def test_block_rejected_for_specifier_untyped_without_question(monkeypatch, worker_env):
    """The gate covers the whole human bucket, incl. an un-typed block
    (kind=None), so the specifier cannot drop the kind to escape."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="entschaerfe die gdpr-Zeile", assignee="specifier",
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "specifier")
    from tools import kanban_tools as kt
    out = kt._handle_block({"reason": "Anker nicht gefunden, breche ab."})
    d = json.loads(out)
    assert "error" in d
    assert "genuine Zielfrage" in d["error"]
    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


def test_block_rejected_for_specifier_question_only_in_reason(monkeypatch, worker_env):
    """F24: the Beobachter's _letzte_frage reads the Zielfrage ONLY from task
    COMMENTS, never from the block reason. A question that lives only in the
    block reason is invisible to the observer → frage="" → rot. The gate must
    reject it even though the reason carries a '?'. This is the exact signature
    of live probe card t_664772aa, which the specifier blocked needs_input with
    the question only in the reason ('…apply changes from worktree or re-edit
    from scratch?') → beobachter 'rot-technische-rueckfrage'."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="CLAUDE.md gdpr-delete-Zeile entschaerfen",
            assignee="specifier", repository="voicera",
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "specifier")
    from tools import kanban_tools as kt
    out = kt._handle_block({
        "reason": "Verification complete: main CLAUDE.md missing both safety "
                  "constraints. Old worktree contains correct version but "
                  "uncommitted. Parent agent needs to decide: apply changes "
                  "from worktree or re-edit from scratch?",
        "kind": "needs_input",
    })
    d = json.loads(out)
    assert "error" in d
    assert "genuine Zielfrage" in d["error"]
    assert "COMMENT" in d["error"]
    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


def test_block_rejected_for_specifier_technical_question_comment(monkeypatch, worker_env):
    """F24: the gate mirrors the Beobachter's TECH_REGEX vorfilter — a question
    carrying a technical term (which forces the observer's zielfrage=False and
    cannot be overridden) can never be green, so it is rejected even when posted
    as a proper comment."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="Loeschweg fuer Nutzerdaten", assignee="specifier",
        )
        kb.add_comment(
            conn, tid, author="specifier",
            body="brauche Klaerung: welche version der library soll ich nehmen?",
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "specifier")
    from tools import kanban_tools as kt
    out = kt._handle_block({"reason": "siehe Frage oben", "kind": "needs_input"})
    d = json.loads(out)
    assert "error" in d
    assert "genuine Zielfrage" in d["error"]
    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


def test_block_allowed_for_specifier_question_in_prior_comment(monkeypatch, worker_env):
    """A specifier block whose genuine business Zielfrage is posted AS A COMMENT
    (where the beobachter's _letzte_frage reads it) and carries no technical
    term passes the gate — the beobachter's richter then judges whether it is a
    legitimate business Zielfrage."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="Sichtbarkeit der Domain", assignee="specifier",
        )
        kb.add_comment(
            conn, tid, author="specifier",
            body="brauche Klaerung: soll die Domain oeffentlich sein?",
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "specifier")
    from tools import kanban_tools as kt
    out = kt._handle_block({"reason": "siehe Frage oben", "kind": "needs_input"})
    d = json.loads(out)
    assert d.get("ok") is True


def test_block_rejected_for_specifier_abnahmekriterium_present_no_question(monkeypatch, worker_env):
    """F28 (2nd occurrence): a specifier that has ALREADY signed an
    ABNAHMEKRITERIUM: has made the card baureif — the only valid next move is a
    coder handoff, never a human block. Live probe card t_82585a19 ('Hermes aus
    dem Frontend verbannen', klein, erwartung=done): the specifier wrote the
    ABNAHMEKRITERIUM: comment (which quotes a "?"-bearing UI error string,
    "… Hermes nicht erreichbar?", and trips no TECH_REGEX) and then a read-only
    File-Conflict-Check sub-run blocked needs_input with a REASON carrying no
    "?". The naive _letzte_frage latched onto the ABNAHMEKRITERIUM as the
    "frage", so the old gate passed; the observer's richter then judges that
    extracted frage NOT a Zielfrage → rot-technische-rueckfrage. The gate now
    excludes the ABNAHMEKRITERIUM (and non-specifier comments) from the frage,
    so the block is rejected and steered to a handoff."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="Hermes aus dem Frontend verbannen",
            assignee="specifier", repository="voicera",
        )
        kb.add_comment(
            conn, tid, author="specifier",
            body=("ABNAHMEKRITERIUM: 0 sichtbare `Hermes`-Renderstellen unter "
                  "frontend/src; Rail-/Board-Fehlertexte lauten 'Move "
                  "fehlgeschlagen — Voicera nicht erreichbar?' und 'Voicera-"
                  "Board nicht erreichbar'; `tsc --noEmit` Exit 0."),
        )
        kb.add_comment(
            conn, tid, author="specifier",
            body=("Baureif; Uebergabe technisch blockiert: ein read-only "
                  "Hilfsworker hat die Hauptkarte irrtuemlich blockiert."),
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "specifier")
    from tools import kanban_tools as kt
    out = kt._handle_block({
        "reason": "kanban_complete vom Rollen-Gate abgelehnt; Handoff ausserhalb "
                  "dieses read-only Teilauftrags.",
        "kind": "needs_input",
    })
    d = json.loads(out)
    assert "error" in d
    assert "genuine Zielfrage" in d["error"]
    assert "baureif" in d["error"]
    assert "kanban_handoff" in d["error"]
    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


def test_block_allowed_for_specifier_genuine_question_after_abnahmekriterium(monkeypatch, worker_env):
    """The ABNAHMEKRITERIUM exclusion must not swallow a genuine business
    Zielfrage the specifier raises AFTER writing the criterion: a specifier-
    authored, non-ABNAHMEKRITERIUM "?" comment still passes the gate (the
    beobachter's richter then judges it)."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="Branding-Umstellung", assignee="specifier",
            repository="voicera",
        )
        kb.add_comment(
            conn, tid, author="specifier",
            body="ABNAHMEKRITERIUM: 0 sichtbare Hermes-Treffer; tsc Exit 0.",
        )
        kb.add_comment(
            conn, tid, author="specifier",
            body="brauche Klaerung: soll der oeffentliche Markenname Voicera sein?",
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "specifier")
    from tools import kanban_tools as kt
    out = kt._handle_block({"reason": "siehe Frage oben", "kind": "needs_input"})
    d = json.loads(out)
    assert d.get("ok") is True


def test_block_rejected_for_specifier_unprepared_scope_demand(monkeypatch, worker_env):
    """F44: a specifier that cannot close an open 'alle'/'vollständig' scope must
    not off-load the enumeration onto the owner. Live probe Karte 18
    ('Deutsche Uebersetzung der verbleibenden englischen UI-Worte', klein,
    erwartung=done, t_e7ef743c): the specifier blocked needs_input conceding the
    scope has no closed check-set (ledger falsified, classes 'nur Beispiele',
    'keine abschließende Whitelist') and then asked the owner 'welche konkret
    aufgezählten Ansichten/Begriffe gelten als vollständig zu übersetzen, welche
    dürfen englisch bleiben?'. The observer's richter marks TOR1/TOR3 JA but
    TOR2 NEIN → rot-technische-rueckfrage. The gate rejects it and steers the
    specifier to enumerate the cut itself (build) or escalate a prepared GO."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="Deutsche Uebersetzung der verbleibenden englischen UI-Worte",
            assignee="specifier", repository="voicera",
        )
        kb.add_comment(
            conn, tid, author="specifier",
            body=("BLOCKED: Das Vollständigkeitsziel „alle produktiv sichtbaren "
                  "statischen Frontend-Texte“ hat noch keine geschlossene "
                  "Prüfmenge: Der 55er-Ledger ist falsifiziert, die bekannten "
                  "Regressionsklassen sind nur Beispiele, und die erlaubten "
                  "englischen Fachbegriffe haben ausdrücklich keine abschließende "
                  "Whitelist. Welche konkret aufgezählten Ansichten/Begriffe "
                  "gelten für diese Runde als vollständig zu übersetzen, und "
                  "welche konkret aufgezählten englischen Begriffe dürfen sichtbar "
                  "bleiben?"),
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "specifier")
    from tools import kanban_tools as kt
    out = kt._handle_block({"reason": "siehe Frage oben", "kind": "needs_input"})
    d = json.loads(out)
    assert "error" in d
    assert "UNPREPARED scope escalation" in d["error"]
    assert "kanban_handoff" in d["error"]
    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


def test_block_rejected_for_orchestrator_unprepared_scope_demand(monkeypatch, worker_env):
    """F44 also fires for the orchestrator (the other scope-owning role): it
    posted the same off-loading Zielfrage ('… oder welche konkret aufgezählten
    Ansichten/Begriffe …?') on Karte 18 before handing to the specifier."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="Deutsche Uebersetzung der verbleibenden englischen UI-Worte",
            assignee="orchestrator", repository="voicera",
        )
        kb.add_comment(
            conn, tid, author="orchestrator",
            body=("Der 55er-Ledger ist falsifiziert, nur Beispiele. ZIELFRAGE AN "
                  "DEN INHABER: Soll der erreichte Stand als fertig gelten; oder "
                  "welche konkret aufgezählten Ansichten/Begriffe müssen noch "
                  "deutsch sein und welche dürfen bewusst englisch bleiben?"),
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    from tools import kanban_tools as kt
    out = kt._handle_block({"reason": "siehe Zielfrage", "kind": "needs_input"})
    d = json.loads(out)
    assert "error" in d
    assert "UNPREPARED scope escalation" in d["error"]


def test_block_allowed_for_specifier_prepared_go_on_reached_state(monkeypatch, worker_env):
    """F44 must not catch a PREPARED escalation: a plain Abnahme/Freigabe-GO on
    the reached state (no open-set concession married to an owner-enumeration
    demand) passes the gate so the richter can green it (rule b)."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="Deutsche Uebersetzung der verbleibenden englischen UI-Worte",
            assignee="specifier", repository="voicera",
        )
        kb.add_comment(
            conn, tid, author="specifier",
            body=("Der breit übersetzte Stand ist erreicht und dokumentiert. "
                  "ABNAHME/FREIGABE: Soll der aktuell erreichte Stand für diese "
                  "Runde als fertig abgenommen und freigegeben werden? Folge bei "
                  "GO: vereinzelte dynamische Resttexte bleiben vorerst im "
                  "englischen Original."),
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "specifier")
    from tools import kanban_tools as kt
    out = kt._handle_block({"reason": "siehe GO oben", "kind": "needs_input"})
    d = json.loads(out)
    assert d.get("ok") is True


_TECHNIK_BODY = (
    "Generierungs-Artefakt: in SUMMARY- und PLAN-Dateien des S3-Backups taucht "
    "ein verwaistes </content>-Tag auf. Ursache im Erzeuger finden und beheben.\n\n"
    "Technik-Entscheidung (selbst treffen, im Kommentar begründen): Ob per Regex, "
    "Parser oder Prompt-Aenderung am Erzeuger entscheidet die Fabrik selbst."
)


def test_block_rejected_for_technik_card_rueckfrage(monkeypatch, worker_env):
    """F48: a technik card (body carries the 'Technik-Entscheidung (selbst
    treffen …)' contract marker) must never be escalated with a Rückfrage — the
    observer marks ANY blocked technik card ROT (rot-technik-rueckfrage, no
    Zielfrage-Richter). Live probe Karte 19 't_18b885af' ('S3-Backup: Streu-Tag
    </content> aus SUMMARY/PLAN entfernen'): the specifier blocked needs_input
    with a goal-redefinition Rückfrage. The gate rejects it and steers the
    fabric to decide the method itself and build to done."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="S3-Backup: Streu-Tag </content> aus SUMMARY/PLAN entfernen",
            body=_TECHNIK_BODY, assignee="specifier", repository="voicera",
        )
        kb.add_comment(
            conn, tid, author="specifier",
            body=("BRAUCHE KLÄRUNG: Soll das Ziel weiterhin „neue fehlerhafte "
                  "PLAN-/SUMMARY-Artefakte am Erzeuger verhindern“ lauten und der "
                  "externe Generator zugänglich gemacht werden, oder soll die "
                  "Karte nur auf die einmalige Bereinigung der 23 Repo-Treffer "
                  "umgeschnitten beziehungsweise geschlossen werden?"),
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "specifier")
    from tools import kanban_tools as kt
    out = kt._handle_block({"reason": "siehe Frage oben", "kind": "needs_input"})
    d = json.loads(out)
    assert "error" in d
    assert "TECHNIK card" in d["error"]
    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


def test_block_rejected_for_technik_card_any_role(monkeypatch, worker_env):
    """F48 is not role-scoped: a coder/reviewer Rückfrage on a technik card is
    equally ROT, so the gate fires for every factory role."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="asyncio Event-Loop-Bug in Celery-Scheduled-Tasks",
            body=_TECHNIK_BODY, assignee="voicera-coder", repository="voicera",
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "voicera-coder")
    from tools import kanban_tools as kt
    out = kt._handle_block(
        {"reason": "welche Lösung soll ich wählen?", "kind": "capability"}
    )
    d = json.loads(out)
    assert "error" in d
    assert "TECHNIK card" in d["error"]


def test_block_allowed_for_technik_card_dependency_kind(monkeypatch, worker_env):
    """F48 is scoped to the human-escalation kinds: `dependency` is machine
    routing (→ todo), not a Rückfrage, so it stays ungated even on a technik
    card (mirrors the observer, which only trips on a human blocked status)."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="S3-Backup: Streu-Tag entfernen",
            body=_TECHNIK_BODY, assignee="voicera-coder", repository="voicera",
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "voicera-coder")
    from tools import kanban_tools as kt
    out = kt._handle_block(
        {"reason": "file-conflict with running t_x", "kind": "dependency"}
    )
    d = json.loads(out)
    assert d.get("ok") is True


def test_block_allowed_for_non_technik_card(monkeypatch, worker_env):
    """F48 must not catch an ordinary card without the technik marker: a genuine
    human escalation on a non-technik card still blocks normally (checked with a
    coder role, which carries no other human-block gate)."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="coder hit a genuine external wall",
            body="Normale Aufgabe ohne Technik-Entscheidungsmarker.",
            assignee="voicera-coder", repository="voicera",
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "voicera-coder")
    from tools import kanban_tools as kt
    out = kt._handle_block({"reason": "need a decision from Till", "kind": "needs_input"})
    d = json.loads(out)
    assert d.get("ok") is True


def test_block_rejected_for_coder_internal_triage_comment(monkeypatch, worker_env):
    """F28: the repo coder may not escalate internal work-coordination. Live
    probe card t_9b6d4067 ('Ungenutzte Funktion leereTexte entfernen', klein,
    erwartung=done): the coder found the function still in develop and blocked
    needs_input asking the orchestrator whether t_9b6d4067 is a duplicate of
    t_7e8f28b1 (fn already removed on wt/t_7e8f28b1 but not merged) — close the
    card or rebuild? That question has a '?' and trips no TECH_REGEX, but it is
    pure internal task-identity/merge triage (task IDs, wt/ branches) → the
    beobachter's richter forces TOR1/TOR3 NO → 'rot-technische-rueckfrage'. The
    gate rejects it; the coder must resolve it itself and build."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="Ungenutzte Funktion leereTexte entfernen",
            assignee="voicera-coder", repository="voicera",
        )
        kb.add_comment(
            conn, tid, author="voicera-coder",
            body=("## Coder-Befund: Task-Verwechslung\n\nIst t_9b6d4067 ein "
                  "Duplikat von t_7e8f28b1 (Funktion schon auf wt/t_7e8f28b1 "
                  "entfernt, aber nicht in develop gemerged)? Karte schliessen "
                  "oder neu bauen?"),
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "voicera-coder")
    from tools import kanban_tools as kt
    out = kt._handle_block({"reason": "siehe Frage oben", "kind": "needs_input"})
    d = json.loads(out)
    assert "error" in d
    assert "internal work-coordination" in d["error"]
    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


def test_block_rejected_for_coder_internal_triage_in_reason(monkeypatch, worker_env):
    """F28: the gate also scans the block reason, so the coder cannot kind-shop
    the internal-triage question out of a comment and into the reason to slip
    past it. A reason naming a wt/ worktree branch is rejected."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="Ungenutzte Funktion leereTexte entfernen",
            assignee="voicera-coder", repository="voicera",
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "voicera-coder")
    from tools import kanban_tools as kt
    out = kt._handle_block({
        "reason": "Arbeit evtl. schon auf wt/t_7e8f28b1 erledigt — Karte "
                  "schliessen oder neu bauen?",
        "kind": "needs_input",
    })
    d = json.loads(out)
    assert "error" in d
    assert "internal work-coordination" in d["error"]
    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


def test_block_allowed_for_coder_genuine_business_wall(monkeypatch, worker_env):
    """F28 must not narrow the coder's genuine-wall path: a block whose question
    is a real business/Freigabe decision for Till — carrying no internal task
    IDs or wt/ branch names — still passes (the beobachter's richter then judges
    it). Mirror of test_block_allowed_for_non_orchestrator_repo_work_card."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="coder hit a wall",
            assignee="voicera-coder", repository="voicera",
        )
        kb.add_comment(
            conn, tid, author="voicera-coder",
            body="brauche Klaerung: soll ich nach main freigeben?",
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "voicera-coder")
    from tools import kanban_tools as kt
    out = kt._handle_block({"reason": "need a decision from Till", "kind": "needs_input"})
    d = json.loads(out)
    assert d.get("ok") is True


def test_handoff_rejected_for_specifier_to_recon(monkeypatch, worker_env):
    """F25: the specifier's forward handoff must go to a write-capable
    ``<repo>-coder``; recon is read-only and cannot build, so routing a build
    card there strands it. Guards against live probe card t_37376e6a ('CLAUDE.md
    gdpr-delete-Zeile entschaerfen', erwartung=done): an orchestrator note
    mislabeled 'Coder-Ziel: recon (read-write)', the specifier handed off to
    recon, recon reported a role-mismatch and blocked needs_input with no '?' →
    beobachter 'rot-technische-rueckfrage'. The error must point at the real
    repo coder."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="CLAUDE.md gdpr-delete-Zeile entschaerfen",
            assignee="specifier", repository="voicera",
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "specifier")
    from tools import kanban_tools as kt
    out = kt._handle_handoff({"to": "recon", "note": "bitte die Zeile anpassen"})
    d = json.loads(out)
    assert "error" in d
    assert "read-only" in d["error"]
    assert "voicera-coder" in d["error"]
    conn = kb.connect()
    try:
        t = kb.get_task(conn, tid)
        # Card stays with the specifier (running), not routed to recon.
        assert t.status == "running"
        assert t.assignee == "specifier"
    finally:
        conn.close()


def test_handoff_rejected_for_specifier_to_brain_reader(monkeypatch, worker_env):
    """The gate covers the other read-only delegation role too, so the specifier
    cannot role-shop (recon→brain-reader) to escape."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="Doku-Zeile entschaerfen", assignee="specifier",
            repository="voicera",
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "specifier")
    from tools import kanban_tools as kt
    out = kt._handle_handoff({"to": "brain-reader"})
    d = json.loads(out)
    assert "error" in d
    assert "read-only" in d["error"]


def test_handoff_allowed_for_specifier_to_coder(monkeypatch, worker_env):
    """The happy path: the specifier hands the build card to the repo coder —
    the F24-resolved route that greens this card."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="CLAUDE.md gdpr-delete-Zeile entschaerfen",
            assignee="specifier", repository="voicera",
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "specifier")
    from tools import kanban_tools as kt
    out = kt._handle_handoff({"to": "voicera-coder", "note": "ABNAHMEKRITERIUM steht"})
    d = json.loads(out)
    assert d.get("ok") is True
    assert d.get("assignee") == "voicera-coder"


def test_handoff_to_recon_allowed_for_orchestrator(monkeypatch, worker_env):
    """The gate is scoped to the specifier: an orchestrator dispatching a genuine
    read-only recon/verification card to recon stays free."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="Verify CLAUDE.md state", assignee="orchestrator",
            repository="voicera",
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    from tools import kanban_tools as kt
    out = kt._handle_handoff({"to": "recon", "note": "nur lesen und melden"})
    d = json.loads(out)
    assert d.get("ok") is True
    assert d.get("assignee") == "recon"


def test_complete_metadata_round_trips_through_show(worker_env):
    """Structured completion metadata should be visible to downstream agents."""
    from tools import kanban_tools as kt

    handoff = {
        "changed_files": ["hermes_cli/kanban.py"],
        "verification": ["pytest tests/tools/test_kanban_tools.py -q"],
        "dependencies": [],
        "blocked_reason": None,
        "retry_notes": "none",
        "residual_risk": ["dashboard rendering not exercised"],
    }

    complete_out = kt._handle_complete({
        "summary": "finished with structured evidence",
        "metadata": handoff,
    })
    assert json.loads(complete_out)["ok"] is True

    show_out = kt._handle_show({"task_id": worker_env})
    shown = json.loads(show_out)
    assert shown["task"]["status"] == "done"
    assert shown["runs"][-1]["summary"] == "finished with structured evidence"
    assert shown["runs"][-1]["metadata"] == handoff


def test_complete_stamps_worker_session_id_from_env(monkeypatch, worker_env):
    from tools import kanban_tools as kt

    monkeypatch.setenv("HERMES_SESSION_ID", "session-trusted")
    metadata = {"files": 2, "worker_session_id": "user-spoof"}

    out = kt._handle_complete({
        "summary": "done by scoped worker",
        "metadata": metadata,
    })
    assert json.loads(out)["ok"] is True
    assert metadata["worker_session_id"] == "user-spoof"

    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        run = kb.latest_run(conn, worker_env)
        assert run.metadata == {
            "files": 2,
            "worker_session_id": "session-trusted",
        }
    finally:
        conn.close()


def test_complete_does_not_stamp_worker_session_id_without_scoped_task(
    monkeypatch, worker_env
):
    from tools import kanban_tools as kt

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_SESSION_ID", "session-trusted")

    out = kt._handle_complete({
        "task_id": worker_env,
        "summary": "done outside worker scope",
        "metadata": {"files": 2, "worker_session_id": "user-provided"},
    })
    assert json.loads(out)["ok"] is True

    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        run = kb.latest_run(conn, worker_env)
        assert run.metadata == {
            "files": 2,
            "worker_session_id": "user-provided",
        }
    finally:
        conn.close()


def test_complete_with_result_only(worker_env):
    """`result` alone (without summary) is accepted for legacy compat."""
    from tools import kanban_tools as kt
    out = kt._handle_complete({"result": "legacy result"})
    d = json.loads(out)
    assert d["ok"] is True


def test_complete_with_artifacts_lands_in_event_payload(worker_env):
    """``artifacts=[...]`` rides into the completed event payload so the
    gateway notifier can upload them as native attachments. See the
    kanban notifier in gateway/run.py for the consumer side."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_complete({
        "summary": "rendered the chart",
        "artifacts": ["/tmp/q3-revenue.png", "/tmp/q3-report.pdf"],
    })
    assert json.loads(out)["ok"] is True

    conn = kb.connect()
    try:
        events = kb.list_events(conn, worker_env)
        # Find the completion event
        completed = [e for e in events if e.kind == "completed"]
        assert len(completed) == 1
        payload = completed[0].payload or {}
        assert payload.get("artifacts") == [
            "/tmp/q3-revenue.png",
            "/tmp/q3-report.pdf",
        ]
        # And the artifacts also live on metadata for downstream workers
        run = kb.latest_run(conn, worker_env)
        assert run.metadata.get("artifacts") == [
            "/tmp/q3-revenue.png",
            "/tmp/q3-report.pdf",
        ]
    finally:
        conn.close()


def test_complete_artifacts_accepts_single_string(worker_env):
    """A bare string is auto-promoted to a single-element list for convenience."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_complete({
        "summary": "one chart",
        "artifacts": "/tmp/chart.png",
    })
    assert json.loads(out)["ok"] is True

    conn = kb.connect()
    try:
        run = kb.latest_run(conn, worker_env)
        assert run.metadata.get("artifacts") == ["/tmp/chart.png"]
    finally:
        conn.close()


def test_complete_artifacts_merges_with_explicit_metadata_field(worker_env):
    """If the worker passes metadata.artifacts AND the top-level artifacts
    param, merge the two without duplicates."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_complete({
        "summary": "merged",
        "metadata": {"artifacts": ["/tmp/a.png"], "other": "fact"},
        "artifacts": ["/tmp/b.pdf", "/tmp/a.png"],
    })
    assert json.loads(out)["ok"] is True

    conn = kb.connect()
    try:
        run = kb.latest_run(conn, worker_env)
        # Order: existing entries first, then new ones, deduplicated.
        assert run.metadata.get("artifacts") == ["/tmp/a.png", "/tmp/b.pdf"]
        assert run.metadata.get("other") == "fact"
    finally:
        conn.close()


def test_complete_rejects_non_list_artifacts(worker_env):
    """Non-list, non-string artifacts should be rejected with a clear error."""
    from tools import kanban_tools as kt
    out = kt._handle_complete({
        "summary": "bad shape",
        "artifacts": {"not": "a list"},
    })
    err = json.loads(out).get("error", "")
    assert "artifacts must be a list" in err


def test_complete_missing_scratch_artifact_stays_in_flight(worker_env):
    """A false deliverable claim must return retry guidance, not mark Done."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    with kb.connect() as conn:
        task = kb.get_task(conn, worker_env)
        assert task is not None
        workspace = kb.resolve_workspace(task)
        kb.set_workspace_path(conn, worker_env, workspace)

    output = kt._handle_complete({
        "summary": "report complete",
        "artifacts": [str(workspace / "missing-report.md")],
    })
    error = json.loads(output).get("error", "")

    assert "could not preserve" in error
    assert "still in-flight" in error
    assert "retry kanban_complete" in error
    with kb.connect() as conn:
        assert kb.get_task(conn, worker_env).status == "running"
    assert workspace.exists()


def test_complete_rejects_no_handoff(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_complete({})
    assert json.loads(out).get("error"), "should have errored"


def test_complete_rejects_non_dict_metadata(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_complete({"summary": "x", "metadata": [1, 2, 3]})
    assert json.loads(out).get("error")


def test_complete_phantom_card_message_advertises_retry(worker_env):
    """A phantom-card rejection must surface a tool_error that explicitly
    tells the worker the task is still in-flight and how to retry — the
    worker has no other channel to discover that. Regression for #22923,
    where the previous wording read like a terminal failure and workers
    routinely abandoned the run instead of trying again.
    """
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_complete({
        "summary": "oops claimed a phantom",
        "created_cards": ["t_phantomdeadbeef"],
    })
    err = json.loads(out).get("error", "")
    assert err, f"expected an error, got {out!r}"
    # Phantom id surfaced verbatim.
    assert "t_phantomdeadbeef" in err
    # The retry-is-supported phrasing — these are the literal cues a
    # worker reads to decide whether to retry vs block/abandon. If a
    # future change rewords the message, these checks will catch the
    # regression. See #22923 for the failure mode.
    assert "still in-flight" in err
    assert "Retry kanban_complete" in err
    assert "created_cards=[]" in err

    # Critically: the task is genuinely still in-flight — the gate
    # rejection did not mutate state, so the worker's retry can land.
    conn = kb.connect()
    try:
        assert kb.get_task(conn, worker_env).status == "running"
    finally:
        conn.close()


def test_complete_retry_with_empty_created_cards_succeeds(worker_env):
    """After a phantom rejection, retrying kanban_complete with
    created_cards=[] (the documented escape hatch) must complete the
    task. Regression for #22923."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    # Hit the gate first.
    rejected = json.loads(kt._handle_complete({
        "summary": "oops",
        "created_cards": ["t_phantomdeadbeef"],
    }))
    assert rejected.get("error")

    # Retry with the escape hatch.
    ok = json.loads(kt._handle_complete({
        "summary": "retry without claims",
        "created_cards": [],
    }))
    assert ok.get("ok") is True

    conn = kb.connect()
    try:
        assert kb.get_task(conn, worker_env).status == "done"
    finally:
        conn.close()


def test_complete_retry_with_corrected_created_cards_succeeds(worker_env):
    """After a phantom rejection, retrying kanban_complete with a
    corrected created_cards list (phantom ids removed) must complete the
    task. Regression for #22923."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    # Create a real child via the tool so it gets the worker-profile
    # attribution the gate trusts.
    child = json.loads(kt._handle_create({
        "title": "real child", "assignee": "peer",
    }))
    assert child["ok"]
    real_id = child["task_id"]

    # First attempt mixes real + phantom — gate rejects.
    rejected = json.loads(kt._handle_complete({
        "summary": "oops",
        "created_cards": [real_id, "t_phantomdeadbeef"],
    }))
    assert rejected.get("error")
    assert "t_phantomdeadbeef" in rejected["error"]

    # Retry with corrected list.
    ok = json.loads(kt._handle_complete({
        "summary": "retry with corrected list",
        "created_cards": [real_id],
    }))
    assert ok.get("ok") is True


def test_complete_goal_mode_rejected_by_judge(monkeypatch, tmp_path):
    """Goal-mode tasks must pass the auxiliary judge before completion.
    Regression for #38367: workers bypassing the judge via early kanban_complete."""
    from pathlib import Path as _Path
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    # Set up isolated HERMES_HOME
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        goal_task_id = kb.create_task(
            conn, title="goal-mode-test", assignee="test-worker",
            body="Must achieve X with verified evidence.", goal_mode=True
        )
        kb.claim_task(conn, goal_task_id)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", goal_task_id)

    # Mock the judge to reject the completion. The gate only runs when a
    # judge is reachable, so force the availability probe True as well.
    def mock_judge_goal(goal, last_response, *, timeout=30.0, subgoals=None):
        return "continue", "missing verification evidence", False

    monkeypatch.setattr("tools.kanban_tools.judge_goal", mock_judge_goal)
    monkeypatch.setattr("tools.kanban_tools._goal_judge_available", lambda: True)

    # Attempt to complete should be rejected
    out = kt._handle_complete({"summary": "I did some stuff but not X"})
    d = json.loads(out)
    assert "error" in d
    assert "Goal completion rejected by judge" in d["error"]
    assert "missing verification evidence" in d["error"]
    assert f"parents=[{goal_task_id}]" in d["error"]

    # Verify the task is NOT completed in the DB
    conn2 = kb.connect()
    try:
        task = kb.get_task(conn2, goal_task_id)
        assert task.status == "running"  # Should still be running, not done
    finally:
        conn2.close()


def test_complete_goal_mode_allows_when_judge_unavailable(monkeypatch, tmp_path):
    """Fail-open: an unreachable judge must not wedge a goal_mode worker.

    judge_goal returns a "continue" verdict when no auxiliary model is
    configured, which is indistinguishable from a real "not done" judgment.
    The gate probes availability first, so completion proceeds rather than
    being rejected forever when no judge can be reached."""
    from pathlib import Path as _Path
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        goal_task_id = kb.create_task(
            conn, title="goal-mode-test", assignee="test-worker",
            body="Must achieve X with verified evidence.", goal_mode=True
        )
        kb.claim_task(conn, goal_task_id)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", goal_task_id)

    # No judge reachable. judge_goal must not even be consulted; if it were,
    # this stub would reject — so reaching "done" proves the probe short-circuit.
    def fail_if_called(goal, last_response, *, timeout=30.0, subgoals=None):
        raise AssertionError("judge_goal must not run when no judge is available")

    monkeypatch.setattr("tools.kanban_tools.judge_goal", fail_if_called)
    monkeypatch.setattr("tools.kanban_tools._goal_judge_available", lambda: False)

    out = kt._handle_complete({"summary": "done enough"})
    d = json.loads(out)
    assert d.get("ok") is True

    conn2 = kb.connect()
    try:
        assert kb.get_task(conn2, goal_task_id).status == "done"
    finally:
        conn2.close()


def test_block_happy_path(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_block({"reason": "need clarification"})
    d = json.loads(out)
    assert d["ok"] is True
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        assert kb.get_task(conn, worker_env).status == "blocked"
    finally:
        conn.close()


def test_block_rejects_empty_reason(worker_env):
    from tools import kanban_tools as kt
    for bad in ["", "   ", None]:
        out = kt._handle_block({"reason": bad})
        assert json.loads(out).get("error")


def _make_goal_mode_worker_env(monkeypatch, tmp_path):
    """Set up an isolated HERMES_HOME with one claimed goal_mode task,
    matching the pattern used by the kanban_complete judge gate tests."""
    from pathlib import Path as _Path
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        goal_task_id = kb.create_task(
            conn, title="goal-mode-block-test", assignee="test-worker",
            body="Must achieve X.", goal_mode=True,
        )
        kb.claim_task(conn, goal_task_id)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", goal_task_id)
    return goal_task_id


def test_block_goal_mode_rejects_missing_kind(monkeypatch, tmp_path):
    """A goal_mode worker calling kanban_block with no kind must not be able
    to use it as an unguarded escape from the goal loop (Issue #38696,
    sibling of the kanban_complete judge gate / Issue #38367)."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    tid = _make_goal_mode_worker_env(monkeypatch, tmp_path)
    out = kt._handle_block({"reason": "giving up"})
    d = json.loads(out)
    assert "error" in d
    assert "goal_mode" in d["error"]

    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


def test_block_goal_mode_rejects_disallowed_kind(monkeypatch, tmp_path):
    """`capability` / `transient` are valid kinds in general but must not
    let a goal_mode worker exit the loop without going through the judge."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    tid = _make_goal_mode_worker_env(monkeypatch, tmp_path)
    for kind in ("capability", "transient"):
        out = kt._handle_block({"reason": "blocked", "kind": kind})
        d = json.loads(out)
        assert "error" in d, f"kind={kind} should be rejected for goal_mode"

    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


def test_block_goal_mode_allows_dependency_kind(monkeypatch, tmp_path):
    """`dependency` and `needs_input` represent a genuine external blocker
    the worker cannot resolve itself — these remain ungated.

    `dependency` routes to status='todo' (not 'blocked') per block_task's
    own kind-routing — the goal loop still treats anything outside
    running/ready/done/blocked as a stop, so this is still a legitimate,
    judge-free exit; it's just not the literal 'blocked' status."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    tid = _make_goal_mode_worker_env(monkeypatch, tmp_path)
    out = kt._handle_block({"reason": "waiting on another task", "kind": "dependency"})
    d = json.loads(out)
    assert d.get("ok") is True

    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "todo"
    finally:
        conn.close()


def test_block_goal_mode_allows_needs_input_kind(monkeypatch, tmp_path):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    tid = _make_goal_mode_worker_env(monkeypatch, tmp_path)
    out = kt._handle_block({"reason": "need a decision from the user", "kind": "needs_input"})
    d = json.loads(out)
    assert d.get("ok") is True

    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "blocked"
    finally:
        conn.close()


def test_block_non_goal_mode_task_unaffected_by_new_gate(worker_env):
    """The new gate only applies to goal_mode tasks — plain tasks must keep
    blocking freely with no kind, exactly as before this fix."""
    from tools import kanban_tools as kt
    out = kt._handle_block({"reason": "need clarification"})
    assert json.loads(out).get("ok") is True


def test_heartbeat_happy_path(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_heartbeat({"note": "progress"})
    d = json.loads(out)
    assert d["ok"] is True


def test_heartbeat_without_note(worker_env):
    """note is optional."""
    from tools import kanban_tools as kt
    out = kt._handle_heartbeat({})
    d = json.loads(out)
    assert d["ok"] is True


def test_heartbeat_extends_claim_expires(worker_env):
    """The kanban_heartbeat tool MUST extend claim_expires, not just
    update last_heartbeat_at — otherwise long-running workers loop the
    heartbeat tool diligently and still get reclaimed by
    release_stale_claims at DEFAULT_CLAIM_TTL_SECONDS.

    Regression test for the bug where _handle_heartbeat called
    heartbeat_worker but never heartbeat_claim, so claim_expires sat
    static while last_heartbeat_at advanced.
    """
    import time as _time
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    # Rewind claim_expires into the past so any forward movement is
    # unambiguous (avoids time.sleep flakiness).
    conn = kb.connect()
    try:
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (1, worker_env),
        )
        conn.commit()
        before = conn.execute(
            "SELECT claim_expires FROM tasks WHERE id = ?", (worker_env,)
        ).fetchone()["claim_expires"]
    finally:
        conn.close()
    assert before == 1

    out = kt._handle_heartbeat({"note": "still alive"})
    assert json.loads(out).get("ok") is True

    conn = kb.connect()
    try:
        after = conn.execute(
            "SELECT claim_expires FROM tasks WHERE id = ?", (worker_env,)
        ).fetchone()["claim_expires"]
    finally:
        conn.close()

    now = int(_time.time())
    # claim_expires should be roughly now + DEFAULT_CLAIM_TTL_SECONDS.
    # We assert a generous floor (now + half the default TTL) to keep the
    # test stable against future TTL changes.
    assert after > before, (
        f"claim_expires did not advance ({before} -> {after}); workers "
        f"would be reclaimed at TTL despite heartbeating"
    )
    assert after >= now + (kb.DEFAULT_CLAIM_TTL_SECONDS // 2), (
        f"claim_expires={after} is suspiciously close to now={now}; "
        f"expected at least now + {kb.DEFAULT_CLAIM_TTL_SECONDS // 2}"
    )


def test_comment_happy_path(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_comment({
        "task_id": worker_env,
        "body": "hello thread",
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["comment_id"]
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        comments = kb.list_comments(conn, worker_env)
        assert len(comments) == 1
        # Author defaults to HERMES_PROFILE env we set in the fixture
        assert comments[0].author == "test-worker"
        assert comments[0].body == "hello thread"
    finally:
        conn.close()


def test_comment_rejects_empty_body(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_comment({"task_id": worker_env, "body": "   "})
    assert json.loads(out).get("error")


def test_comment_ignores_caller_supplied_author(worker_env):
    """``args["author"]`` is no longer honored — the author is always
    derived from ``HERMES_PROFILE`` so a worker can't forge a comment
    under an authoritative-looking name like ``hermes-system`` and
    poison the next worker's prompt context. Cross-task commenting
    itself remains unrestricted (see #19713); only the author override
    is removed.
    """
    from tools import kanban_tools as kt
    out = kt._handle_comment({
        "task_id": worker_env, "body": "hi", "author": "hermes-system",
    })
    assert json.loads(out)["ok"]
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        comments = kb.list_comments(conn, worker_env)
        # Author comes from HERMES_PROFILE in the fixture, not the
        # caller-supplied "hermes-system" override.
        assert comments[0].author == "test-worker"
    finally:
        conn.close()


def test_comment_schema_omits_author_override():
    """The ``author`` property must not appear on KANBAN_COMMENT_SCHEMA;
    exposing it to the LLM would re-introduce the forgery surface this
    handler is hardened against.
    """
    from tools.kanban_tools import KANBAN_COMMENT_SCHEMA
    props = KANBAN_COMMENT_SCHEMA["parameters"]["properties"]
    assert "author" not in props


def test_create_happy_path(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_create({
        "title": "child task",
        "assignee": "peer",
        "parents": [worker_env],
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["task_id"]
    assert d["status"] == "todo"  # parent isn't done yet
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        child = kb.get_task(conn, d["task_id"])
        assert child.title == "child task"
        assert child.assignee == "peer"
    finally:
        conn.close()


def test_create_persists_structured_repository(worker_env):
    """The worker API stores repository routing independently of the body."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    out = kt._handle_create({
        "title": "framework child",
        "assignee": "peer",
        "repository": "hermes-agent",
    })
    task_id = json.loads(out)["task_id"]
    conn = kb.connect()
    try:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.repository == "hermes"
        profile = kb.resolve_task_repository_profile(conn, task_id)
        assert profile is not None
        assert profile.base_ref == "fork/main"
    finally:
        conn.close()


def test_create_inherits_worker_dir_workspace(monkeypatch, worker_env):
    """A worker scoped to a dir: task that spawns a child without a
    workspace arg inherits the dir, not scratch (so follow-up code-gen
    lands in the same project)."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    proj = "/home/teknium/myproject"
    conn = kb.connect()
    try:
        self_tid = kb.create_task(
            conn, title="dir worker", assignee="test-worker",
            workspace_kind="dir", workspace_path=proj,
        )
        kb.claim_task(conn, self_tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", self_tid)

    d = json.loads(kt._handle_create({"title": "follow-up", "assignee": "peer"}))
    assert d["ok"] is True
    conn = kb.connect()
    try:
        child = kb.get_task(conn, d["task_id"])
        assert child.workspace_kind == "dir"
        assert child.workspace_path == proj
    finally:
        conn.close()


def test_create_explicit_workspace_beats_inheritance(monkeypatch, worker_env):
    """An explicit workspace arg overrides worker-task inheritance."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        self_tid = kb.create_task(
            conn, title="dir worker", assignee="test-worker",
            workspace_kind="dir", workspace_path="/home/teknium/proj",
        )
        kb.claim_task(conn, self_tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", self_tid)

    d = json.loads(kt._handle_create({
        "title": "scratch child", "assignee": "peer",
        "workspace_kind": "scratch",
    }))
    assert d["ok"] is True
    conn = kb.connect()
    try:
        child = kb.get_task(conn, d["task_id"])
        assert child.workspace_kind == "scratch"
    finally:
        conn.close()


def test_create_no_worker_task_stays_scratch(monkeypatch, worker_env):
    """Orchestrator/CLI callers (no HERMES_KANBAN_TASK) still default to
    scratch — inheritance only applies to task-scoped workers."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    d = json.loads(kt._handle_create({"title": "orch child", "assignee": "peer"}))
    assert d["ok"] is True
    conn = kb.connect()
    try:
        child = kb.get_task(conn, d["task_id"])
        assert child.workspace_kind == "scratch"
        assert child.workspace_path is None
    finally:
        conn.close()


def test_create_stamps_session_id_from_env(monkeypatch, worker_env):
    """When the agent loop runs under ACP, the server propagates the
    originating chat session id via HERMES_SESSION_ID. ``kanban_create``
    reads it and stamps the new task so clients can render a per-session
    board (issue: ACP session linkage on kanban tasks)."""
    monkeypatch.setenv("HERMES_SESSION_ID", "acp-sess-abc")
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "from chat",
        "assignee": "peer",
        "parents": [worker_env],
    })
    d = json.loads(out)
    assert d["ok"] is True
    conn = kb.connect()
    try:
        new_task = kb.get_task(conn, d["task_id"])
        assert new_task.session_id == "acp-sess-abc"
    finally:
        conn.close()


def test_create_session_id_arg_overrides_env(monkeypatch, worker_env):
    """An explicit ``session_id`` arg from the model wins over the env
    propagation. Edge case but exercised: a tool call could carry a
    different session id (e.g. cross-session linking) and the explicit
    arg should not be silently overwritten."""
    monkeypatch.setenv("HERMES_SESSION_ID", "from-env")
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "explicit override",
        "assignee": "peer",
        "parents": [worker_env],
        "session_id": "explicit-arg",
    })
    d = json.loads(out)
    assert d["ok"] is True
    conn = kb.connect()
    try:
        new_task = kb.get_task(conn, d["task_id"])
        assert new_task.session_id == "explicit-arg"
    finally:
        conn.close()


def test_create_session_id_absent_when_env_unset(monkeypatch, worker_env):
    """No env var, no arg → session_id stays NULL. Important for backwards
    compatibility: pre-ACP-propagation hosts and CLI-driven creates must
    not accidentally inherit a stale id."""
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "no session",
        "assignee": "peer",
        "parents": [worker_env],
    })
    d = json.loads(out)
    assert d["ok"] is True
    conn = kb.connect()
    try:
        new_task = kb.get_task(conn, d["task_id"])
        assert new_task.session_id is None
    finally:
        conn.close()


def test_create_rejects_no_title(worker_env):
    from tools import kanban_tools as kt
    assert json.loads(kt._handle_create({"assignee": "x"})).get("error")
    assert json.loads(kt._handle_create({"title": "   ", "assignee": "x"})).get("error")


def test_create_rejects_no_assignee(worker_env):
    from tools import kanban_tools as kt
    assert json.loads(kt._handle_create({"title": "t"})).get("error")


def test_create_rejects_non_list_parents(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_create({"title": "t", "assignee": "a", "parents": 42})
    assert json.loads(out).get("error")


def test_create_parses_triage_string_false(worker_env):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "not triage",
        "assignee": "peer",
        "triage": "false",
    })
    d = json.loads(out)
    assert d["ok"] is True
    conn = kb.connect()
    try:
        task = kb.get_task(conn, d["task_id"])
        assert task.status == "ready"
    finally:
        conn.close()


def test_create_parses_triage_string_true(worker_env):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "needs triage",
        "assignee": "peer",
        "triage": "true",
    })
    d = json.loads(out)
    assert d["ok"] is True
    conn = kb.connect()
    try:
        task = kb.get_task(conn, d["task_id"])
        assert task.status == "triage"
    finally:
        conn.close()


def test_create_rejects_bad_triage(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_create({
        "title": "bad triage",
        "assignee": "peer",
        "triage": "sometimes",
    })
    assert "triage must be" in json.loads(out).get("error", "")


def test_create_accepts_string_parent(worker_env):
    """Convenience: a single parent id as string is coerced to [id]."""
    from tools import kanban_tools as kt
    out = kt._handle_create({
        "title": "t", "assignee": "a", "parents": worker_env,
    })
    assert json.loads(out)["ok"]


def test_create_accepts_skills_list(worker_env):
    """Tool writes the per-task skills through to the kernel."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "skilled",
        "assignee": "linguist",
        "skills": ["translation", "github-code-review"],
    })
    d = json.loads(out)
    assert d["ok"] is True
    with kb.connect() as conn:
        task = kb.get_task(conn, d["task_id"])
    assert task.skills == ["translation", "github-code-review"]


def test_create_accepts_skills_string(worker_env):
    """Convenience: a single skill name as string is coerced to [name]."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "one-skill",
        "assignee": "a",
        "skills": "translation",
    })
    d = json.loads(out)
    assert d["ok"] is True
    with kb.connect() as conn:
        task = kb.get_task(conn, d["task_id"])
    assert task.skills == ["translation"]


def test_create_rejects_non_list_skills(worker_env):
    """skills: 42 must be rejected, not silently dropped."""
    from tools import kanban_tools as kt
    out = kt._handle_create({
        "title": "t", "assignee": "a", "skills": 42,
    })
    assert json.loads(out).get("error")


def test_link_happy_path(worker_env):
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="A", assignee="x")
        b = kb.create_task(conn, title="B", assignee="x")
    finally:
        conn.close()
    from tools import kanban_tools as kt
    out = kt._handle_link({"parent_id": a, "child_id": b})
    d = json.loads(out)
    assert d["ok"] is True


def test_link_rejects_self_reference(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_link({"parent_id": worker_env, "child_id": worker_env})
    assert json.loads(out).get("error")


def test_link_rejects_missing_args(worker_env):
    from tools import kanban_tools as kt
    assert json.loads(kt._handle_link({"parent_id": "x"})).get("error")
    assert json.loads(kt._handle_link({"child_id": "y"})).get("error")


def test_link_rejects_cycle(worker_env):
    """A → B, then try to link B → A."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="A", assignee="x")
        b = kb.create_task(conn, title="B", assignee="x", parents=[a])
    finally:
        conn.close()
    from tools import kanban_tools as kt
    out = kt._handle_link({"parent_id": b, "child_id": a})
    assert json.loads(out).get("error")


def test_unblock_happy_path(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="blocked", assignee="worker")
        kb.block_task(conn, tid, reason="waiting")
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_unblock({"task_id": tid})
    d = json.loads(out)
    assert d["ok"] is True
    assert d["status"] == "ready"

    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "ready"
    finally:
        conn.close()


def test_unblock_rejects_non_blocked_task(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from tools import kanban_tools as kt
    out = kt._handle_unblock({"task_id": worker_env})
    assert json.loads(out).get("error")


def test_worker_lifecycle_through_tools(worker_env):
    """Drive the full claim -> heartbeat -> comment -> complete lifecycle
    exclusively through the tools, then verify the DB state matches what
    the dispatcher/notifier expect."""
    from tools import kanban_tools as kt

    # 1. show — worker orientation
    show = json.loads(kt._handle_show({}))
    assert show["task"]["id"] == worker_env

    # 2. heartbeat during long op
    assert json.loads(kt._handle_heartbeat({"note": "warming up"}))["ok"]

    # 3. comment for a future peer
    assert json.loads(kt._handle_comment({
        "task_id": worker_env,
        "body": "note: using stdlib sqlite3 bindings",
    }))["ok"]

    # 4. spawn a child task for follow-up
    child_out = json.loads(kt._handle_create({
        "title": "write integration test",
        "assignee": "qa",
        "parents": [worker_env],
    }))
    assert child_out["ok"]

    # 5. complete with structured handoff
    comp = json.loads(kt._handle_complete({
        "summary": "implemented + spawned QA follow-up",
        "metadata": {"child_task": child_out["task_id"]},
    }))
    assert comp["ok"]

    # Verify final state
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        parent = kb.get_task(conn, worker_env)
        assert parent.status == "done"
        assert parent.current_run_id is None
        run = kb.latest_run(conn, worker_env)
        assert run.outcome == "completed"
        assert run.metadata == {"child_task": child_out["task_id"]}
        # Child is todo (parent just finished, but recompute_ready may
        # have promoted it — complete_task runs recompute internally).
        child = kb.get_task(conn, child_out["task_id"])
        assert child.status == "ready", (
            f"child should be ready after parent done, got {child.status}"
        )
        # Comment is visible
        assert len(kb.list_comments(conn, worker_env)) == 1
        # Heartbeat event recorded
        hb = [e for e in kb.list_events(conn, worker_env) if e.kind == "heartbeat"]
        assert len(hb) == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# System-prompt guidance injection
# ---------------------------------------------------------------------------

def test_kanban_guidance_not_in_normal_prompt(monkeypatch, tmp_path):
    """A normal chat session (no HERMES_KANBAN_TASK) must NOT have
    KANBAN_GUIDANCE in its system prompt."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from pathlib import Path as _P
    monkeypatch.setattr(_P, "home", lambda: tmp_path)

    from tools.registry import invalidate_check_fn_cache
    from model_tools import _clear_tool_defs_cache
    invalidate_check_fn_cache()
    _clear_tool_defs_cache()

    from run_agent import AIAgent
    a = AIAgent(
        api_key="test",
        base_url="https://openrouter.ai/api/v1",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    prompt = a._build_system_prompt()
    assert "You are a Kanban worker" not in prompt
    assert "kanban_show()" not in prompt


def test_kanban_guidance_in_worker_prompt(monkeypatch, tmp_path):
    """A worker session (HERMES_KANBAN_TASK set) MUST have the full
    lifecycle guidance in its system prompt."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fake")
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from pathlib import Path as _P
    monkeypatch.setattr(_P, "home", lambda: tmp_path)

    from tools.registry import invalidate_check_fn_cache
    from model_tools import _clear_tool_defs_cache
    invalidate_check_fn_cache()
    _clear_tool_defs_cache()

    from run_agent import AIAgent
    a = AIAgent(
        api_key="test",
        base_url="https://openrouter.ai/api/v1",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    prompt = a._build_system_prompt()
    # Header phrase (identity-free — SOUL.md owns identity, layer 3 is protocol)
    assert "Kanban task execution protocol" in prompt
    # Lifecycle signals
    assert "kanban_show()" in prompt
    assert "kanban_complete" in prompt
    assert "kanban_block" in prompt
    assert "kanban_create" in prompt
    # Anti-shell guidance
    assert "Do not shell out" in prompt or "tools — they work" in prompt


def test_kanban_guidance_prompt_size_bounded(monkeypatch, tmp_path):
    """Sanity: the guidance block stays lean so it doesn't blow up the
    cached prompt.

    The ceiling guards against unbounded growth, not against any growth.
    The block absorbed the load-bearing worker/orchestrator reference
    details (workspace kinds, deliverable artifacts, created-card claims,
    profile discovery) when the standalone kanban-worker / kanban-orchestrator
    skills were removed and folded into this always-injected guidance, so the
    ceiling is sized to fit that content with a little headroom.
    """
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fake")
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from pathlib import Path as _P
    monkeypatch.setattr(_P, "home", lambda: tmp_path)

    from agent.prompt_builder import KANBAN_GUIDANCE
    # Ceiling raised 5_500 → 8_500 after the review-routing/push-before-review
    # and board-column rules were folded into the guidance (local commits
    # 4970bee22/c829b0326 grew it to ~7k chars intentionally).
    # Ceiling raised 8_500 → 12_000 (2026-08-18): the self-healing pass
    # codified six live-reproduced worker failure modes as rules
    # (origin/develop base, dep install, dirty-worktree adoption,
    # force-with-lease on own branch, reviewer-owned visual checks,
    # human-GO cards to till, done-verification evidence) — each one
    # prevented a real chain stall, so they are load-bearing.
    assert 1_500 < len(KANBAN_GUIDANCE) < 12_000, (
        f"KANBAN_GUIDANCE is {len(KANBAN_GUIDANCE)} chars — too short (missing?) or too long"
    )


# ---------------------------------------------------------------------------
# Worker task-ownership enforcement (regression tests for #19534)
# ---------------------------------------------------------------------------
#
# A worker process has HERMES_KANBAN_TASK set to its own task id. The
# destructive tools (kanban_complete, kanban_block, kanban_heartbeat,
# kanban_unblock) must refuse to operate
# on any OTHER task id, even if the caller supplies an explicit `task_id`
# argument. Workers legitimately call kanban_show / kanban_list /
# kanban_comment / kanban_create / kanban_link on other tasks, so those
# are unrestricted.
#
# Orchestrator profiles (no HERMES_KANBAN_TASK in env) are intentionally
# exempt — their job is routing, and they sometimes close out child
# tasks on behalf of the child.


def test_worker_complete_rejects_foreign_task_id(worker_env):
    """A worker cannot complete a task that isn't its own (#19534)."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="sibling")
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (other,))
        conn.commit()
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_complete({"task_id": other, "summary": "HIJACK"})
    d = json.loads(out)
    assert d.get("ok") is not True
    assert "refusing to mutate" in d.get("error", "")

    # Sibling task must be untouched.
    conn = kb.connect()
    try:
        assert kb.get_task(conn, other).status == "ready"
    finally:
        conn.close()


def test_worker_block_rejects_foreign_task_id(worker_env):
    """A worker cannot block a task that isn't its own (#19534)."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="sibling")
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (other,))
        conn.commit()
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_block({"task_id": other, "reason": "evil"})
    d = json.loads(out)
    assert "refusing to mutate" in d.get("error", "")

    conn = kb.connect()
    try:
        assert kb.get_task(conn, other).status == "ready"
    finally:
        conn.close()


def test_worker_heartbeat_rejects_foreign_task_id(worker_env):
    """A worker cannot heartbeat a task that isn't its own (#19534)."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="sibling")
        # Put sibling in running state so heartbeat would otherwise succeed.
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (other,))
        conn.commit()
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_heartbeat({"task_id": other})
    d = json.loads(out)
    assert "refusing to mutate" in d.get("error", "")


def test_worker_can_comment_on_foreign_task(worker_env):
    """Cross-task commenting must remain unrestricted (#19713 policy).

    The author-forgery hardening removed args['author'] but deliberately
    did NOT add an ownership gate to kanban_comment — comments are the
    documented handoff channel between tasks. This test pins that policy
    so a future change accidentally adding ``_enforce_worker_task_ownership``
    to ``_handle_comment`` would fail CI immediately.
    """
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="sibling")
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_comment({
        "task_id": other,
        "body": "handoff: see prior findings before starting",
    })
    d = json.loads(out)
    assert d.get("ok") is True, f"cross-task comment must succeed: {d}"

    # The comment lands on the foreign task, attributed to the worker's
    # HERMES_PROFILE — never to a caller-controlled string.
    conn = kb.connect()
    try:
        comments = kb.list_comments(conn, other)
        assert len(comments) == 1
        assert comments[0].author == "test-worker"
        assert comments[0].body.startswith("handoff:")
    finally:
        conn.close()


def test_worker_unblock_rejects_foreign_task_id(worker_env):
    """A worker cannot unblock any task — kanban_unblock is orchestrator-only.

    The check fires before the per-task ownership check, so the error
    surface is the orchestrator-only refusal rather than the
    cross-task-ownership refusal. Either is fine — the property we're
    pinning is "worker cannot mutate foreign task via kanban_unblock".
    """
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="blocked sibling", assignee="peer")
        kb.block_task(conn, other, reason="waiting")
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_unblock({"task_id": other})
    d = json.loads(out)
    err = d.get("error", "")
    assert "orchestrator-only" in err or "refusing to mutate" in err, (
        f"expected worker-rejection error, got {err}"
    )

    conn = kb.connect()
    try:
        assert kb.get_task(conn, other).status == "blocked"
    finally:
        conn.close()


def test_worker_complete_own_task_still_works(worker_env):
    """The ownership check doesn't break the normal own-task happy path."""
    from tools import kanban_tools as kt
    # Both implicit (no task_id arg) and explicit (matching env) must work.
    out = kt._handle_complete({"task_id": worker_env, "summary": "explicit own"})
    d = json.loads(out)
    assert d.get("ok") is True and d.get("task_id") == worker_env


def test_worker_complete_rejects_stale_run_id(worker_env, monkeypatch):
    """A retried worker cannot complete the task using an old run token."""
    from hermes_cli import kanban_db as kb
    import hermes_cli.kanban_db as _kb

    # detect_crashed_workers now gates each running task behind a
    # launch-window grace period (c002668ff) so a freshly-spawned worker
    # whose PID isn't yet visible on /proc isn't reclaimed. The fixture
    # creates the task moments before this assertion, so the grace
    # period (default 30s) would skip the liveness check. Zero it out
    # for this test — we WANT immediate reclamation here.
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    conn = kb.connect()
    try:
        run1 = kb.latest_run(conn, worker_env)
        kb._set_worker_pid(conn, worker_env, 98765)
        monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
        monkeypatch.setattr(_kb, "_pid_alive", lambda pid: False)
        assert kb.detect_crashed_workers(conn) == [worker_env]

        kb.claim_task(conn, worker_env)
        run2 = kb.latest_run(conn, worker_env)
        assert run2.id != run1.id
    finally:
        conn.close()

    from tools import kanban_tools as kt
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run1.id))
    out = kt._handle_complete({"summary": "late stale completion"})
    d = json.loads(out)
    assert d.get("ok") is not True

    conn = kb.connect()
    try:
        task = kb.get_task(conn, worker_env)
        assert task.status == "running"
        assert task.current_run_id == run2.id
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run2.id))
    out = kt._handle_complete({"summary": "current completion"})
    d = json.loads(out)
    assert d.get("ok") is True


def test_orchestrator_complete_any_task_allowed(monkeypatch, tmp_path):
    """Orchestrator profiles (no HERMES_KANBAN_TASK) can still complete
    any task via explicit task_id. The check only applies to workers."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from pathlib import Path as _P
    monkeypatch.setattr(_P, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="child to close out")
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        conn.commit()
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_complete({"task_id": tid, "summary": "orchestrator close"})
    d = json.loads(out)
    assert d.get("ok") is True and d.get("task_id") == tid


# ---------------------------------------------------------------------------
# Optional ``board`` parameter — per-call DB override
# ---------------------------------------------------------------------------
#
# The dispatcher pins the active board via HERMES_KANBAN_BOARD env var,
# but a Telegram-side orchestrator handling multiple boards needs to be
# able to route a single tool call to a specific board's DB without
# restarting Hermes. These tests pin that ``board=<slug>`` argument
# routes each handler to that board's sqlite file, and that omitting
# ``board`` preserves the legacy env-driven resolution.


@pytest.fixture
def multi_board_env(monkeypatch, tmp_path):
    """Isolated Hermes home with two distinct kanban boards seeded.

    Returns ``("default", "alt")`` slugs. The default board has one
    pre-existing task ``seed_default``; ``alt`` has ``seed_alt``. No
    HERMES_KANBAN_TASK is pinned (orchestrator context) — workers test
    the env-task case via the existing ``worker_env`` fixture.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Make sure neither HERMES_KANBAN_DB nor HERMES_KANBAN_BOARD pin a
    # board — the test is specifically about the per-call override.
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "test-orchestrator")
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    # Default board — implicit
    conn = kb.connect()
    try:
        seed_default = kb.create_task(
            conn, title="seed-default", assignee="worker-d"
        )
    finally:
        conn.close()
    # Alt board — explicit slug routes the connection to a separate DB
    conn = kb.connect(board="alt")
    try:
        seed_alt = kb.create_task(
            conn, title="seed-alt", assignee="worker-a"
        )
    finally:
        conn.close()
    return {
        "default_seed": seed_default,
        "alt_seed": seed_alt,
        "default_db": kb.kanban_db_path(),
        "alt_db": kb.kanban_db_path(board="alt"),
    }


def test_board_param_routes_create_to_alt_board(multi_board_env):
    """kanban_create with ``board="alt"`` must write into the alt board's DB,
    not the default one."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_create({
        "title": "alt-only",
        "assignee": "worker",
        "board": "alt",
    })
    d = json.loads(out)
    assert d["ok"] is True, d
    new_tid = d["task_id"]

    # Lands on alt board.
    with kb.connect(board="alt") as conn:
        assert kb.get_task(conn, new_tid).title == "alt-only"
    # Does NOT land on default board.
    with kb.connect() as conn:
        assert kb.get_task(conn, new_tid) is None


def test_board_param_routes_list_to_alt_board(multi_board_env):
    """kanban_list filters by the board parameter, not env-active."""
    from tools import kanban_tools as kt

    # Default — sees seed-default, not seed-alt.
    default_out = json.loads(kt._handle_list({}))
    default_titles = {t["title"] for t in default_out["tasks"]}
    assert "seed-default" in default_titles
    assert "seed-alt" not in default_titles

    # Alt — sees seed-alt, not seed-default.
    alt_out = json.loads(kt._handle_list({"board": "alt"}))
    alt_titles = {t["title"] for t in alt_out["tasks"]}
    assert "seed-alt" in alt_titles
    assert "seed-default" not in alt_titles


def test_board_param_routes_show_to_alt_board(multi_board_env):
    """kanban_show reads from the board parameter, not env-active.

    Tasks across boards may share ids (the id space is per-DB) but the
    seed task ids in this fixture are distinct, so a cross-board show
    must return the matching task only when board is correct.
    """
    from tools import kanban_tools as kt

    alt_seed = multi_board_env["alt_seed"]
    # Without board override, the alt task is invisible.
    bad = json.loads(kt._handle_show({"task_id": alt_seed}))
    assert "not found" in bad.get("error", "")

    # With board override, it's readable.
    good = json.loads(kt._handle_show({"task_id": alt_seed, "board": "alt"}))
    assert good["task"]["id"] == alt_seed
    assert good["task"]["title"] == "seed-alt"


def test_board_param_routes_assign_via_create_to_alt(multi_board_env):
    """Workflow test for the 'assign' UX — create with assignee on a
    specific board. (The CLI has a separate ``kanban assign`` verb; the
    MCP surface assigns at task creation time.)"""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_create({
        "title": "alt-assigned",
        "assignee": "linguist",
        "board": "alt",
    })
    d = json.loads(out)
    assert d["ok"] is True
    with kb.connect(board="alt") as conn:
        task = kb.get_task(conn, d["task_id"])
        assert task is not None
        assert task.assignee == "linguist"


def test_board_param_routes_comment_to_alt_board(multi_board_env):
    """kanban_comment routes the insert to the alt board's DB."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    alt_seed = multi_board_env["alt_seed"]
    out = kt._handle_comment({
        "task_id": alt_seed,
        "body": "alt comment",
        "board": "alt",
    })
    d = json.loads(out)
    assert d["ok"] is True

    with kb.connect(board="alt") as conn:
        comments = kb.list_comments(conn, alt_seed)
        assert len(comments) == 1
        assert comments[0].body == "alt comment"
    # Default board does not have this task at all, so no rogue comment.
    with kb.connect() as conn:
        assert kb.get_task(conn, alt_seed) is None


def test_board_param_routes_complete_to_alt_board(multi_board_env):
    """kanban_complete on the alt board closes the alt task, leaving
    the default seed untouched."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    alt_seed = multi_board_env["alt_seed"]
    # Make alt task running so complete is valid.
    with kb.connect(board="alt") as conn:
        kb.claim_task(conn, alt_seed)

    out = kt._handle_complete({
        "task_id": alt_seed,
        "summary": "alt close",
        "board": "alt",
    })
    d = json.loads(out)
    assert d["ok"] is True

    with kb.connect(board="alt") as conn:
        assert kb.get_task(conn, alt_seed).status == "done"
    # Default seed is unchanged.
    with kb.connect() as conn:
        default_seed = multi_board_env["default_seed"]
        assert kb.get_task(conn, default_seed).status == "ready"


def test_board_param_routes_block_to_alt_board(multi_board_env):
    """kanban_block targets the alt board's DB."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    alt_seed = multi_board_env["alt_seed"]
    with kb.connect(board="alt") as conn:
        kb.claim_task(conn, alt_seed)

    out = kt._handle_block({
        "task_id": alt_seed,
        "reason": "need input on alt board",
        "board": "alt",
    })
    d = json.loads(out)
    assert d["ok"] is True

    with kb.connect(board="alt") as conn:
        assert kb.get_task(conn, alt_seed).status == "blocked"


def test_board_param_routes_unblock_to_alt_board(multi_board_env):
    """kanban_unblock targets the alt board's DB."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    alt_seed = multi_board_env["alt_seed"]
    with kb.connect(board="alt") as conn:
        kb.block_task(conn, alt_seed, reason="waiting")
        assert kb.get_task(conn, alt_seed).status == "blocked"

    out = kt._handle_unblock({"task_id": alt_seed, "board": "alt"})
    d = json.loads(out)
    assert d["ok"] is True
    assert d["status"] == "ready"

    with kb.connect(board="alt") as conn:
        assert kb.get_task(conn, alt_seed).status == "ready"


def test_board_param_routes_heartbeat_to_alt_board(monkeypatch, tmp_path):
    """kanban_heartbeat targets the alt board's DB. Worker-scoped, so we
    use the worker-env style fixture inline (pinning HERMES_KANBAN_TASK
    to a task that exists in the alt board)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "alt-worker")
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    # Seed the alt board with a claimed task.
    with kb.connect(board="alt") as conn:
        tid = kb.create_task(conn, title="alt hb", assignee="alt-worker")
        kb.claim_task(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)

    from tools import kanban_tools as kt
    out = kt._handle_heartbeat({"note": "alive on alt", "board": "alt"})
    d = json.loads(out)
    assert d["ok"] is True

    # Heartbeat event landed in the alt DB.
    with kb.connect(board="alt") as conn:
        events = [e for e in kb.list_events(conn, tid) if e.kind == "heartbeat"]
        assert len(events) == 1


def test_board_param_routes_link_to_alt_board(multi_board_env):
    """kanban_link operates on the alt board's DB."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    with kb.connect(board="alt") as conn:
        a = kb.create_task(conn, title="A-alt", assignee="x")
        b = kb.create_task(conn, title="B-alt", assignee="x")

    out = kt._handle_link({
        "parent_id": a,
        "child_id": b,
        "board": "alt",
    })
    d = json.loads(out)
    assert d["ok"] is True

    with kb.connect(board="alt") as conn:
        assert b in kb.child_ids(conn, a)


def test_board_param_none_falls_back_to_env(worker_env):
    """When ``board`` is omitted or None, behaviour is unchanged from
    before this feature — calls land on whatever the env resolves to.
    Regression guard against accidentally rewiring default resolution."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_show({})  # no board, no task_id
    d = json.loads(out)
    assert d["task"]["id"] == worker_env

    out = kt._handle_show({"task_id": worker_env, "board": None})
    d = json.loads(out)
    assert d["task"]["id"] == worker_env

    # Sanity: the env-resolved path is the legacy default DB, NOT an
    # 'alt' board path. Confirms the override path was not silently
    # forced.
    assert kb.kanban_db_path() == kb.kanban_db_path(board="default")


def test_board_param_rejects_invalid_slug(multi_board_env):
    """A board slug that fails ``_normalize_board_slug`` surfaces as a
    structured tool_error rather than a 500 / unhandled exception."""
    from tools import kanban_tools as kt

    out = kt._handle_list({"board": "Has Spaces"})
    err = json.loads(out).get("error", "")
    assert "invalid board slug" in err, f"got {err!r}"


def test_board_param_in_all_schemas():
    """Every kanban_* tool schema must expose an optional ``board``
    parameter. This pins the contract surfaced to the LLM — adding a
    new kanban tool without ``board`` will fail CI immediately."""
    from tools import kanban_tools as kt

    schemas = [
        kt.KANBAN_SHOW_SCHEMA,
        kt.KANBAN_LIST_SCHEMA,
        kt.KANBAN_COMPLETE_SCHEMA,
        kt.KANBAN_BLOCK_SCHEMA,
        kt.KANBAN_HEARTBEAT_SCHEMA,
        kt.KANBAN_COMMENT_SCHEMA,
        kt.KANBAN_CREATE_SCHEMA,
        kt.KANBAN_UNBLOCK_SCHEMA,
        kt.KANBAN_LINK_SCHEMA,
        kt.KANBAN_ATTACH_SCHEMA,
        kt.KANBAN_ATTACH_URL_SCHEMA,
        kt.KANBAN_ATTACHMENTS_SCHEMA,
    ]
    for schema in schemas:
        props = schema["parameters"]["properties"]
        assert "board" in props, (
            f"{schema['name']} is missing the 'board' property"
        )
        assert props["board"]["type"] == "string"
        # board is optional everywhere — never in required.
        assert "board" not in schema["parameters"].get("required", []), (
            f"{schema['name']} marks board as required; must be optional"
        )


# ---------------------------------------------------------------------------
# kanban_create auto-subscribe behaviour
#
# When a worker calls kanban_create from inside a session that has a
# persistent delivery channel, the originating session should be
# subscribed to the new task's completion/block events automatically.
# - Gateway sessions: HERMES_SESSION_PLATFORM + HERMES_SESSION_CHAT_ID set.
# - TUI sessions: HERMES_SESSION_KEY (or HERMES_SESSION_ID) set, with
#   the platform/chat_id ContextVars intentionally empty.
# - CLI / cron / test sessions: no delivery channel -> no subscription.
# - Config gate kanban.auto_subscribe_on_create: false -> no subscription
#   even when the session has a delivery channel.
# ---------------------------------------------------------------------------

def _list_subs_for_task(task_id):
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        return list(kb.list_notify_subs(conn, task_id))
    finally:
        conn.close()


def _sub_index(subs):
    """Normalise a list of notify-subs (dicts or objects) into dicts
    keyed by platform+chat_id, so assertions work regardless of the
    return shape."""
    out = []
    for s in subs:
        if isinstance(s, dict):
            out.append(s)
        else:
            out.append({
                "platform": getattr(s, "platform", None),
                "chat_id": getattr(s, "chat_id", None),
                "thread_id": getattr(s, "thread_id", None),
                "user_id": getattr(s, "user_id", None),
            })
    return out


def test_create_subscribes_gateway_session(monkeypatch, worker_env):
    """A gateway session (platform + chat_id set) gets auto-subscribed
    to its own kanban_create result, and the response surfaces the
    ``subscribed`` flag so the orchestrator can react."""
    from tools import kanban_tools as kt
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "chat-42")
    monkeypatch.setenv("HERMES_SESSION_THREAD_ID", "thread-7")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "user-9")

    out = kt._handle_create({
        "title": "auto-sub gateway",
        "assignee": "peer",
    })
    d = json.loads(out)
    assert d["ok"] is True
    new_tid = d["task_id"]
    assert d["subscribed"] is True, d

    subs = _sub_index(_list_subs_for_task(new_tid))
    assert len(subs) == 1
    s = subs[0]
    assert s["platform"] == "telegram"
    assert s["chat_id"] == "chat-42"
    assert s["thread_id"] == "thread-7"
    assert s["user_id"] == "user-9"


def test_create_subscribes_tui_session_via_session_key(monkeypatch, worker_env):
    """TUI / desktop sessions don't have a platform/chat_id (single
    local channel), but the parent process exports HERMES_SESSION_KEY.
    We should still auto-subscribe, with platform='tui' and
    chat_id=<key>."""
    from tools import kanban_tools as kt
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_THREAD_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_USER_ID", raising=False)
    monkeypatch.setenv("HERMES_SESSION_KEY", "tui-session-abc")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)

    out = kt._handle_create({
        "title": "auto-sub tui",
        "assignee": "peer",
    })
    d = json.loads(out)
    assert d["ok"] is True
    new_tid = d["task_id"]
    assert d["subscribed"] is True, d

    subs = _sub_index(_list_subs_for_task(new_tid))
    assert len(subs) == 1
    assert subs[0]["platform"] == "tui"
    assert subs[0]["chat_id"] == "tui-session-abc"


def test_create_does_not_subscribe_in_cli_session(monkeypatch, worker_env):
    """CLI / cron / test sessions have no persistent delivery channel.
    _maybe_auto_subscribe returns False and no row is written."""
    from tools import kanban_tools as kt
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)

    out = kt._handle_create({
        "title": "no sub cli",
        "assignee": "peer",
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["subscribed"] is False, d

    assert _list_subs_for_task(d["task_id"]) == []


def test_create_respects_auto_subscribe_on_create_false(monkeypatch, worker_env, tmp_path):
    """The config gate kanban.auto_subscribe_on_create=false must
    suppress auto-subscription even when the session has a delivery
    channel. This is the knob that addresses the upstream design
    concern from PR #19718 (reverted in #19721) — users who want
    explicit kanban_notify-subscribe calls per task get that."""
    # worker_env already created <tmp>/.hermes; use a fresh sibling
    # home to avoid mkdir() colliding with the worker's directory.
    home = tmp_path / "gate-home" / ".hermes"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(
        "kanban:\n  auto_subscribe_on_create: false\n"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "channel-1")

    from tools import kanban_tools as kt
    out = kt._handle_create({
        "title": "no sub gated",
        "assignee": "peer",
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["subscribed"] is False, d

    assert _list_subs_for_task(d["task_id"]) == []


def test_create_partial_session_context_no_subscribe(monkeypatch, worker_env):
    """Only one of (platform, chat_id) set -> no implicit subscribe.
    Either both are set (gateway) or neither (TUI / CLI); partial is
    ambiguous and the safe default is to skip."""
    from tools import kanban_tools as kt
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "slack")
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)

    out = kt._handle_create({
        "title": "no sub partial",
        "assignee": "peer",
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["subscribed"] is False, d


def test_maybe_auto_subscribe_swallows_add_notify_sub_failure(monkeypatch, worker_env):
    """If add_notify_sub itself raises (e.g. DB locked, schema drift),
    _maybe_auto_subscribe must NOT bubble that up and fail the parent
    kanban_create. The function returns False and the parent create
    still succeeds with subscribed=False."""
    from tools import kanban_tools as kt
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "chat-42")

    from hermes_cli import kanban_db as kb

    def _boom(*a, **kw):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(kb, "add_notify_sub", _boom)

    out = kt._handle_create({
        "title": "auto-sub tolerates add_notify_sub failure",
        "assignee": "peer",
    })
    d = json.loads(out)
    assert d["ok"] is True, d
    assert d["subscribed"] is False, d


# ---------------------------------------------------------------------------
# Attachments — kanban_attach / kanban_attach_url / kanban_attachments
# ---------------------------------------------------------------------------


@pytest.fixture
def allow_private_urls(monkeypatch):
    """Opt the SSRF guard into private/loopback targets for local fixtures.

    Mirrors a user setting HERMES_ALLOW_PRIVATE_URLS on a private network.
    Resets the url_safety process-lifetime cache on both sides so the
    override neither leaks in nor out of the test.
    """
    from tools import url_safety

    monkeypatch.setenv("HERMES_ALLOW_PRIVATE_URLS", "true")
    url_safety._reset_allow_private_cache()
    yield
    url_safety._reset_allow_private_cache()


def test_attach_roundtrips_bytes_to_row_and_disk(worker_env):
    """kanban_attach decodes base64, writes the blob, and records the row."""
    import base64
    from pathlib import Path

    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    content = b"hello attachment from a tool"
    out = kt._handle_attach({
        "filename": "notes.txt",
        "content_base64": base64.b64encode(content).decode(),
        "content_type": "text/plain",
    })
    d = json.loads(out)
    assert d.get("ok") is True, out
    assert d["size"] == len(content)
    att_id = d["attachment_id"]

    conn = kb.connect()
    try:
        atts = kb.list_attachments(conn, worker_env)
        assert [a.filename for a in atts] == ["notes.txt"]
        a = atts[0]
        assert a.id == att_id
        assert a.content_type == "text/plain"
        assert a.uploaded_by == "agent"
        # Blob is on disk under the task's attachments dir with the bytes.
        assert Path(a.stored_path).read_bytes() == content
        assert Path(a.stored_path).resolve().is_relative_to(
            kb.task_attachments_dir(worker_env).resolve()
        )
    finally:
        conn.close()


def test_attach_rejects_oversize(worker_env, monkeypatch):
    """A decoded payload over the cap returns a clean tool error, no row."""
    import base64

    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    # Shrink the cap so we don't have to build a 25 MB payload.
    monkeypatch.setattr(kb, "KANBAN_ATTACHMENT_MAX_BYTES", 8)
    out = kt._handle_attach({
        "filename": "big.bin",
        "content_base64": base64.b64encode(b"0123456789").decode(),
    })
    d = json.loads(out)
    assert "error" in d
    assert "MB limit" in d["error"]

    conn = kb.connect()
    try:
        assert kb.list_attachments(conn, worker_env) == []
    finally:
        conn.close()


def test_attach_rejects_bad_base64(worker_env):
    from tools import kanban_tools as kt

    out = kt._handle_attach({"filename": "x.txt", "content_base64": "not base64!!!"})
    d = json.loads(out)
    assert "error" in d and "base64" in d["error"]


def test_attach_requires_filename_and_content(worker_env):
    from tools import kanban_tools as kt

    assert "error" in json.loads(kt._handle_attach({"content_base64": "QQ=="}))
    assert "error" in json.loads(kt._handle_attach({"filename": "x.txt"}))


def test_attach_enforces_worker_task_ownership(worker_env):
    """A worker scoped to its own task can't attach to a foreign task."""
    import base64

    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="someone else's task", assignee="peer")
    finally:
        conn.close()

    out = kt._handle_attach({
        "task_id": other,
        "filename": "x.txt",
        "content_base64": base64.b64encode(b"x").decode(),
    })
    d = json.loads(out)
    assert "error" in d
    assert "scoped to task" in d["error"]


def test_attachments_lists_uploaded_files(worker_env):
    import base64

    from tools import kanban_tools as kt

    kt._handle_attach({
        "filename": "a.txt",
        "content_base64": base64.b64encode(b"aaa").decode(),
    })
    kt._handle_attach({
        "filename": "b.txt",
        "content_base64": base64.b64encode(b"bbbb").decode(),
    })
    out = kt._handle_attachments({})
    d = json.loads(out)
    assert d.get("ok") is True
    names = sorted(a["filename"] for a in d["attachments"])
    assert names == ["a.txt", "b.txt"]
    sizes = {a["filename"]: a["size"] for a in d["attachments"]}
    assert sizes == {"a.txt": 3, "b.txt": 4}


def test_attachments_unknown_task_errors(worker_env):
    from tools import kanban_tools as kt

    out = kt._handle_attachments({"task_id": "t_nope"})
    assert "error" in json.loads(out)


def test_attach_url_fetches_local_fixture(worker_env, allow_private_urls):
    """kanban_attach_url downloads from an http(s) URL and stores the bytes.

    The fixture server lives on loopback, which the SSRF guard blocks by
    default — opted in via the allow_private_urls fixture exactly like a
    user on a private network would.
    """
    import http.server
    import threading
    from pathlib import Path

    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    payload = b"downloaded-by-url body"

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):  # silence
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        out = kt._handle_attach_url({
            "url": f"http://127.0.0.1:{port}/files/report.bin",
        })
    finally:
        srv.shutdown()
    d = json.loads(out)
    assert d.get("ok") is True, out
    assert d["size"] == len(payload)

    conn = kb.connect()
    try:
        atts = kb.list_attachments(conn, worker_env)
        # Filename derived from the URL path leaf.
        assert atts[0].filename == "report.bin"
        assert Path(atts[0].stored_path).read_bytes() == payload
    finally:
        conn.close()


def test_attach_url_rejects_oversize_stream(worker_env, monkeypatch, allow_private_urls):
    """An oversize response body is rejected during download, no row written."""
    import http.server
    import threading

    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    big = b"x" * (64 * 1024)

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(big)))
            self.end_headers()
            self.wfile.write(big)

        def log_message(self, *a):
            pass

    monkeypatch.setattr(kb, "KANBAN_ATTACHMENT_MAX_BYTES", 1024)
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        out = kt._handle_attach_url({"url": f"http://127.0.0.1:{port}/big.bin"})
    finally:
        srv.shutdown()
    d = json.loads(out)
    assert "error" in d
    assert "MB limit" in d["error"]

    conn = kb.connect()
    try:
        assert kb.list_attachments(conn, worker_env) == []
    finally:
        conn.close()


def test_attach_url_rejects_non_http_scheme(worker_env):
    from tools import kanban_tools as kt

    out = kt._handle_attach_url({"url": "file:///etc/passwd"})
    d = json.loads(out)
    assert "error" in d
    assert "scheme" in d["error"]


# ---------------------------------------------------------------------------
# kanban_attach_url — SSRF guard (tools/url_safety.is_safe_url per hop)
# ---------------------------------------------------------------------------


@pytest.fixture
def default_url_guard(monkeypatch):
    """Force the SSRF guard to its secure default for this test.

    Clears HERMES_ALLOW_PRIVATE_URLS and resets url_safety's process-lifetime
    cache on both sides so a prior test's opt-in can't leak in.
    """
    from tools import url_safety

    monkeypatch.delenv("HERMES_ALLOW_PRIVATE_URLS", raising=False)
    url_safety._reset_allow_private_cache()
    yield
    url_safety._reset_allow_private_cache()


def _assert_attach_url_blocked(worker_env, url):
    """Call kanban_attach_url with ``url`` and assert the SSRF guard fired
    (clean tool error, no attachment row, no network fetch needed)."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_attach_url({"url": url})
    d = json.loads(out)
    assert "error" in d, out
    assert "SSRF" in d["error"] or "blocked" in d["error"].lower(), out
    conn = kb.connect()
    try:
        assert kb.list_attachments(conn, worker_env) == []
    finally:
        conn.close()


def test_attach_url_blocks_loopback(worker_env, default_url_guard):
    """http://127.0.0.1/ is rejected before any connection is made."""
    _assert_attach_url_blocked(worker_env, "http://127.0.0.1/")


def test_attach_url_blocks_cloud_metadata(worker_env, default_url_guard):
    """The cloud metadata endpoint is rejected — the #1 SSRF target."""
    _assert_attach_url_blocked(
        worker_env, "http://169.254.169.254/latest/meta-data/"
    )


def test_attach_url_blocks_private_range(worker_env, default_url_guard):
    """RFC1918 addresses (http://10.0.0.1/) are rejected."""
    _assert_attach_url_blocked(worker_env, "http://10.0.0.1/")


def _fake_public_dns(monkeypatch, mapping):
    """Patch url_safety's getaddrinfo so hostnames in ``mapping`` resolve to
    the given (public) IPs and literal IPs resolve to themselves — no real
    DNS or network traffic."""
    import ipaddress
    import socket as _socket

    real_af, real_sock = _socket.AF_INET, _socket.SOCK_STREAM

    def fake_getaddrinfo(host, *args, **kwargs):
        ip = mapping.get(host)
        if ip is None:
            # Literal IPs pass through; unknown hostnames fail like NXDOMAIN.
            try:
                ipaddress.ip_address(host)
            except ValueError:
                raise _socket.gaierror(f"fake DNS: unknown host {host!r}")
            ip = host
        return [(real_af, real_sock, 6, "", (ip, 0))]

    from tools import url_safety
    monkeypatch.setattr(url_safety.socket, "getaddrinfo", fake_getaddrinfo)


class _FakeStreamResponse:
    def __init__(self, *, status_code=200, headers=None, body=b""):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body

    @property
    def is_redirect(self):
        return 300 <= self.status_code < 400 and "location" in {
            k.lower() for k in self.headers
        }

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_bytes(self, chunk_size):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_attach_url_blocks_redirect_to_loopback(worker_env, default_url_guard, monkeypatch):
    """A public host 302ing to loopback is caught on the redirect hop.

    The pre-flight check passes (public IP), then the mocked response
    redirects to http://127.0.0.1/ — the guard must re-validate the
    Location target and refuse to follow it.
    """
    import httpx

    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    _fake_public_dns(monkeypatch, {"files.example.com": "93.184.216.34"})

    requested = []

    def fake_stream(method, url, **kwargs):
        requested.append(url)
        assert kwargs.get("follow_redirects") is False
        return _FakeStreamResponse(
            status_code=302,
            headers={"location": "http://127.0.0.1/latest/secrets"},
        )

    monkeypatch.setattr(httpx, "stream", fake_stream)

    out = kt._handle_attach_url({"url": "http://files.example.com/report.pdf"})
    d = json.loads(out)
    assert "error" in d, out
    assert "127.0.0.1" in d["error"], out
    # Only the public hop was ever fetched; the loopback target never was.
    assert requested == ["http://files.example.com/report.pdf"]

    conn = kb.connect()
    try:
        assert kb.list_attachments(conn, worker_env) == []
    finally:
        conn.close()


def test_attach_url_happy_path_public_host(worker_env, default_url_guard, monkeypatch):
    """A public URL passes the guard and the bytes are stored (mocked fetch)."""
    from pathlib import Path

    import httpx

    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    _fake_public_dns(monkeypatch, {"files.example.com": "93.184.216.34"})

    payload = b"public fetch body"

    def fake_stream(method, url, **kwargs):
        assert url == "http://files.example.com/docs/spec.pdf"
        return _FakeStreamResponse(
            status_code=200,
            headers={"content-type": "application/pdf; charset=binary"},
            body=payload,
        )

    monkeypatch.setattr(httpx, "stream", fake_stream)

    out = kt._handle_attach_url({"url": "http://files.example.com/docs/spec.pdf"})
    d = json.loads(out)
    assert d.get("ok") is True, out
    assert d["size"] == len(payload)

    conn = kb.connect()
    try:
        atts = kb.list_attachments(conn, worker_env)
        assert [a.filename for a in atts] == ["spec.pdf"]
        assert atts[0].content_type == "application/pdf"
        assert Path(atts[0].stored_path).read_bytes() == payload
    finally:
        conn.close()


def test_block_rejected_for_coder_defective_acceptance_criterion(monkeypatch, worker_env):
    """F37: the repo coder may not escalate a defective ABNAHMEKRITERIUM to a
    human. Live probe card t_82eac595 ('Tests fuer die Mandanten-Farben',
    erwartung=done): the coder read the public API as
    ``tenantChartColor(name, mode: "light" | "dark")`` but the specifier's
    criterion demanded Boolean args, so building was impossible. It blocked
    needs_input with 'HALT: Specifier-Abnahmekriterium nutzt Boolean-Modi ...
    bitte Kriterium technisch korrigieren und Karte erneut an den Coder geben.'
    - no '?' anywhere, so the beobachter extracts no frage and classes the
    question-less blocked done-card as rot-infra-eskalation-ohne-frage. A wrong
    criterion is the SPECIFIER's to fix, re-handed to the coder - a
    factory-internal loop, never Till's. The gate rejects the block and steers
    the coder to kanban_handoff(to="specifier")."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="Tests fuer die Mandanten-Farben",
            assignee="voicera-coder", repository="voicera",
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "voicera-coder")
    from tools import kanban_tools as kt
    out = kt._handle_block({
        "reason": ("HALT: Specifier-Abnahmekriterium nutzt Boolean-Modi, die "
                   "oeffentliche API erwartet \"light\" | \"dark\"; bitte "
                   "Kriterium technisch korrigieren und Karte erneut an den "
                   "Coder geben."),
        "kind": "needs_input",
    })
    d = json.loads(out)
    assert "error" in d
    assert "acceptance criterion" in d["error"]
    assert "kanban_handoff(to=\"specifier\")" in d["error"]
    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


def test_block_rejected_for_coder_criterion_defect_in_comment(monkeypatch, worker_env):
    """F37: the gate also scans comments (like the beobachter's _letzte_frage),
    so a coder cannot move the 'criterion is wrong' finding into a comment and
    slip a terse reason past the gate."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="Tests fuer die Mandanten-Farben",
            assignee="voicera-coder", repository="voicera",
        )
        kb.add_comment(
            conn, tid, author="voicera-coder",
            body=("Das Abnahmekriterium muss auf tenantChartColor(\"Acme\", "
                  "\"light\") != tenantChartColor(\"Acme\", \"dark\") korrigiert "
                  "werden, bevor gebaut wird."),
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "voicera-coder")
    from tools import kanban_tools as kt
    out = kt._handle_block({"reason": "siehe Kommentar oben", "kind": "needs_input"})
    d = json.loads(out)
    assert "error" in d
    assert "acceptance criterion" in d["error"]
    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


def test_block_allowed_for_coder_business_wall_mentioning_criterion(monkeypatch, worker_env):
    """F37 must not swallow a genuine business block that merely mentions the
    word 'Kriterium' without framing it as defective. A real Freigabe question
    still passes to the beobachter's richter."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="coder hit a wall",
            assignee="voicera-coder", repository="voicera",
        )
        kb.add_comment(
            conn, tid, author="voicera-coder",
            body=("Alle Kriterien erfuellt, aber brauche Klaerung: soll ich "
                  "nach main freigeben?"),
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "voicera-coder")
    from tools import kanban_tools as kt
    out = kt._handle_block({"reason": "need a decision from Till", "kind": "needs_input"})
    d = json.loads(out)
    assert d.get("ok") is True


# ---------------------------------------------------------------------------
# F21: a DB-less backend pytest targeting the importable app package is
# env-gated in the Beobachter venv (collection boots app.database's async
# engine, which the check venv can't provision) → reviewer routes it to the
# merger, not a doomed self-`done`.
# ---------------------------------------------------------------------------

def test_card_is_build_gated_db_less_backend_pytest():
    """The live probe t_cdc32d75: a reviewed DB-less unit test for
    `_check_priority` in `backend/app/services/todo_service.py`. The test uses
    no DB fixture and passes 4/4 for coder and reviewer, but the Beobachter's
    `cd backend && pytest tests/test_todo_priority.py` dies at COLLECTION — the
    import chain reaches `app/database.py` which calls `create_async_engine(
    settings.DATABASE_URL)` at import, and the check venv has no aiosqlite / real
    DATABASE_URL. A pure env gap; must route to the merger like F31."""
    from tools import kanban_tools as kt
    title = "Unit-Test fuer die Prioritaetspruefung im To-Do-Service ohne Datenbank"
    body = (
        "_check_priority in backend/app/services/todo_service.py wird nur im "
        "Postgres-Test mitgeprueft. Reiner Unit-Test fuer PRIORITY_MIN/"
        "PRIORITY_MAX. Keine DB-Fixture."
    )
    comments = [
        "ABNAHMEKRITERIUM: Aus `backend/` beendet `.venv/bin/python -m pytest "
        "tests/test_todo_priority.py -q` ohne laufende Datenbank und ohne "
        "DB-Fixture mit genau vier Grenzfaellen.",
    ]
    assert kt._card_is_build_gated(title, body, comments) is True


def test_card_not_build_gated_for_static_helper_pytest():
    """A truly static pytest card (pure helper, no app-package import, no
    DB framing) must keep the honest Regel-a `done` path — all three F21 signals
    are required."""
    from tools import kanban_tools as kt
    assert kt._card_is_build_gated(
        "Unit-Test fuer String-Helfer",
        "Teste reine Funktion in backend/app_utils/strcase.py.",
        ["ABNAHMEKRITERIUM: pytest tests/test_strcase.py -q -> 3 passed"],
    ) is False


# ---------------------------------------------------------------------------
# F49: the full/backend acceptance is container/integration-gated and the factory
# admits it could not verify it read-only (no docker → `make test` aborts at the
# backend step, Exit 127). The Beobachter's `cd backend && pytest` acceptance dies
# the same way, so Regel a stays ROT — route to the merger like F21/F30/F31.
# ---------------------------------------------------------------------------

def test_card_is_build_gated_docker_integration_gap():
    """The live probe t_d5ca2c82 (Karte 19 recurrence): the factory built a real
    fix but the full `make test` could not run because the worker has no docker
    binary, and the reviewer admits the same — a self-`done` is 'done ohne
    vollständige Abnahme'. Must route to the merger."""
    from tools import kanban_tools as kt
    title = "S3-Backup: Streu-Tag </content> aus SUMMARY/PLAN entfernen"
    body = (
        "Generierungs-Artefakt in SUMMARY/PLAN. Technik-Entscheidung (selbst "
        "treffen, im Kommentar begruenden): per Regex, Parser oder Prompt."
    )
    comments = [
        "Offen/unverifiziert: der vollstaendige `make test` stoppt am "
        "Backend-Schritt, weil kein docker-Binary vorhanden ist "
        "(`make: docker: No such file or directory`, Exit 127).",
        "Docker fehlt auf dem Reviewer-Worker, daher kein vollstaendiges "
        "make test; nur der fokussierte Gate-Lauf wurde reproduziert.",
    ]
    assert kt._card_is_build_gated(title, body, comments) is True


def test_card_not_build_gated_for_incidental_docker_mention():
    """A card that merely names docker/make-test in passing, with every test green
    and no unavailability admission, keeps the honest Regel-a `done` path — both
    F49 signals are required."""
    from tools import kanban_tools as kt
    assert kt._card_is_build_gated(
        "Doku: Entwickler-Setup",
        "Beschreibe wie man `docker compose up` und `make test` lokal startet.",
        ["REVIEW GRUEN: Doku ergaenzt, alle Tests gruen, make test 12 passed"],
    ) is False


def test_reviewer_routes_docker_gated_card_to_merger(monkeypatch, worker_env):
    """End-to-end (F49): the reviewer completing the container/integration-gated
    card hands the branch to `ready @ merger` instead of a doomed self-`done`."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="S3-Backup: Streu-Tag </content> aus SUMMARY/PLAN entfernen",
            body=(
                "Generierungs-Artefakt in SUMMARY/PLAN. Technik-Entscheidung "
                "(selbst treffen, im Kommentar begruenden)."
            ),
            assignee="voicera-reviewer", repository="voicera",
            workspace_kind="worktree", branch_name="wt/review",
        )
        kb.add_comment(
            conn, tid, author="voicera-coder",
            body=(
                "Offen/unverifiziert: der vollstaendige `make test` stoppt am "
                "Backend-Schritt, weil kein docker-Binary vorhanden ist "
                "(`make: docker: No such file or directory`, Exit 127)."
            ),
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "voicera-reviewer")
    from tools import kanban_tools as kt
    out = kt._handle_complete({"summary": "REVIEW GRUEN - fokussierter Gate-Lauf 9 passed"})
    d = json.loads(out)
    assert d.get("ok") is True
    assert d.get("assignee") == "merger"
    assert d.get("status") == "ready"
    conn = kb.connect()
    try:
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.assignee == "merger"
    finally:
        conn.close()


def test_reviewer_routes_db_less_backend_pytest_to_merger(monkeypatch, worker_env):
    """End-to-end: the reviewer completing the env-gated DB-less pytest card does
    not land `done` (which would be shell=ROT every re-check); it hands the
    branch to `ready @ merger` with a Merge-GO Freigabe note."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="Unit-Test fuer die Prioritaetspruefung ohne Datenbank",
            body=(
                "_check_priority in backend/app/services/todo_service.py. "
                "Reiner Unit-Test. Keine DB-Fixture."
            ),
            assignee="voicera-reviewer", repository="voicera",
            workspace_kind="worktree", branch_name="wt/review",
        )
        kb.add_comment(
            conn, tid, author="specifier",
            body=(
                "ABNAHMEKRITERIUM: Aus `backend/` `.venv/bin/python -m pytest "
                "tests/test_todo_priority.py -q` ohne laufende Datenbank und "
                "ohne DB-Fixture, vier Grenzfaelle."
            ),
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "voicera-reviewer")
    from tools import kanban_tools as kt
    out = kt._handle_complete({"summary": "REVIEW GRUEN - 4 passed, diff nur Testdatei"})
    d = json.loads(out)
    assert d.get("ok") is True
    assert d.get("assignee") == "merger"
    assert d.get("status") == "ready"
    conn = kb.connect()
    try:
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.assignee == "merger"
    finally:
        conn.close()


def test_completion_is_premise_noop_detects_historical_noop():
    """F47: the predicate fires only when a no-op delivery AND the premise it
    rests on (already fixed / producer not found) are both present — mirroring
    the Beobachter's exact rejection of Karte 19 (t_68d0de26)."""
    from tools import kanban_tools as kt
    coder = (
        "Ergebnis: erfüllt als belegter No-op. `git status --porcelain` belegen "
        "exakt 0 geänderte Dateien. Metadaten: `changed_files=[]`. im Repo "
        "existiert kein weiterer PLAN-/SUMMARY-Writer."
    )
    reviewer = "REVIEW GRÜN (belegter No-op)\nBetroffene Datei-Liste: `[]` (No-op)."
    orch = "Diese Karte wird als bereits historisch behoben geschlossen."
    # Positive: full thread, and each factory comment on its own.
    assert kt._completion_is_premise_noop([orch, coder, reviewer], "", "", {"changed_files": []}) is True
    assert kt._completion_is_premise_noop([coder], "", "", None) is True
    assert kt._completion_is_premise_noop([reviewer], "", "", None) is True
    # Negative: a real diff with a regression test keeps the honest done path.
    real = (
        "REVIEW GRÜN. Betroffene Dateien: backend/app/services/foo.py, "
        "tests/test_foo.py. Fix implementiert, Regressionstest ergänzt, pytest 12/12."
    )
    assert kt._completion_is_premise_noop([real], "", "", {"changed_files": ["backend/app/services/foo.py"]}) is False
    # Negative: no-op wording without the premise, or premise with a real diff.
    assert kt._completion_is_premise_noop(["changed_files=[]"], "", "", None) is False
    assert kt._completion_is_premise_noop(
        ["kein Repo-Writer gefunden, daher Fix am Aufrufer; 3 Dateien geändert"],
        "", "", {"changed_files": ["a.py"]},
    ) is False


def test_complete_rejected_for_premise_noop_card(monkeypatch, worker_env):
    """F47 e2e: the reviewer completing a repo work card as a historical no-op
    (0 changed files, 'bereits historisch behoben', 'kein Repo-Writer gefunden')
    is rejected — a 0-file `done` is 'done ohne vollständige Abnahme'. The reviewer
    must escalate a prepared premise-Zielfrage to blocked@till instead. The card
    stays in-flight (running), never slips to done."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="S3-Backup: Streu-Tag </content> aus SUMMARY/PLAN entfernen",
            body=(
                "Der Erzeuger schreibt ein Streu-Tag in die Artefakte. Fix im "
                "Generator, Regressionstest am Beispielartefakt."
            ),
            assignee="voicera-reviewer", repository="voicera",
            workspace_kind="worktree", branch_name="wt/review",
        )
        kb.add_comment(
            conn, tid, author="orchestrator",
            body="Diese Karte wird als bereits historisch behoben geschlossen.",
        )
        kb.add_comment(
            conn, tid, author="voicera-coder",
            body=(
                "Ergebnis: belegter No-op, exakt 0 geänderte Dateien, "
                "changed_files=[]. Kein Repo-Writer gefunden."
            ),
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_PROFILE", "voicera-reviewer")
    from tools import kanban_tools as kt
    out = kt._handle_complete({"summary": "REVIEW GRÜN (belegter No-op)"})
    d = json.loads(out)
    assert "error" in d
    assert "NO-OP" in d["error"]
    assert "kanban_block" in d["error"]
    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()
