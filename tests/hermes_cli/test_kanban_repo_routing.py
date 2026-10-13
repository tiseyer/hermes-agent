"""Regression tests: repo declaration beats tenant default in routing.

Goal decompose-repo-routing (Aug 2026): a card that explicitly declares
its target repository (e.g. "Repository: hermes-agent") must route its
decomposed children, review handoffs, and loop-brake diagnosis to the
declared repo's role profiles — not the tenant defaults. The reverse
direction (fbe14b734c: wrong-tenant profile on an undeclared card is
rewritten to the tenant pair) must keep working.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from tests.support.kanban_routing import (
    CANONICAL_TENANT_REVIEWERS,
    profiles_with_override as _profiles_with_override,
    seed_canonical,
    write_kanban_routing_config as _write_kanban_config,
)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Routing is operator config now; seed the canonical six so the behavioral
    # assertions below exercise exactly what the hardcoded registry used to
    # provide. The fail-closed empty default lives in
    # test_kanban_repo_profiles_isolation.py.
    seed_canonical(home)
    kb.init_db()
    return home


@pytest.fixture
def role_profiles_exist(monkeypatch):
    from hermes_cli import profiles
    monkeypatch.setattr(
        profiles, "profile_exists",
        lambda name: name in (
            "hermes-coder", "hermes-reviewer",
            "voicera-coder", "voicera-reviewer",
            "goya-coder", "goya-reviewer",
            "orchestrator",
        ),
    )


HERMES_DECL_BODY = (
    "Bitte im Framework fixen.\n"
    "Repository: Hermes-Agent-Framework /home/till/.hermes/hermes-agent, "
    "Profile hermes-coder/hermes-reviewer\n"
    "Details siehe unten."
)


# ---------------------------------------------------------------------------
# resolve_repo_profiles unit behavior
# ---------------------------------------------------------------------------

def test_resolver_declaration_beats_tenant(kanban_home, role_profiles_exist):
    resolved = kb.resolve_repo_profiles(HERMES_DECL_BODY, "voicera")
    assert resolved == ("hermes-coder", "hermes-reviewer", "declared")


def test_resolver_repo_line_without_profiles(kanban_home, role_profiles_exist):
    resolved = kb.resolve_repo_profiles(
        "Titel\nRepo: hermes-agent\nmehr text", "voicera",
    )
    assert resolved == ("hermes-coder", "hermes-reviewer", "declared")


def test_repository_profile_supplies_hermes_merge_target_and_base_ref(
    kanban_home, role_profiles_exist,
):
    """A declared framework card must use the fork's main ref end to end."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Framework-Fix",
            body="Repository: hermes-agent",
            tenant="voicera",
        )

        profile = kb.resolve_task_repository_profile(conn, task_id)

    assert profile is not None
    assert profile.name == "hermes"
    assert profile.base_ref == "fork/main"
    assert profile.merge_target == "main"


def test_worker_context_surfaces_repository_merge_target_for_merger(
    kanban_home, role_profiles_exist,
):
    """The merger receives the target from the card profile, not a default."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Framework-Fix",
            body="Repository: hermes-agent",
            assignee="merger",
            tenant="voicera",
        )

        context = kb.build_worker_context(conn, task_id)

    assert (
        "Repository profile: hermes (base ref: fork/main; "
        "merge target: main; push target: fork)"
    ) in context
    assert "merge target: develop" not in context


# ---------------------------------------------------------------------------
# Paket 4 (P08): push_target as a third profile field + full repo coverage.
# ---------------------------------------------------------------------------

# (repo signature, base_ref, merge_target, push_target)
_PROFILE_ROUTING_EXPECTATIONS = [
    ("hermes-agent", "hermes", "fork/main", "main", "fork"),
    ("voicera-os", "voicera", "origin/develop", "develop", "origin"),
    ("goya-v2", "goya", "origin/develop", "develop", "origin"),
    ("ams-erp", "ams-erp", "origin/main", "main", "origin"),
    ("voicera-website", "voicera-website", "origin/main", "main", "origin"),
    ("life-os", "life-os", "origin/main", "main", "origin"),
]


@pytest.mark.parametrize(
    "signature,name,base_ref,merge_target,push_target",
    _PROFILE_ROUTING_EXPECTATIONS,
)
def test_repository_profile_routing_matrix(
    kanban_home, signature, name, base_ref, merge_target, push_target,
):
    """Every configured repo resolves base_ref, merge_target and push_target.

    Paket-4 requirement 5: the full profile x field matrix, incl. the
    ams-erp entry (no develop) and voicera-website (production, main-based).
    """
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="repo-routing card",
            body=f"Repository: {signature}",
            tenant=None,
        )
        profile = kb.resolve_task_repository_profile(conn, task_id)

    assert profile is not None
    assert profile.name == name
    assert profile.base_ref == base_ref
    assert profile.merge_target == merge_target
    assert profile.push_target == push_target
    # No repo silently keeps the old hardcoded develop assumption.
    if name in ("ams-erp", "voicera-website", "hermes"):
        assert profile.merge_target != "develop"


@pytest.mark.parametrize(
    "signature,name,base_ref,merge_target,push_target",
    _PROFILE_ROUTING_EXPECTATIONS,
)
def test_worker_context_surfaces_push_target(
    kanban_home, signature, name, base_ref, merge_target, push_target,
):
    """The merger reads base ref, merge target AND push target from the
    profile line in its worker context — not from a hardcoded default."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="merge this",
            body=f"Repository: {signature}",
            assignee="merger",
            tenant=None,
        )
        context = kb.build_worker_context(conn, task_id)

    assert (
        f"Repository profile: {name} (base ref: {base_ref}; "
        f"merge target: {merge_target}; push target: {push_target})"
    ) in context


