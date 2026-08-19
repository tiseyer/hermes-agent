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


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
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


# ---------------------------------------------------------------------------
# Condition 1: decompose honors the root's repo declaration
# ---------------------------------------------------------------------------

def test_decompose_honors_repo_declaration(kanban_home, role_profiles_exist):
    with kb.connect() as conn:
        root = kb.create_task(
            conn, title="Framework-Bug fixen", body=HERMES_DECL_BODY,
            tenant="voicera", triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn, root, root_assignee="orchestrator",
            children=[
                # The decomposer LLM guessed the tenant pair — the root's
                # explicit declaration must override it.
                {"title": "code it", "assignee": "voicera-coder", "parents": []},
                {"title": "review it", "assignee": "voicera-reviewer",
                 "parents": [0]},
            ],
        )
        assert child_ids
        assert kb.get_task(conn, child_ids[0]).assignee == "hermes-coder"
        assert kb.get_task(conn, child_ids[1]).assignee == "hermes-reviewer"


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
# Negative case (Review-Nachlieferung 19.08.): UNKNOWN explicit declaration
# → tenant-default fallback + log warning, never a crash, never silent
# ---------------------------------------------------------------------------

def test_resolver_unknown_declaration_falls_back_to_tenant_with_warning(
    kanban_home, role_profiles_exist, caplog,
):
    import logging
    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
        resolved = kb.resolve_repo_profiles(
            "Titel\nRepository: unbekanntes-repo\nmehr text", "voicera",
        )
    assert resolved == ("voicera-coder", "voicera-reviewer", "tenant"), (
        "unknown explicit declaration must fall back to the tenant default"
    )
    assert any(
        "Unbekannte Repo-Deklaration" in r.message and "unbekanntes-repo" in r.message
        for r in caplog.records
    ), "the unknown declaration must be surfaced as a log warning"


def test_resolver_unknown_declaration_without_tenant_returns_none(
    kanban_home, role_profiles_exist, caplog,
):
    import logging
    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
        resolved = kb.resolve_repo_profiles(
            "Repository: unbekanntes-repo", None,
        )
    assert resolved is None, (
        "no known declaration and no tenant → None (caller legacy fallback)"
    )
    assert any(
        "Unbekannte Repo-Deklaration" in r.message for r in caplog.records
    )


def test_decompose_unknown_declaration_keeps_tenant_default(
    kanban_home, role_profiles_exist,
):
    """End-to-end pin: a root that declares an unknown repo must route its
    children exactly like an undeclared card — tenant pair, no crash."""
    with kb.connect() as conn:
        root = kb.create_task(
            conn, title="Kaputte Deklaration",
            body="Repository: unbekanntes-repo\nBitte umsetzen.",
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
