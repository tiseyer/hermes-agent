"""Orchestrator-autonomy mechanics (goal orchestrator-autonomy.md).

Covers the five repairs as deterministic unit/integration tests:

1. ``recompute_ready`` no longer promotes parent-less ``todo`` backlog
   cards board-wide; decompose/specify/delete promote their own scope.
2. Done-Verifikation: a review-gated worktree completion is rejected
   (task back to ``ready`` + findings comment) when the branch was never
   pushed / no test evidence is documented, and passes when reality
   agrees.
3. Loop detection: two consecutive failures with the same signature
   route the card to the reviewer as a diagnosis check instead of a
   third identical coder attempt.
4. GO-gate: smoke/deploy cards with no assignee (or ``default``) are
   parked on ``till`` instead of auto-spawning.
5. Reviewer worktree isolation: review dispatch materializes a separate
   ``<repo>/.worktrees/<id>-review`` checkout instead of reusing the
   coder's worktree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


def _run(args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=30)


def _commit(repo, name, msg):
    (Path(repo) / name).write_text(msg + "\n")
    _run(["git", "add", "."], repo)
    _run(["git", "commit", "-m", msg], repo)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def repo_with_remote(tmp_path):
    """A local clone with a bare 'remote' (fetch/push work offline)."""
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    _run(["git", "init"], seed)
    _run(["git", "config", "user.email", "t@t.com"], seed)
    _run(["git", "config", "user.name", "T"], seed)
    _run(["git", "checkout", "-b", "main"], seed)
    _commit(seed, "README.md", "base commit")
    _run(["git", "init", "--bare", str(remote)], tmp_path)
    # Point the bare remote's HEAD at main so clones get a checkout
    # (git's default bare HEAD is master).
    _run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], remote)
    _run(["git", "remote", "add", "origin", str(remote)], seed)
    _run(["git", "push", "-u", "origin", "main"], seed)
    clone = tmp_path / "clone"
    _run(["git", "clone", str(remote), str(clone)], tmp_path)
    _run(["git", "config", "user.email", "t@t.com"], clone)
    _run(["git", "config", "user.name", "T"], clone)
    return clone


# ---------------------------------------------------------------------------
# 1. Parent-less todo cards stay put
# ---------------------------------------------------------------------------

def test_recompute_ready_leaves_parentless_todo_alone(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="backlog card")
        # Operator parks the card in the todo backlog.
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (tid,))
        promoted = kb.recompute_ready(conn)
        task = kb.get_task(conn, tid)
        assert task.status == "todo"
        assert promoted == 0


def test_recompute_ready_still_promotes_dependency_gated(kanban_home):
    with kb.connect() as conn:
        p = kb.create_task(conn, title="parent")
        c = kb.create_task(conn, title="child", parents=[p])
        # Parent not done yet -> child stays todo
        kb.recompute_ready(conn)
        assert kb.get_task(conn, c).status == "todo"
        # Complete the parent (promote it first so complete_task accepts)
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (p,))
        assert kb.complete_task(conn, p, result="ok")
        assert kb.get_task(conn, c).status == "ready"


def test_decompose_promotes_only_its_own_children(kanban_home):
    with kb.connect() as conn:
        bystander = kb.create_task(conn, title="unrelated backlog")
        conn.execute(
            "UPDATE tasks SET status = 'todo' WHERE id = ?", (bystander,)
        )
        root = kb.create_task(conn, title="root", triage=True)
        child_ids = kb.decompose_triage_task(
            conn, root,
            root_assignee="orchestrator",
            children=[
                {"title": "first", "parents": []},
                {"title": "second", "parents": [0]},
            ],
        )
        assert child_ids and len(child_ids) == 2
        assert kb.get_task(conn, child_ids[0]).status == "ready"
        assert kb.get_task(conn, child_ids[1]).status == "todo"
        assert kb.get_task(conn, bystander).status == "todo"


# ---------------------------------------------------------------------------
# 2. Done-Verifikation
# ---------------------------------------------------------------------------

def _make_review_claimed_worktree_task(conn, repo, branch="wt/test-branch"):
    wt = repo.parent / "wt-checkout"
    kb._ensure_git_worktree(repo, wt, branch)
    tid = kb.create_task(
        conn, title="worktree task", assignee="alice",
        workspace_kind="worktree", workspace_path=str(wt),
        branch_name=branch, tenant="voicera",
    )
    conn.execute(
        "UPDATE tasks SET status = 'running', block_kind = NULL WHERE id = ?",
        (tid,),
    )
    assert kb.block_task(conn, tid, reason="please review", kind="review")
    claimed = kb.claim_review_task(conn, tid)
    assert claimed is not None
    return tid, wt


def test_done_verification_rejects_unpushed_branch(kanban_home, repo_with_remote):
    with kb.connect() as conn:
        tid, wt = _make_review_claimed_worktree_task(conn, repo_with_remote)
        # Branch exists locally but was never pushed; no test evidence.
        with pytest.raises(kb.DoneVerificationError) as exc:
            kb.complete_task(conn, tid, result="looks good", summary="approved")
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert any("remote" in f or "push" in f for f in exc.value.findings)
        events = [e for e in kb.list_events(conn, tid)
                  if e.kind == "done_verification_failed"]
        assert events


def test_done_verification_accepts_pushed_branch_with_evidence(
    kanban_home, repo_with_remote,
):
    with kb.connect() as conn:
        tid, wt = _make_review_claimed_worktree_task(conn, repo_with_remote)
        _commit(wt, "work.txt", "the work")
        _run(["git", "push", "-u", "origin", "wt/test-branch"], wt)
        ok = kb.complete_task(
            conn, tid, result="approved",
            summary="Review grün: pytest 12 passed, Branch gepusht.",
        )
        assert ok
        assert kb.get_task(conn, tid).status == "done"


def test_done_verification_env_off_switch(kanban_home, repo_with_remote, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_VERIFY_DONE", "0")
    with kb.connect() as conn:
        tid, _ = _make_review_claimed_worktree_task(conn, repo_with_remote)
        assert kb.complete_task(conn, tid, result="unverified ok")
        assert kb.get_task(conn, tid).status == "done"


# ---------------------------------------------------------------------------
# 3. Loop detection
# ---------------------------------------------------------------------------

def _fail_run(conn, tid, error):
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None, "claim failed — task not ready?"
    kb._record_task_failure(
        conn, tid, error,
        outcome="gave_up", release_claim=True, end_run=True,
    )


def test_loop_detected_after_two_identical_failures(kanban_home, all_assignees_spawnable):
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
        res = kb.dispatch_once(
            conn,
            spawn_fn=lambda t, w, **k: spawned.append((t.id, t.assignee)),
        )
        # No third CODER attempt — the only spawn this tick (if any) is
        # the diagnosis reviewer the loop routing handed the card to.
        assert all(a == "voicera-reviewer" for _, a in spawned)
        assert (tid, "loop_detected") in res.respawn_guarded
        task = kb.get_task(conn, tid)
        assert task.assignee == "voicera-reviewer"
        events = [e for e in kb.list_events(conn, tid) if e.kind == "loop_detected"]
        assert events
        comments = kb.list_comments(conn, tid)
        assert any("diagnose" in (c.body or "").lower() for c in comments)


def test_no_loop_on_different_failures(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="task", assignee="alice",
        )
        _fail_run(conn, tid, "AssertionError in tests/test_x.py:42")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        _fail_run(conn, tid, "TimeoutError: provider unreachable")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        assert kb.check_failure_loop(conn, tid) is None


# ---------------------------------------------------------------------------
# 4. GO-gate for smoke/deploy cards
# ---------------------------------------------------------------------------

def test_go_gate_holds_unassigned_smoke_card(kanban_home, all_assignees_spawnable):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="Smoke: Preview-Proxy prüfen und deployen",
            )
        spawned = []
        kb.dispatch_once(
            conn, spawn_fn=lambda t, w, **k: spawned.append(t),
            default_assignee="default",
        )
        assert spawned == []
        task = kb.get_task(conn, tid)
        # GO-gated cards park as blocked@till (board invariant: waiting
        # on a human == blocked), sticky against recompute_ready.
        assert task.status == "blocked"
        assert task.assignee == "till"
        assert task.block_kind == "needs_input"
        events = [e for e in kb.list_events(conn, tid) if e.kind == "go_gate_held"]
        assert events
        blocked_events = [
            e for e in kb.list_events(conn, tid) if e.kind == "blocked"
        ]
        assert blocked_events, "go gate must write a sticky blocked event"
        # Sticky: a recompute pass must not promote it back to ready.
        kb.recompute_ready(conn)
        assert kb.get_task(conn, tid).status == "blocked"


def test_go_gate_lets_explicit_profile_assignment_through(
    kanban_home, all_assignees_spawnable,
):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="Smoke-Vorbereitung dokumentieren",
            assignee="alice",
        )
        spawned = []
        kb.dispatch_once(conn, spawn_fn=lambda t, w, **k: spawned.append(t.id))
        assert spawned == [tid]


def test_go_gate_ignores_normal_cards(kanban_home, all_assignees_spawnable):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="README-Tippfehler korrigieren",
        )
        spawned = []
        kb.dispatch_once(
            conn, spawn_fn=lambda t, w, **k: spawned.append(t.id),
            default_assignee="default",
        )
        assert spawned == [tid]
        assert kb.get_task(conn, tid).assignee == "default"


# ---------------------------------------------------------------------------
# 5. Reviewer worktree isolation
# ---------------------------------------------------------------------------

def test_review_dispatch_uses_separate_reviewer_worktree(
    kanban_home, repo_with_remote, all_assignees_spawnable,
):
    with kb.connect() as conn:
        branch = "wt/iso-branch"
        coder_wt = repo_with_remote.parent / "coder-wt"
        kb._ensure_git_worktree(repo_with_remote, coder_wt, branch)
        _commit(coder_wt, "feature.txt", "pushed work")
        _run(["git", "push", "-u", "origin", branch], coder_wt)
        # Un-pushed local dirt in the coder worktree must NOT be visible
        # to the reviewer.
        (coder_wt / "dirt.txt").write_text("uncommitted coder state\n")

        tid = kb.create_task(
            conn, title="feature task", assignee="alice",
            workspace_kind="worktree", workspace_path=str(coder_wt),
            branch_name=branch, tenant="voicera",
        )
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (tid,))
        assert kb.block_task(conn, tid, reason="review it", kind="review")

        spawned = {}
        def capture(task, workspace, **kw):
            spawned[task.id] = workspace
        kb.dispatch_once(conn, spawn_fn=capture)

        assert tid in spawned
        review_ws = Path(spawned[tid])
        assert review_ws != coder_wt
        assert review_ws.name == f"{tid}-review"
        assert (review_ws / "feature.txt").exists()
        assert not (review_ws / "dirt.txt").exists()
        # Coder's workspace_path on the task row is untouched.
        assert kb.get_task(conn, tid).workspace_path == str(coder_wt)
