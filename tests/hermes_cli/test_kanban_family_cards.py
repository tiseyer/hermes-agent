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