def test_ams_erp_uses_main_never_develop(kanban_home):
    """ams-erp is main-based: merge target main, push origin, no develop.

    Verified 2026-09-24 via ls-remote git@github-ams-erp:tiseyer/ams-erp.git
    (only refs/heads/main + feature branches; no develop).
    """
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="ERP fix", body="Repository: ams-erp", tenant=None,
        )
        profile = kb.resolve_task_repository_profile(conn, task_id)
    assert profile is not None
    assert (profile.base_ref, profile.merge_target, profile.push_target) == (
        "origin/main", "main", "origin",
    )


def test_repository_profile_missing_push_target_fails_closed(
    kanban_home, role_profiles_exist,
):
    """An empty push_target is a fail-closed config error, like base_ref."""
    _write_kanban_config(
        kanban_home,
        _profiles_with_override("hermes", push_target=""),
        CANONICAL_TENANT_REVIEWERS,
    )
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="Framework-Fix", body="Repository: hermes-agent",
            tenant="voicera",
        )
        with pytest.raises(kb.RepositoryProfileError, match="push_target"):
            kb.resolve_task_repository_profile(conn, task_id)
        context = kb.build_worker_context(conn, task_id)
    assert "Repository profile: INVALID" in context
    assert "missing required push_target" in context


def test_all_registered_profiles_have_push_target(kanban_home):
    """Every profile in the configured registry carries a push_target."""
    profiles = kb._repository_profiles()
    assert profiles, "canonical profiles should be configured in this fixture"
    for name, profile in profiles.items():
        assert profile.push_target.strip(), f"{name} missing push_target"


def test_repository_profile_missing_required_field_fails_closed(
    kanban_home, role_profiles_exist,
):
    _write_kanban_config(
        kanban_home,
        _profiles_with_override("hermes", base_ref=""),
        CANONICAL_TENANT_REVIEWERS,
    )
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Framework-Fix",
            body="Repository: hermes-agent",
            tenant="voicera",
        )

        with pytest.raises(kb.RepositoryProfileError, match="base_ref"):
            kb.resolve_task_repository_profile(conn, task_id)
        context = kb.build_worker_context(conn, task_id)

    assert "Repository profile: INVALID" in context
    assert "missing required base_ref" in context


def test_dispatch_blocks_for_missing_repository_profile_field(
    kanban_home, role_profiles_exist, tmp_path,
):
    """A malformed declared profile is needs_input, never a retry/default."""
    _write_kanban_config(
        kanban_home,
        _profiles_with_override("hermes", merge_target=""),
        CANONICAL_TENANT_REVIEWERS,
    )
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Framework-Fix",
            body="Repository: hermes-agent",
            assignee="hermes-coder",
            tenant="voicera",
            workspace_kind="worktree",
            workspace_path=str(tmp_path / "not-needed"),
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))

        result = kb.dispatch_once(conn, spawn_fn=lambda *_args, **_kwargs: 1)
        task = kb.get_task(conn, task_id)

    assert task is not None
    assert task.status == "blocked"
    assert task.block_kind == "needs_input"
    assert task.consecutive_failures == 0
    assert task_id in result.auto_blocked


