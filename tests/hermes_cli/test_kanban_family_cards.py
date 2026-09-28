"""Regression coverage for decomposed Kanban card families."""
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


def test_family_root_tracks_work_children_and_ignores_human_check(kanban_home):
    with kb.connect_closing() as conn:
        root_id = kb.create_task(conn, title="root", triage=True)
        child_ids = kb.decompose_triage_task(
            conn,
            root_id,
            root_assignee="orchestrator",
            children=[
                {"title": "first", "assignee": "worker", "parents": []},
                {"title": "second", "assignee": "worker", "parents": [0]},
            ],
        )
        assert child_ids is not None
        first, second = child_ids
        root = kb.get_task(conn, root_id)
        assert (root.family_root_id, root.family_order, root.status) == (root_id, 0, "ready")
        assert kb.claim_task(conn, root_id) is None

        assert kb.claim_task(conn, first) is not None
        assert kb.get_task(conn, root_id).status == "running"
        assert kb.complete_task(conn, first, result="done")
        assert kb.get_task(conn, root_id).status == "ready"
        assert kb.complete_task(conn, second, result="done")
        assert kb.get_task(conn, root_id).status == "done"

        human_check = kb.create_task(
            conn,
            title="acceptance",
            assignee="till",
            parents=[second],
            child_role="human_check",
        )
        check = kb.get_task(conn, human_check)
        assert (check.family_root_id, check.child_role) == (root_id, "human_check")
        assert kb.get_task(conn, root_id).status == "done"


def test_existing_cards_are_work_and_not_family_members(kanban_home):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="ordinary")
        task = kb.get_task(conn, task_id)
    assert task.child_role == "work"
    assert task.family_root_id is None


def test_linking_child_to_family_member_inherits_family_metadata(kanban_home):
    """The public link path appends an existing card to the parent's family."""
    with kb.connect_closing() as conn:
        root_id = kb.create_task(conn, title="root", triage=True)
        child_ids = kb.decompose_triage_task(
            conn,
            root_id,
            root_assignee="orchestrator",
            children=[{"title": "work", "assignee": "worker", "parents": []}],
        )
        assert child_ids is not None
        work_child = child_ids[0]
        assert kb.complete_task(conn, work_child, result="done")

        human_check = kb.create_task(
            conn,
            title="acceptance",
            assignee="till",
            child_role="human_check",
        )
        kb.link_tasks(conn, work_child, human_check)

        appended = kb.get_task(conn, human_check)
        assert (appended.family_root_id, appended.family_order, appended.child_role) == (
            root_id,
            2,
            "human_check",
        )
        assert kb.get_task(conn, root_id).status == "done"


@pytest.mark.parametrize("concurrency_limit", [
    {"max_in_progress": 2},
    {"max_spawn": 2},
])
def test_dispatch_concurrency_ignores_mirrored_family_root(
    concurrency_limit,
    kanban_home, all_assignees_spawnable,
):
    """A mirrored root is board state, not an in-flight worker slot."""
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append(task.id)

    with kb.connect_closing() as conn:
        root_id = kb.create_task(conn, title="root", triage=True)
        child_ids = kb.decompose_triage_task(
            conn,
            root_id,
            root_assignee="orchestrator",
            children=[{"title": "work", "assignee": "worker", "parents": []}],
        )
        assert child_ids is not None
        work_child = child_ids[0]

        first = kb.dispatch_once(conn, spawn_fn=fake_spawn, **concurrency_limit)
        assert [task_id for task_id, _, _ in first.spawned] == [work_child]
        root = kb.get_task(conn, root_id)
        assert root is not None
        assert root.status == "running"

        independent = kb.create_task(conn, title="independent", assignee="other")
        second = kb.dispatch_once(conn, spawn_fn=fake_spawn, **concurrency_limit)

    assert [task_id for task_id, _, _ in second.spawned] == [independent]


