"""Backlog cards are human-controlled parking spots, never dispatcher input."""
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_decompose
from hermes_cli.kanban_db_graph import decompose_triage_task
from plugins.kanban.dashboard import plugin_api


def test_backlog_card_is_not_dispatched_or_decomposed(tmp_path, monkeypatch, all_assignees_spawnable):
    """A parked card must stay untouched by both autonomous worker entry points."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.init_db()

    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="human parked", assignee="default", initial_status="backlog")

        result = kbd.dispatch_once(conn, dry_run=True)
        assert result.spawned == []
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "backlog"
        assert kanban_decompose.list_triage_ids() == []
        assert decompose_triage_task(
            conn, task_id, root_assignee="default", children=[{"title": "would be wrong"}],
        ) is None


def test_backlog_card_dispatches_after_human_moves_it_to_todo(tmp_path, monkeypatch, all_assignees_spawnable):
    """Moving a parked card to todo re-enters the established promotion/dispatch path."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kb.init_db()

    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="resume when released", assignee="default", initial_status="backlog")
        assert plugin_api._set_status_direct(conn, task_id, "todo") is True

        result = kbd.dispatch_once(conn, dry_run=True)

    assert [spawned_id for spawned_id, _assignee, _workspace in result.spawned] == [task_id]