def test_resolver_no_declaration_falls_to_tenant(kanban_home, role_profiles_exist):
    resolved = kb.resolve_repo_profiles(
        "Fix den Footer im Login-Screen", "voicera",
    )
    assert resolved == ("voicera-coder", "voicera-reviewer", "tenant")


def test_resolver_prose_mention_is_not_a_declaration(kanban_home, role_profiles_exist):
    # A mid-prose mention of another repo must NOT reroute the card.
    resolved = kb.resolve_repo_profiles(
        "Der Bug ähnelt einem im hermes-agent Repo, aber hier geht es um "
        "das Voicera-Frontend.", "voicera",
    )
    assert resolved == ("voicera-coder", "voicera-reviewer", "tenant")


def test_resolver_unknown_everything_returns_none(kanban_home, role_profiles_exist):
    assert kb.resolve_repo_profiles("nur prosa", None) is None
    assert kb.resolve_repo_profiles(None, "unknown-tenant") is None


def test_task_without_repository_or_legacy_declaration_fails_closed(
    kanban_home, role_profiles_exist,
):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="Undeclared task")
        assert kb.resolve_task_repository_profile(conn, task_id) is None


# ---------------------------------------------------------------------------
# Condition 1: decompose honors the root's repo declaration
# ---------------------------------------------------------------------------

def test_redecompose_inherits_structured_repository_to_children(
    kanban_home, role_profiles_exist,
):
    """The durable repository field survives LLM bodies that omit it."""
    with kb.connect() as conn:
        root = kb.create_task(
            conn,
            title="Framework-Bug fixen",
            body="Die Re-Decomposition darf keine Repo-Zeile verlieren.",
            repository="hermes",
            tenant="voicera",
            triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn, root, root_assignee="orchestrator",
            children=[
                # The decomposer LLM guessed the tenant pair — the root's
                # structured declaration must override it.
                {"title": "code it", "assignee": "voicera-coder", "parents": []},
                {"title": "review it", "assignee": "voicera-reviewer",
                 "parents": [0]},
            ],
        )
        assert child_ids
        for child_id in child_ids:
            child = kb.get_task(conn, child_id)
            assert child.repository == "hermes"
            profile = kb.resolve_task_repository_profile(conn, child_id)
            assert profile is not None
            assert profile.name == "hermes"
            assert profile.base_ref == "fork/main"
        assert kb.get_task(conn, child_ids[0]).assignee == "hermes-coder"
        assert kb.get_task(conn, child_ids[1]).assignee == "hermes-reviewer"


def test_review_block_honors_structured_repository_on_redecomposed_child(
    kanban_home, role_profiles_exist,
):
    """Review routing must use the inherited field after the body lost it."""
    with kb.connect() as conn:
        root = kb.create_task(
            conn,
            title="Framework-Bug fixen",
            body="Keine Legacy-Repo-Zeile.",
            repository="hermes",
            tenant="voicera",
            triage=True,
        )
        (child_id,) = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[{
                "title": "Implementierung",
                "body": "Der Redekompositionsschritt ohne Repo-Deklaration.",
                "assignee": "voicera-coder",
                "parents": [],
            }],
        )
        child = kb.get_task(conn, child_id)
        assert child.repository == "hermes"
        assert "Repository:" not in (child.body or "")
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (child_id,))

        assert kb.block_task(conn, child_id, reason="Review requested", kind="review")
        routed = kb.get_task(conn, child_id)

    assert routed.status == "review"
    assert routed.assignee == "hermes-reviewer"


def test_structured_repository_beats_conflicting_legacy_body(
    kanban_home, role_profiles_exist,
):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Framework-Fix",
            body="Repository: voicera",
            repository="hermes",
            tenant="voicera",
        )
        profile = kb.resolve_task_repository_profile(conn, task_id)

    assert profile is not None
    assert profile.name == "hermes"
    assert profile.base_ref == "fork/main"


# ---------------------------------------------------------------------------
# Condition 2: no declaration → tenant default unchanged
# ---------------------------------------------------------------------------