def test_dispatch_per_profile_limit_ignores_mirrored_family_root(
    kanban_home, all_assignees_spawnable,
):
    """A root sharing an assignee with its child does not consume a profile slot."""
    with kb.connect_closing() as conn:
        root_id = kb.create_task(conn, title="root", triage=True)
        child_ids = kb.decompose_triage_task(
            conn,
            root_id,
            root_assignee="worker",
            children=[{"title": "work", "assignee": "worker", "parents": []}],
        )
        assert child_ids is not None
        work_child = child_ids[0]

        first = kb.dispatch_once(
            conn, spawn_fn=lambda task, workspace: None,
            max_in_progress_per_profile=2,
        )
        assert [task_id for task_id, _, _ in first.spawned] == [work_child]

        independent = kb.create_task(conn, title="independent", assignee="worker")
        second = kb.dispatch_once(
            conn, spawn_fn=lambda task, workspace: None,
            max_in_progress_per_profile=2,
        )

    assert [task_id for task_id, _, _ in second.spawned] == [independent]


def _event_kinds(conn, task_id):
    return [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ?", (task_id,)
    )]


def test_dispatch_filters_family_root_before_claim(
    kanban_home, all_assignees_spawnable,
):
    """A root mirroring a ready work child is excluded from dispatch candidates.

    Regression for the family-root ghost state: the SQL trigger mirrors a ready
    child onto the root, so the root also reads ``ready``. Before the fix the
    dispatcher tried to claim the root every tick and wrote a ``spawn_rejected``
    event on it (board/telemetry noise). After the fix the root is filtered out
    of ``ready_rows`` entirely: the child spawns, the root is never claimed, and
    no ``spawn_rejected`` is written — the root simply waits on its children.
    """
    spawns = []

    def fake_spawn(task, workspace):
        spawns.append(task.id)

    with kb.connect_closing() as conn:
        root_id = kb.create_task(conn, title="root", triage=True)
        child_ids = kb.decompose_triage_task(
            conn,
            root_id,
            root_assignee="worker",
            children=[{"title": "work", "assignee": "worker", "parents": []}],
        )
        assert child_ids is not None
        work_child = child_ids[0]

        # Trigger mirrors the ready child onto the root -> root reads 'ready'
        # and would otherwise be a spawn candidate this tick.
        assert kb.get_task(conn, root_id).status == "ready"

        result = kb.dispatch_once(conn, spawn_fn=fake_spawn)

        # The work child spawns; the root never does.
        assert [task_id for task_id, _, _ in result.spawned] == [work_child]
        assert spawns == [work_child]
        # The root was filtered before claim_task: no spawn_rejected noise.
        assert "spawn_rejected" not in _event_kinds(conn, root_id)
        # Root state is a pure projection of its now-running child.
        assert kb.get_task(conn, root_id).status == "running"


def test_dispatch_still_repairs_ready_child_with_undone_parent(
    kanban_home, all_assignees_spawnable,
):
    """The family-root filter must not disturb the parents_not_done repair.

    A non-root child racily left in ``ready`` while a parent is still unfinished
    must still be demoted back to ``todo`` (and left unspawned) by the loop's
    claim path. This guards that filtering roots did not remove that repair.
    """
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = kb.create_task(
            conn, title="child", assignee="worker", parents=[parent],
        )
        # Open parent -> child is created in 'todo'.
        assert kb.get_task(conn, child).status == "todo"

        # Simulate a racy writer that left the child 'ready' too early.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready' WHERE id = ?", (child,),
            )

        result = kb.dispatch_once(conn, spawn_fn=lambda task, workspace: None)

        # Parent spawns; the child is repaired to 'todo', not spawned.
        assert [task_id for task_id, _, _ in result.spawned] == [parent]
        assert kb.get_task(conn, child).status == "todo"
        # The repair path still records its operator-visible signal.
        assert "spawn_rejected" in _event_kinds(conn, child)
