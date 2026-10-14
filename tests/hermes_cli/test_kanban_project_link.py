"""Kanban <-> Projects integration: project-linked tasks get a deterministic
worktree path and a persisted D/F-safe ``wt/<task-id>`` branch."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as pdb


@pytest.fixture
def kanban_conn(tmp_path):
    c = kb.connect(db_path=tmp_path / "kanban.db")
    try:
        yield c
    finally:
        c.close()


def _make_project(name="Web App", repo="/tmp/webapp"):
    with pdb.connect_closing() as pc:
        pid = pdb.create_project(pc, name=name, folders=[repo])
        return pdb.get_project(pc, pid)


def test_project_linked_task_gets_deterministic_worktree_and_branch(kanban_conn):
    proj = _make_project()
    tid = kb.create_task(kanban_conn, title="Add login", project_id=proj.slug)
    task = kb.get_task(kanban_conn, tid)

    assert task is not None
    assert task.project_id == proj.id
    assert task.workspace_kind == "worktree"
    # Worktree dir anchored under the project's primary repo, keyed on task id.
    assert task.workspace_path == os.path.join(proj.primary_path, ".worktrees", tid)
    # The fixed namespace cannot collide with a top-level project-slug branch.
    assert task.branch_name == f"wt/{tid}-add-login"


def test_project_linked_dispatch_avoids_project_slug_ref_collision(
    kanban_conn, tmp_path, all_assignees_spawnable,
):
    repo = tmp_path / "voicera-os"
    repo.mkdir()
    subprocess.run(
        ["git", "-C", str(repo), "init", "-b", "main"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True,
    )
    (repo / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )

    proj = _make_project(name="voicera-os", repo=str(repo))
    assert proj is not None
    subprocess.run(
        ["git", "-C", str(repo), "branch", proj.slug],
        check=True,
        capture_output=True,
    )
    spawned = []

    def fake_spawn(task, workspace, board=None):
        spawned.append((task.id, workspace))

    tid = kb.create_task(
        kanban_conn,
        title="Collision probe",
        assignee="hermes-coder",
        project_id=proj.id,
        max_retries=1,
    )
    result = kb.dispatch_once(kanban_conn, spawn_fn=fake_spawn)
    task = kb.get_task(kanban_conn, tid)
    events = kb.list_events(kanban_conn, tid)
    runs = kb.list_runs(kanban_conn, tid)

    expected_workspace = repo / ".worktrees" / tid
    expected_branch = f"wt/{tid}-collision-probe"
    checked_out_branch = subprocess.run(
        ["git", "-C", str(expected_workspace), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert task is not None
    assert task.status == "running"
    assert task.branch_name == expected_branch
    assert expected_workspace.is_dir()
    assert checked_out_branch == expected_branch
    assert result.spawned == [(tid, "hermes-coder", str(expected_workspace))]
    assert spawned == [(tid, str(expected_workspace))]
    assert not ({"spawn_failed", "gave_up"} & {event.kind for event in events})
    assert not ({"spawn_failed", "gave_up"} & {run.outcome for run in runs})


def test_explicit_branch_overrides_project_default(kanban_conn):
    proj = _make_project()
    tid = kb.create_task(
        kanban_conn,
        title="x",
        project_id=proj.slug,
        workspace_kind="worktree",
        branch_name="feature/custom",
    )
    task = kb.get_task(kanban_conn, tid)
    assert task.branch_name == "feature/custom"


def test_unlinked_task_unchanged(kanban_conn):
    tid = kb.create_task(kanban_conn, title="plain")
    task = kb.get_task(kanban_conn, tid)

    assert task.project_id is None
    assert task.workspace_kind == "scratch"
    # No branch is persisted — the worker still owns the wt/<id> fallback for
    # genuinely ad-hoc worktree tasks, but unlinked scratch tasks have none.
    assert task.branch_name is None


def test_unknown_project_id_falls_back_gracefully(kanban_conn):
    # A project id that doesn't resolve must not crash task creation; the task
    # is created as-is (scratch) and project_id stays unset.
    tid = kb.create_task(kanban_conn, title="x", project_id="does-not-exist")
    task = kb.get_task(kanban_conn, tid)
    assert task.workspace_kind == "scratch"
    assert task.project_id is None


def test_reviewer_uses_project_primary_path_when_coder_workspace_is_unavailable(
    kanban_conn, tmp_path,
):
    """A project link remains a repo anchor for review on boards without defaults.

    The reviewer process may not be able to inspect a coder worktree path from
    another worker host.  It must still resolve the repository via the task's
    project instead of requiring ``board.default_workdir``.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True)

    proj = _make_project(repo=str(repo))
    assert proj is not None
    tid = kb.create_task(kanban_conn, title="Review me", project_id=proj.id)
    task = kb.get_task(kanban_conn, tid)
    assert task is not None
    assert task.branch_name is not None
    subprocess.run(
        ["git", "-C", str(repo), "branch", task.branch_name],
        check=True,
        capture_output=True,
    )
    # The board has no default_workdir. ``--project`` persisted this future
    # worktree target, but the coder checkout is unavailable to the reviewer.
    assert task.workspace_path is not None
    assert not Path(task.workspace_path).exists()

    review_ws, branch = kb._resolve_review_worktree_workspace(task)

    assert review_ws == Path(repo) / ".worktrees" / f"{tid}-review"
    assert review_ws.is_dir()
    assert branch == task.branch_name