def test_decompose_without_declaration_keeps_tenant_default(
    kanban_home, role_profiles_exist,
):
    with kb.connect() as conn:
        root = kb.create_task(
            conn, title="Voicera-Feature bauen", body="Ganz normal, kein Repo-Feld.",
            tenant="voicera", triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn, root, root_assignee="orchestrator",
            children=[
                {"title": "code it", "assignee": "voicera-coder", "parents": []},
                {"title": "review it", "assignee": "voicera-reviewer",
                 "parents": [0]},
            ],
        )
        assert child_ids
        assert kb.get_task(conn, child_ids[0]).assignee == "voicera-coder"
        assert kb.get_task(conn, child_ids[1]).assignee == "voicera-reviewer"


# ---------------------------------------------------------------------------
# Condition 3: fbe14b734c reverse direction keeps working
# (also covered by test_kanban_decompose_db.py — duplicated here so the
# repo-routing suite is self-contained evidence)
# ---------------------------------------------------------------------------

def test_decompose_still_rewrites_wrong_tenant_profiles(
    kanban_home, role_profiles_exist,
):
    with kb.connect() as conn:
        root = kb.create_task(
            conn, title="Voicera-Arbeit", body="Kein Repo-Feld hier.",
            tenant="voicera", triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn, root, root_assignee="orchestrator",
            children=[
                {"title": "code it", "assignee": "hermes-coder", "parents": []},
                {"title": "review it", "assignee": "hermes-reviewer",
                 "parents": [0]},
            ],
        )
        assert child_ids
        assert kb.get_task(conn, child_ids[0]).assignee == "voicera-coder"
        assert kb.get_task(conn, child_ids[1]).assignee == "voicera-reviewer"


# ---------------------------------------------------------------------------
# Condition 1 (review leg): kind=review handoff honors the declaration
# ---------------------------------------------------------------------------

def test_review_block_honors_repo_declaration(kanban_home, role_profiles_exist):
    with kb.connect() as conn:
        t = kb.create_task(
            conn, title="Framework-Fix", body=HERMES_DECL_BODY,
            assignee="hermes-coder", tenant="voicera",
        )
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (t,))
        assert kb.block_task(conn, t, reason="Review requested", kind="review")
        task = kb.get_task(conn, t)
        assert task.status == "review"
        assert task.assignee == "hermes-reviewer"


def test_review_block_without_declaration_uses_tenant(kanban_home, role_profiles_exist):
    with kb.connect() as conn:
        t = kb.create_task(
            conn, title="build footer", assignee="voicera-coder",
            tenant="voicera",
        )
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (t,))
        assert kb.block_task(conn, t, reason="Review requested", kind="review")
        task = kb.get_task(conn, t)
        assert task.status == "review"
        assert task.assignee == "voicera-reviewer"


def test_review_block_inherits_declaration_from_parent(
    kanban_home, role_profiles_exist,
):
    """A decomposed child usually doesn't repeat the root's repo
    declaration — the review handoff must still honor the root's."""
    with kb.connect() as conn:
        root = kb.create_task(
            conn, title="Framework-Initiative", body=HERMES_DECL_BODY,
            tenant="voicera", triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn, root, root_assignee="orchestrator",
            children=[
                {"title": "step 1: implement",
                 "body": "Nur der Schritt, ohne Repo-Feld.",
                 "assignee": "voicera-coder", "parents": []},
            ],
        )
        assert child_ids
        child = child_ids[0]
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (child,))
        assert kb.block_task(conn, child, reason="Review requested", kind="review")
        task = kb.get_task(conn, child)
        assert task.status == "review"
        assert task.assignee == "hermes-reviewer"


# ---------------------------------------------------------------------------
# Condition 4: loop-brake diagnosis routing uses the same resolution
# ---------------------------------------------------------------------------

def _fail_run(conn, tid, error):
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None, "claim failed — task not ready?"
    kb._record_task_failure(
        conn, tid, error,
        outcome="gave_up", release_claim=True, end_run=True,
    )


def test_loop_brake_honors_repo_declaration(
    kanban_home, role_profiles_exist, monkeypatch,
):
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="Framework-Fix, hängt", body=HERMES_DECL_BODY,
            assignee="hermes-coder", tenant="voicera",
        )
        _fail_run(conn, tid, "AssertionError in tests/test_x.py:42")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        _fail_run(conn, tid, "AssertionError in tests/test_x.py:42")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        assert kb.check_respawn_guard(conn, tid) == "loop_detected"

        spawned = []
        kb.dispatch_once(
            conn, spawn_fn=lambda t, w, **k: spawned.append((t.id, t.assignee)),
        )
        # The loop brake reroutes the card and spawns the DIAGNOSIS
        # reviewer in the same tick — so the card is running@reviewer.
        task = kb.get_task(conn, tid)
        assert task.assignee == "hermes-reviewer", (
            "loop-brake diagnosis must route to the DECLARED repo's "
            "reviewer, not the tenant default"
        )
        assert all(a == "hermes-reviewer" for _, a in spawned)


