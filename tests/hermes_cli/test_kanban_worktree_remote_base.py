"""Kanban dispatcher worktree creation must branch from the current remote
tip, not a stale/dirty local ``HEAD`` (task t_c3e3ed9c).

Reproduces the exact factory blockade: a local clone whose ``HEAD`` lags
``origin/main`` and carries uncommitted files. Before the fix,
``_ensure_git_worktree`` hardcoded ``HEAD`` as the branch base for new
branches, so every new task's worktree — and thus every task's diff — was
rooted on the stale commit, dragging the local dirt's *absence* along but
missing every commit that landed on the remote after the clone went stale.

These tests exercise the REAL ``hermes_cli.kanban_db._ensure_git_worktree`` /
``_resolve_worktree_workspace`` against a real local bare "remote" + clone
(so ``git fetch`` works offline in the hermetic sandbox), proving the
dispatcher-created worktree contains commits that exist on the remote but
not on the stale local HEAD, while the root clone's dirt is untouched and
never copied into the worktree.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


def _run(args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=30)


def _commit(repo, name, msg):
    (Path(repo) / name).write_text(msg + "\n")
    _run(["git", "add", "."], repo)
    _run(["git", "commit", "-m", msg], repo)


def _head(repo):
    return _run(["git", "rev-parse", "HEAD"], repo).stdout.strip()


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def stale_dirty_clone(tmp_path):
    """A bare 'remote' + a clone that is BEHIND the remote AND has uncommitted dirt.

    Returns (clone_path, remote_head_sha, stale_local_head_sha).
    """
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    _run(["git", "init"], seed)
    _run(["git", "config", "user.email", "t@t.com"], seed)
    _run(["git", "config", "user.name", "T"], seed)
    _run(["git", "checkout", "-b", "main"], seed)
    _commit(seed, "README.md", "base commit")
    _run(["git", "init", "--bare", str(remote)], tmp_path)
    _run(["git", "remote", "add", "origin", str(remote)], seed)
    _run(["git", "push", "origin", "main"], seed)
    # Bare remote's default branch, mirroring a real GitHub remote.
    _run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], remote)

    clone = tmp_path / "clone"
    _run(["git", "clone", str(remote), str(clone)], tmp_path)
    _run(["git", "config", "user.email", "t@t.com"], clone)
    _run(["git", "config", "user.name", "T"], clone)
    stale_local_head = _head(clone)

    # Advance the REMOTE past the clone.
    _commit(seed, "feature.txt", "remote-only commit")
    _run(["git", "push", "origin", "main"], seed)
    remote_head = _head(seed)
    assert remote_head != stale_local_head

    # Make the local clone root dirty (uncommitted file) — must never leak
    # into the created worktree and must remain untouched afterwards.
    dirty_file = clone / "WIP.txt"
    dirty_file.write_text("uncommitted work in progress\n")

    return clone, remote_head, stale_local_head


class TestDispatcherWorktreeBranchesFromRemoteTip:
    def test_ensure_git_worktree_uses_remote_tip_not_stale_head(
        self, kanban_home, stale_dirty_clone, tmp_path
    ):
        clone, remote_head, stale_local_head = stale_dirty_clone
        target = clone / ".worktrees" / "t_test123"

        kb._ensure_git_worktree(clone, target, "wt/t_test123")

        wt_head = _head(target)
        assert wt_head == remote_head, (
            "dispatcher worktree must branch from the fetched remote tip, "
            "not the stale local HEAD"
        )
        assert wt_head != stale_local_head
        assert (target / "feature.txt").exists(), (
            "remote-only commit must be present in the new worktree"
        )

        # Root dirt must remain exactly where it was — untouched, and never
        # copied into the worktree.
        assert (clone / "WIP.txt").exists()
        assert not (target / "WIP.txt").exists()

    def test_full_dispatch_path_creates_worktree_on_remote_tip(
        self, kanban_home, stale_dirty_clone
    ):
        """Direct read-back through the real Kanban dispatch resolution path
        (``_resolve_worktree_workspace``) for a freshly created task."""
        clone, remote_head, stale_local_head = stale_dirty_clone
        with kb.connect() as conn:
            tid = kb.create_task(
                conn,
                title="fix the thing",
                workspace_kind="worktree",
                workspace_path=str(clone),
            )
            task = kb.get_task(conn, tid)

        workspace, branch = kb._resolve_worktree_workspace(task)
        wt_head = _head(workspace)
        assert wt_head == remote_head
        assert wt_head != stale_local_head
        assert (Path(workspace) / "feature.txt").exists()
        assert not (Path(workspace) / "WIP.txt").exists()
        assert (clone / "WIP.txt").exists(), "root clone dirt must remain unchanged"

    def test_offline_no_remote_fallback_to_head_still_works(self, kanban_home, tmp_path):
        """No remote reachable -> falls back to local HEAD, never hard-fails."""
        repo = tmp_path / "no-remote"
        repo.mkdir()
        _run(["git", "init"], repo)
        _run(["git", "config", "user.email", "t@t.com"], repo)
        _run(["git", "config", "user.name", "T"], repo)
        _commit(repo, "README.md", "only commit")
        head = _head(repo)

        target = repo / ".worktrees" / "t_offline"
        kb._ensure_git_worktree(repo, target, "wt/t_offline")

        assert _head(target) == head

    def test_existing_branch_is_attached_as_is_not_rebased(
        self, kanban_home, stale_dirty_clone
    ):
        """An existing branch must be reused verbatim — the remote-base
        resolution only applies to NEW branches, never to reattaching an
        established one (branch-attach semantics must stay stable)."""
        clone, remote_head, stale_local_head = stale_dirty_clone
        branch = "wt/existing-branch"
        first_target = clone / ".worktrees" / "first"
        kb._ensure_git_worktree(clone, first_target, branch)
        assert _head(first_target) == remote_head

        # Remove the first worktree checkout but keep the branch ref, then
        # attach a second target to the SAME (now-existing) branch.
        _run(["git", "worktree", "remove", "--force", str(first_target)], clone)
        second_target = clone / ".worktrees" / "second"
        kb._ensure_git_worktree(clone, second_target, branch)
        assert _head(second_target) == remote_head


def test_resolver_prefers_origin_develop_over_default_branch(tmp_path):
    """A repo carrying origin/develop integrates there — new task branches
    must root on origin/develop, not the remote default branch (main).
    Live-repro: wt/t_31521123 was born 20 commits behind develop."""
    from hermes_cli.worktree_base import resolve_worktree_base

    remote = tmp_path / "remote.git"
    _run(["git", "init", "--bare", str(remote)], tmp_path)
    clone = tmp_path / "clone"
    _run(["git", "clone", str(remote), str(clone)], tmp_path)
    _run(["git", "config", "user.email", "t@example.com"], clone)
    _run(["git", "config", "user.name", "t"], clone)
    _commit(clone, "a.txt", "on main")
    _run(["git", "branch", "-M", "main"], clone)
    _run(["git", "push", "-u", "origin", "main"], clone)
    _run(["git", "checkout", "-b", "develop"], clone)
    _commit(clone, "b.txt", "on develop")
    _run(["git", "push", "-u", "origin", "develop"], clone)
    develop_sha = _head(clone)
    # Detach like the live root checkout (no upstream for step 1).
    _run(["git", "checkout", "--detach", "main"], clone)

    base_ref, label = resolve_worktree_base(str(clone))
    assert base_ref == "origin/develop", (base_ref, label)


def test_resolver_env_override_wins(tmp_path, monkeypatch):
    """HERMES_WORKTREE_BASE_REF pins the base ref explicitly."""
    from hermes_cli.worktree_base import resolve_worktree_base

    remote = tmp_path / "remote.git"
    _run(["git", "init", "--bare", str(remote)], tmp_path)
    clone = tmp_path / "clone"
    _run(["git", "clone", str(remote), str(clone)], tmp_path)
    _run(["git", "config", "user.email", "t@example.com"], clone)
    _run(["git", "config", "user.name", "t"], clone)
    _commit(clone, "a.txt", "on main")
    _run(["git", "branch", "-M", "main"], clone)
    _run(["git", "push", "-u", "origin", "main"], clone)
    _run(["git", "checkout", "-b", "release"], clone)
    _commit(clone, "r.txt", "on release")
    _run(["git", "push", "-u", "origin", "release"], clone)
    _run(["git", "checkout", "--detach", "main"], clone)

    monkeypatch.setenv("HERMES_WORKTREE_BASE_REF", "origin/release")
    base_ref, label = resolve_worktree_base(str(clone))
    assert base_ref == "origin/release", (base_ref, label)
