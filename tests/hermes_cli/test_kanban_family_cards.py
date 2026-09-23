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