def test_loop_brake_without_declaration_uses_tenant(
    kanban_home, role_profiles_exist, monkeypatch,
):
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="fix the flaky thing", assignee="alice",
            tenant="voicera",
        )
        _fail_run(conn, tid, "AssertionError in tests/test_x.py:42")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        _fail_run(conn, tid, "AssertionError in tests/test_x.py:42")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        assert kb.check_respawn_guard(conn, tid) == "loop_detected"

        spawned = []
        kb.dispatch_once(
            conn, spawn_fn=lambda t, w, **k: spawned.append((t.id, t.assignee)),
        )
        task = kb.get_task(conn, tid)
        assert task.assignee == "voicera-reviewer"
        assert all(a == "voicera-reviewer" for _, a in spawned)


# ---------------------------------------------------------------------------
# Condition 4b (F34): an INFRA-driven review loop (clean-exit protocol
# violation recorded as 'crashed', crash, or timeout) hands the work BACK to
# the coder lane for a fresh attempt instead of escalating a question-less
# human block the Beobachter rejects as rot-technische-rueckfrage. A reviewer
# that deliberately keeps failing (gave_up) may be a real wall → still
# blocked@till.
# ---------------------------------------------------------------------------


def _ended_run(conn, tid, profile, outcome, error):
    now = int(__import__("time").time())
    conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, outcome, error, "
        "started_at, ended_at) VALUES (?, ?, 'ended', ?, ?, ?, ?)",
        (tid, profile, outcome, error, now - 60, now - 30),
    )


_VIOLATION_SIG = (
    "worker exited cleanly (rc=0) without calling kanban_complete or "
    "kanban_block — protocol violation."
)


def test_review_loop_infra_routes_to_coder(kanban_home, role_profiles_exist):
    """Two identical clean-exit protocol-violation reviewer runs route the
    card back to the declared coder with a reset budget — no human block."""
    spawns = []
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="Ungenutzte lokale Variablen in drei Routern",
            assignee="voicera-reviewer", tenant="voicera",
        )
        conn.execute(
            "UPDATE tasks SET status='review', consecutive_failures=2 "
            "WHERE id=?",
            (tid,),
        )
        _ended_run(conn, tid, "voicera-reviewer", "crashed", _VIOLATION_SIG)
        _ended_run(conn, tid, "voicera-reviewer", "crashed", _VIOLATION_SIG)
        res = kb.dispatch_once(
            conn, spawn_fn=lambda t, w, **k: spawns.append((t.id, t.assignee)),
        )
        task = kb.get_task(conn, tid)
        events = [e for e in kb.list_events(conn, tid)
                  if e.kind == "review_loop_detected"]
        comments = kb.list_comments(conn, tid)
    assert spawns == [], "no third identical review run"
    assert task.status == "ready", "infra review loop stays in the factory"
    assert task.assignee == "voicera-coder", "handed back to the declared coder"
    assert task.consecutive_failures == 0, "coder gets a fresh failure budget"
    assert ("review_loop_detected" in [r[1] for r in res.respawn_guarded])
    assert events and events[-1].payload.get("disposition") == "coder_reattempt"
    assert events[-1].payload.get("routed_to") == "voicera-coder"
    # No human escalation, no faked Zielfrage.
    assert not any(c.author == "till" for c in comments)
    assert all(
        "?" not in (c.body or "")
        for c in comments if c.author == "orchestrator"
    )


def test_review_loop_noninfra_still_escalates_to_till(
    kanban_home, role_profiles_exist,
):
    """A reviewer that deliberately gave_up twice is a possible real wall —
    even with a resolvable coder it parks at the human, not an infra trip."""
    spawns = []
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="review me", assignee="voicera-reviewer",
            tenant="voicera",
        )
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        _ended_run(conn, tid, "voicera-reviewer", "gave_up", "stalled at X")
        _ended_run(conn, tid, "voicera-reviewer", "gave_up", "stalled at X")
        kb.dispatch_once(conn, spawn_fn=lambda t, w, **k: spawns.append(t.id))
        task = kb.get_task(conn, tid)
    assert spawns == []
    assert task.status == "blocked"
    assert task.assignee == "till"


