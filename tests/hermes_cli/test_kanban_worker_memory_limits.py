"""Repository-profile memory limits for real kanban worker process trees."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
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


def _profile(name: str, memory_limit_mb: int) -> kb.RepositoryProfile:
    return kb.RepositoryProfile(
        name=name,
        coder=f"{name}-coder",
        reviewer=f"{name}-reviewer",
        base_ref="origin/main",
        merge_target="main",
        push_target="origin",
        memory_limit_mb=memory_limit_mb,
    )


def test_all_repository_profiles_have_positive_memory_limits():
    assert kb._REPOSITORY_PROFILES
    for name, profile in kb._REPOSITORY_PROFILES.items():
        assert isinstance(profile.memory_limit_mb, int), name
        assert not isinstance(profile.memory_limit_mb, bool), name
        assert profile.memory_limit_mb > 0, name
        assert kb._validated_repository_profile(profile) is profile


@pytest.mark.parametrize("invalid", [None, True, 0, -1, 1.5, "1024"])
def test_repository_profile_rejects_invalid_memory_limit(invalid):
    with pytest.raises(kb.RepositoryProfileError, match="memory_limit_mb"):
        kb._validated_repository_profile(_profile("invalid", invalid))


def test_memory_limited_spawn_fails_closed_without_user_systemd(
    kanban_home, monkeypatch, tmp_path,
):
    monkeypatch.setattr(kb, "_systemd_user_manager", lambda _env: None)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="limited", assignee="worker")
        task = kb.get_task(conn, task_id)

    with pytest.raises(RuntimeError, match="systemd user manager"):
        kb._default_spawn(
            task,
            str(tmp_path),
            repository_profile=_profile("limited", 128),
        )


def test_dispatch_passes_resolved_profile_to_ready_and_review_spawns(
    kanban_home, monkeypatch, tmp_path,
):
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    captured = []

    def capture_spawn(task, workspace, repository_profile=None):
        captured.append((task.id, repository_profile))

    with kb.connect() as conn:
        ready_id = kb.create_task(
            conn,
            title="ready",
            body="Repository: hermes-agent",
            assignee="hermes-coder",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        review_id = kb.create_task(
            conn,
            title="review",
            body="Repository: hermes-agent",
            assignee="hermes-reviewer",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        conn.execute(
            "UPDATE tasks SET status = 'review' WHERE id = ?", (review_id,)
        )

        result = kb.dispatch_once(conn, spawn_fn=capture_spawn)

    assert {task_id for task_id, _profile_value in captured} == {
        ready_id,
        review_id,
    }
    assert all(profile is kb._REPOSITORY_PROFILES["hermes"] for _, profile in captured)
    assert {task_id for task_id, _assignee, _workspace in result.spawned} == {
        ready_id,
        review_id,
    }


def test_default_spawn_enforces_profile_memory_across_worker_tree(
    kanban_home, monkeypatch, tmp_path,
):
    """The same real worker tree fails below its aggregate use and passes above it."""
    manager = kb._systemd_user_manager(dict(os.environ))
    if manager is None:
        pytest.skip("Linux cgroup-v2 systemd user manager is unavailable")

    worker = tmp_path / "memory_worker.py"
    worker.write_text(
        textwrap.dedent(
            """
            import os
            import subprocess
            import sys
            import time
            from pathlib import Path

            allocation = 50 * 1024 * 1024
            if "--memory-test-child" in sys.argv:
                held = bytearray(allocation)
                time.sleep(2)
                raise SystemExit(0)

            held = bytearray(allocation)
            marker = Path(os.environ["MEMORY_TEST_DIR"]) / (
                os.environ["HERMES_KANBAN_TASK"] + ".cgroup"
            )
            marker.write_text(Path("/proc/self/cgroup").read_text(), encoding="utf-8")
            child = subprocess.Popen([sys.executable, __file__, "--memory-test-child"])
            raise SystemExit(child.wait())
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MEMORY_TEST_DIR", str(tmp_path))
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: [sys.executable, str(worker)])
    monkeypatch.setattr(kb, "_resolve_worker_cli_toolsets", lambda _home: None)

    with kb.connect() as conn:
        low_id = kb.create_task(conn, title="low limit", assignee="worker-low")
        high_id = kb.create_task(conn, title="high limit", assignee="worker-high")
        low_task = kb.get_task(conn, low_id)
        high_task = kb.get_task(conn, high_id)

    low_pid = kb._default_spawn(
        low_task,
        str(tmp_path),
        repository_profile=_profile("low", 96),
    )
    _, low_status = os.waitpid(low_pid, 0)
    low_exit = os.waitstatus_to_exitcode(low_status)

    high_pid = kb._default_spawn(
        high_task,
        str(tmp_path),
        repository_profile=_profile("high", 256),
    )
    _, high_status = os.waitpid(high_pid, 0)
    high_exit = os.waitstatus_to_exitcode(high_status)

    assert low_exit != 0
    assert high_exit == 0
    assert ".scope" in (tmp_path / f"{low_id}.cgroup").read_text(encoding="utf-8")
    assert ".scope" in (tmp_path / f"{high_id}.cgroup").read_text(encoding="utf-8")