def test_ready_loop_detect_scoped_to_assignee(
    kanban_home, role_profiles_exist,
):
    """A ready card is only 'looping' when its OWN assignee failed twice: a
    prior owner's crashes (e.g. after the review-loop hand-back) do not count,
    so the reassigned coder actually respawns instead of flapping (F34)."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="fix", assignee="voicera-coder", tenant="voicera",
        )
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        # Two crashes by the REVIEWER; the card is now owned by the coder.
        _ended_run(conn, tid, "voicera-reviewer", "crashed", _VIOLATION_SIG)
        _ended_run(conn, tid, "voicera-reviewer", "crashed", _VIOLATION_SIG)
        assert kb.check_respawn_guard(conn, tid) is None, (
            "the reviewer's crashes are not the coder's loop"
        )
        # The SAME two crashes by the coder itself → a genuine loop.
        conn.execute("DELETE FROM task_runs WHERE task_id=?", (tid,))
        _ended_run(conn, tid, "voicera-coder", "crashed", _VIOLATION_SIG)
        _ended_run(conn, tid, "voicera-coder", "crashed", _VIOLATION_SIG)
        assert kb.check_respawn_guard(conn, tid) == "loop_detected"


# ---------------------------------------------------------------------------
# Negative case: an explicit, unknown declaration is an integration boundary,
# not a hint. It must never fall through to the tenant default.
# ---------------------------------------------------------------------------

def test_resolver_unknown_declaration_fails_closed(
    kanban_home, role_profiles_exist,
):
    with pytest.raises(kb.RepositoryProfileError, match="unbekanntes-repo"):
        kb.resolve_repo_profiles("Repository: unbekanntes-repo", "voicera")


def test_dispatch_unknown_declaration_blocks_needs_input(
    kanban_home, role_profiles_exist,
):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="Kaputte Deklaration",
            body="Repository: unbekanntes-repo\nBitte umsetzen.",
            assignee="voicera-coder", tenant="voicera",
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
        result = kb.dispatch_once(conn, spawn_fn=lambda *_args, **_kwargs: 1)
        task = kb.get_task(conn, task_id)

    assert task is not None
    assert task.status == "blocked"
    assert task.block_kind == "needs_input"
    assert task.assignee == "voicera-coder"
    assert task_id in result.auto_blocked


def test_decompose_unknown_declaration_blocks_root_needs_input(
    kanban_home, role_profiles_exist,
):
    with kb.connect() as conn:
        root = kb.create_task(
            conn, title="Kaputte Deklaration",
            body="Repository: unbekanntes-repo\nBitte umsetzen.",
            tenant="voicera", triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn, root, root_assignee="orchestrator",
            children=[{"title": "code it", "assignee": "voicera-coder", "parents": []}],
        )
        task = kb.get_task(conn, root)

    assert child_ids is None
    assert task.status == "blocked"
    assert task.block_kind == "needs_input"


def test_review_handoff_unknown_declaration_blocks_needs_input(
    kanban_home, role_profiles_exist,
):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="Kaputte Deklaration",
            body="Repository: unbekanntes-repo", assignee="voicera-coder",
            tenant="voicera",
        )
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task_id,))
        assert kb.block_task(conn, task_id, reason="Review requested", kind="review")
        task = kb.get_task(conn, task_id)

    assert task.status == "blocked"
    assert task.block_kind == "needs_input"
    assert task.assignee == "voicera-coder"


def test_loop_brake_unknown_declaration_blocks_needs_input(
    kanban_home, role_profiles_exist, monkeypatch,
):
    """The loop brake must not let an invalid declaration abort a tick."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Kaputte Deklaration in Fehlerschleife",
            body="Repository: unbekanntes-repo\nBitte umsetzen.",
            assignee="voicera-coder",
            tenant="voicera",
        )
        _fail_run(conn, task_id, "AssertionError in tests/test_x.py:42")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
        _fail_run(conn, task_id, "AssertionError in tests/test_x.py:42")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))

        result = kb.dispatch_once(conn, spawn_fn=lambda *_args, **_kwargs: 1)
        task = kb.get_task(conn, task_id)

    assert task is not None
    assert task.status == "blocked"
    assert task.block_kind == "needs_input"
    assert task.assignee == "voicera-coder"
    assert task_id in result.auto_blocked
