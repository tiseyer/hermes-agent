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
# 1. Dedicated backlog cards stay put; todo activates work
# ---------------------------------------------------------------------------

def test_recompute_ready_promotes_parentless_todo_after_manual_activation(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="backlog card")
        # Backlog is inert; explicitly moving it to todo activates the
        # normal dependency-free promotion path.
        conn.execute("UPDATE tasks SET status = 'backlog' WHERE id = ?", (tid,))
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (tid,))
        promoted = kb.recompute_ready(conn)
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert promoted == 1


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
            "UPDATE tasks SET status = 'backlog' WHERE id = ?", (bystander,)
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
        assert kb.get_task(conn, bystander).status == "backlog"


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


def test_done_verification_accepts_evidence_in_reviewer_comment(
    kanban_home, repo_with_remote,
):
    """A review-green whose reproduced checks live in the reviewer comment
    (not the terse completion summary) is NOT bounced for 'no test evidence'.
    Otherwise the reviewer escalates a mechanical check to a human (blocked)."""
    with kb.connect() as conn:
        tid, wt = _make_review_claimed_worktree_task(conn, repo_with_remote)
        _commit(wt, "work.txt", "the work")
        _run(["git", "push", "-u", "origin", "wt/test-branch"], wt)
        kb.add_comment(
            conn, tid, "voicera-reviewer",
            "REVIEW GRÜN. ruff check --select E712 → 0 Treffer; "
            "py_compile erfolgreich. Abnahmekriterium erfüllt.",
        )
        # Completion summary itself carries NO test keyword.
        ok = kb.complete_task(
            conn, tid, result="approved", summary="Review grün, Branch gepusht.",
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
        rejected = [e for e in kb.list_events(conn, tid) if e.kind == "spawn_rejected"]
        assert rejected[-1].payload == {"reason": "loop_detected"}
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


def test_go_gate_vocabulary_covers_merge_and_release(kanban_home):
    # B3a: the gate regex must catch terminal MERGE/PR/release vocabulary,
    # not just deploy/smoke/live (the pre-B3a blind spot), and must still
    # ignore ordinary prose.
    for terminal in (
        "Merge PR #123 nach main",
        "W3-05 mergen",
        "Pull Request reviewen und mergen",
        "Release 1.2 schneiden",
        "Feature veröffentlichen",
        "Deploy to prod",  # regression: existing vocab still matches
    ):
        assert kb._GO_GATE_RE.search(terminal), terminal
    for ordinary in (
        "README-Tippfehler korrigieren",
        "Doku für das Login-Formular schreiben",
        "has-pr",          # lowercase 'pr' glued into a word must NOT match
        "prüfen",          # ditto — 'PR' is uppercase-acronym-only
        "pull latest changes",  # 'pull' without 'request' is not terminal
    ):
        assert not kb._GO_GATE_RE.search(ordinary), ordinary


def test_go_gate_holds_preassigned_terminal_card(
    kanban_home, all_assignees_spawnable,
):
    # B3b-ii: the pre-assignee bypass is closed. A terminal card handed to
    # a real profile up front (a smoke card on "alice") is now parked for
    # Till's GO — this deliberately reverses the old "explicit assignment
    # passes the gate" contract.
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="Smoke-Tests gegen prod fahren",
            assignee="alice",
        )
        spawned = []
        kb.dispatch_once(conn, spawn_fn=lambda t, w, **k: spawned.append(t.id))
        assert spawned == []
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.assignee == "till"  # parking takes the card from alice
        assert task.block_kind == "needs_input"
        assert [e for e in kb.list_events(conn, tid) if e.kind == "go_gate_held"]


def test_go_gate_reparks_leaked_go_noetig_card_without_comment_spam(
    kanban_home, all_assignees_spawnable,
):
    # F40: a card already GO-gate-parked carries category 'go_noetig' for
    # life, but parking reset its assignee to 'till' and (for a card whose
    # title/body never named a terminal step) left no vocabulary signal. If
    # the Beobachter then answers a stale Zielfrage on it (blocked -> todo)
    # and recompute_ready promotes todo -> ready@till, the title/body/merger
    # checks no longer fire, so the pre-F40 gate ignored it and the card
    # stranded in ready@till (non-spawnable human assignee) until a board
    # scan flagged it rot-stuck. The persisted 'go_noetig' category must
    # re-assert the hold: re-park to blocked@till, and post the one-shot
    # explainer comment only once across repeated leak/re-park cycles.
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="Tier-System: Ballast aufloesen (VEREINFACHUNGS-FRAGE)",
            assignee="till",
        )
        # Simulate the leaked post-park state the Beobachter produces.
        conn.execute(
            "UPDATE tasks SET status = 'ready', assignee = 'till', "
            "category = 'go_noetig', block_kind = 'needs_input' WHERE id = ?",
            (tid,),
        )

        def _comment_count() -> int:
            return conn.execute(
                "SELECT COUNT(*) FROM task_comments WHERE task_id = ? "
                "AND body LIKE 'GO-Gate:%'",
                (tid,),
            ).fetchone()[0]

        # First leak: re-park + one explainer comment.
        spawned = []
        kb.dispatch_once(conn, spawn_fn=lambda t, w, **k: spawned.append(t.id))
        assert spawned == []
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.assignee == "till"
        assert task.block_kind == "needs_input"
        assert [e for e in kb.list_events(conn, tid) if e.kind == "go_gate_held"]
        assert _comment_count() == 1

        # Beobachter answers the stale Zielfrage again: blocked -> ready leak.
        conn.execute(
            "UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,),
        )
        kb.dispatch_once(conn, spawn_fn=lambda t, w, **k: spawned.append(t.id))
        assert spawned == []
        assert kb.get_task(conn, tid).status == "blocked"
        # Idempotent: the explainer comment is NOT duplicated per cycle.
        assert _comment_count() == 1


def test_go_gate_reparks_leaked_entscheidung_card_preserving_category(
    kanban_home, all_assignees_spawnable,
):
    # F51: F40's synchronous re-park only fired for 'go_noetig'. A leaked
    # owner-Zielentscheidung (category 'entscheidung', assignee 'till') — e.g.
    # a merge-gated decision whose category got flipped go_noetig->entscheidung
    # by a manager move_to_human — slipped past the gate, lingered in
    # ready@till, and a slow Manager agent re-blocked it minutes later, a
    # window the board scan catches as rot-stuck (live Karte 47 t_0c438764).
    # Any human-decision category on the human sink must re-assert the hold in
    # the SAME tick, and the category (the inbox bucket) must survive the cycle.
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="Strategie fuer Environment Variables sicher speichern",
            assignee="till",
        )
        conn.execute(
            "UPDATE tasks SET status = 'ready', assignee = 'till', "
            "category = 'entscheidung', block_kind = 'needs_input' WHERE id = ?",
            (tid,),
        )
        spawned = []
        kb.dispatch_once(conn, spawn_fn=lambda t, w, **k: spawned.append(t.id))
        assert spawned == []
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.assignee == "till"
        assert task.block_kind == "needs_input"
        # Inbox bucket preserved — NOT overwritten with go_noetig.
        assert task.category == "entscheidung"
        assert [e for e in kb.list_events(conn, tid) if e.kind == "go_gate_held"]

        # Leak again (Beobachter ANTWORT_TODO -> recompute_ready -> ready@till):
        # the re-park stays synchronous, so the card is never stuck in ready.
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        kb.dispatch_once(conn, spawn_fn=lambda t, w, **k: spawned.append(t.id))
        assert spawned == []
        assert kb.get_task(conn, tid).status == "blocked"
        assert kb.get_task(conn, tid).category == "entscheidung"


def test_go_gate_reparks_leaked_blank_category_owner_decision(
    kanban_home, all_assignees_spawnable,
):
    # F54: the F50 owner-Zielentscheidung escalation parks the card at
    # blocked@till but historically left `category` BLANK. When the Beobachter
    # posts antwort_till and flips the card blocked->todo (a 'status' event, not
    # an 'unblocked'), recompute_ready promotes it todo->ready@till where the
    # F40/F51 category/vocabulary checks no longer recognise it — it stranded in
    # ready@till until the board scan read it as rot-stuck (live Karte 47
    # t_50c33925). The sticky owner-decision 'blocked' event survives the flip,
    # so the dispatcher keys off it even with a blank category and re-asserts the
    # hold in the SAME tick, stamping the correct 'entscheidung' inbox bucket.
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="Strategie fuer Environment Variables sicher speichern",
            assignee="till",
        )
        # Leaked state: ready@till, BLANK category, with the sticky F50 escalation
        # 'blocked' event as the most-recent blocked/unblocked record.
        conn.execute(
            "UPDATE tasks SET status = 'ready', assignee = 'till', "
            "category = '', block_kind = 'needs_input' WHERE id = ?",
            (tid,),
        )
        kb._append_event(
            conn, tid, "blocked",
            {"reason": "owner decision", "kind": "needs_input",
             "recurrences": 2, "escalation": "owner_decision_loop"},
        )
        spawned = []
        kb.dispatch_once(conn, spawn_fn=lambda t, w, **k: spawned.append(t.id))
        assert spawned == []
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.assignee == "till"
        # Blank category is upgraded to the owner-decision inbox bucket, so the
        # NEXT leak is caught by the ordinary F51 category path (self-sustaining).
        assert task.category == "entscheidung"
        assert [e for e in kb.list_events(conn, tid) if e.kind == "go_gate_held"]


def test_go_gate_ignores_blank_category_needs_input_without_escalation(
    kanban_home, all_assignees_spawnable,
):
    # F54 guard against over-gating: a plain needs_input card at ready@till with
    # a blank category and NO owner-decision escalation marker (e.g. a transient
    # infra re-queue) must NOT be treated as a held owner decision — the backstop
    # keys strictly on the sticky 'owner_decision_loop' blocked event. Without an
    # assignee re-route it would just skip as non-spawnable, never re-parked.
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="Irgendeine Karte", assignee="till",
        )
        conn.execute(
            "UPDATE tasks SET status = 'ready', assignee = 'till', "
            "category = '', block_kind = 'needs_input' WHERE id = ?",
            (tid,),
        )
        kb._append_event(conn, tid, "blocked", {"kind": "needs_input"})
        kb.dispatch_once(conn, spawn_fn=lambda t, w, **k: None)
        # The backstop must NOT fire: no re-park to blocked, no go_gate_held
        # marker, category left blank. (In prod 'till' is non-spawnable so the
        # card merely skips; here the test fixture makes every assignee
        # spawnable, so what matters is strictly the absence of the hold.)
        assert kb.get_task(conn, tid).status != "blocked"
        assert kb.get_task(conn, tid).category in ("", None)
        assert not [e for e in kb.list_events(conn, tid) if e.kind == "go_gate_held"]


def test_go_gate_ignores_decision_category_card_owned_by_worker(
    kanban_home, all_assignees_spawnable,
):
    # F51 guard against over-gating: a decision-category card is only a
    # waiting-on-Till card when it sits on the human sink (assignee='till').
    # A card carrying category 'entscheidung' but still owned by a real worker
    # profile is active work (a worker about to escalate) and must still spawn
    # — the gate keys on assignee='till', not on the category alone.
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="Ganz normale Arbeitskarte", assignee="alice",
        )
        conn.execute(
            "UPDATE tasks SET category = 'entscheidung' WHERE id = ?", (tid,),
        )
        spawned = []
        kb.dispatch_once(conn, spawn_fn=lambda t, w, **k: spawned.append(t.id))
        assert spawned == [tid]


def test_go_gate_holds_merger_card_despite_assignee(
    kanban_home, all_assignees_spawnable,
):
    # A merger card performs the terminal branch merge; it is GO-gated by
    # its assignee alone, even with an ordinary (non-terminal) title.
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="Feature-Branches integrieren",
            assignee="merger",
        )
        spawned = []
        kb.dispatch_once(conn, spawn_fn=lambda t, w, **k: spawned.append(t.id))
        assert spawned == []
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.assignee == "till"


def test_go_gate_rests_technik_merger_card_at_ready_not_blocked(
    kanban_home, all_assignees_spawnable,
):
    # F46 (Karte-19 recurrence): a TECHNIK merger card may NOT be parked to
    # blocked@till — the observer marks ANY blocked technik card ROT
    # (rot-technik-rueckfrage), while `ready @ merger` is greened as Regel c and
    # is the ONLY green terminal a technik card can reach. The GO gate keeps it
    # resting in ready@merger (non-spawned, awaiting human GO) instead of
    # flipping it to blocked, records a one-shot go_gate_held marker + explainer,
    # and has_spawnable_ready does not count it so no "stuck" warning fires.
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="S3-Backup: Streu-Tag </content> entfernen",
            assignee="merger",
            body=(
                "Generierungs-Artefakt.\n\nTechnik-Entscheidung (selbst treffen, "
                "im Kommentar begründen): Fabrik entscheidet selbst."
            ),
        )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))

        def _comment_count() -> int:
            return conn.execute(
                "SELECT COUNT(*) FROM task_comments WHERE task_id = ? "
                "AND body LIKE 'GO-Gate:%'",
                (tid,),
            ).fetchone()[0]

        spawned = []
        res = kb.dispatch_once(conn, spawn_fn=lambda t, w, **k: spawned.append(t.id))
        assert spawned == []
        assert tid in res.skipped_nonspawnable
        task = kb.get_task(conn, tid)
        # Rests at ready @ merger — NOT parked to blocked.
        assert task.status == "ready"
        assert task.assignee == "merger"
        assert task.category == "go_noetig"
        assert [e for e in kb.list_events(conn, tid) if e.kind == "go_gate_held"]
        assert _comment_count() == 1
        # The resting card is not counted as spawnable work (no stuck-warning).
        assert kb.has_spawnable_ready(conn) is False

        # Re-encountered every tick: idempotent, no event/comment spam.
        kb.dispatch_once(conn, spawn_fn=lambda t, w, **k: spawned.append(t.id))
        assert spawned == []
        assert kb.get_task(conn, tid).status == "ready"
        assert _comment_count() == 1


def test_go_gate_holds_card_with_merge_group(
    kanban_home, all_assignees_spawnable,
):
    # An explicit merge_group tag marks a card as part of a terminal merge
    # unit — gated regardless of assignee or vocabulary.
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="Sammelkarte Integration",
            assignee="alice", merge_group="release-42",
        )
        spawned = []
        kb.dispatch_once(conn, spawn_fn=lambda t, w, **k: spawned.append(t.id))
        assert spawned == []
        assert kb.get_task(conn, tid).assignee == "till"


def test_go_gate_holds_deploy_card_on_coder_profile(
    kanban_home, all_assignees_spawnable,
):
    # The exact bypass B3b-ii closes: a deploy card pre-routed to a coder
    # profile. Vocabulary makes it terminal; the pre-assignee no longer
    # lets it through.
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="Deploy nach prod ausrollen",
            assignee="hermes-coder",
        )
        spawned = []
        kb.dispatch_once(conn, spawn_fn=lambda t, w, **k: spawned.append(t.id))
        assert spawned == []
        assert kb.get_task(conn, tid).assignee == "till"


def test_go_gate_ignores_normal_assigned_card(
    kanban_home, all_assignees_spawnable,
):
    # Guard against over-gating: a non-terminal card with a real assignee
    # must still spawn. Only terminal cards are gated.
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="README-Tippfehler korrigieren",
            assignee="alice",
        )
        spawned = []
        kb.dispatch_once(conn, spawn_fn=lambda t, w, **k: spawned.append(t.id))
        assert spawned == [tid]


def test_go_gate_releases_terminal_card_after_explicit_unblock(
    kanban_home, all_assignees_spawnable,
):
    # The GO path: a parked terminal card runs once Till re-assigns a real
    # profile and unblocks it (the 'unblocked' event = human GO). Without
    # this release valve the broadened gate would re-park it forever.
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Merge PR #123 nach main")
        first = []
        kb.dispatch_once(
            conn, spawn_fn=lambda t, w, **k: first.append(t.id),
            default_assignee="default",
        )
        assert first == []
        assert kb.get_task(conn, tid).status == "blocked"
        # Till gives GO: re-assign a real profile + explicit unblock.
        kb.assign_task(conn, tid, "alice")
        assert kb.unblock_task(conn, tid) is True
        assert kb.get_task(conn, tid).status == "ready"
        # Second tick: released → spawns instead of re-parking.
        second = []
        kb.dispatch_once(conn, spawn_fn=lambda t, w, **k: second.append(t.id))
        assert second == [tid]


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


def test_go_gate_ignores_spec_card_with_merge_word_only_in_body(
    kanban_home, all_assignees_spawnable,
):
    # The fixed false-positive (diagnosed live 2026-09-29): a spec card whose
    # title has no terminal vocabulary but whose body carries the boundary
    # note "kein Merge ohne Tills GO" was wrongly parked, because merge\w* was
    # matched against title+body concatenated. The full regex now applies to
    # the TITLE only; a bare "merge" in a body must NOT gate the card.
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Spezifikation: chat_v2 Queue-API baureif festlegen",
            body=(
                "Nur eine Spezifikation erstellen. Grenze: kein Merge ohne "
                "Tills GO. Kein Release, kein PR in diesem Schritt."
            ),
            assignee="alice",
        )
        spawned = []
        kb.dispatch_once(conn, spawn_fn=lambda t, w, **k: spawned.append(t.id))
        assert spawned == [tid]  # runs — no longer parked
        assert kb.get_task(conn, tid).assignee == "alice"


def test_go_gate_holds_deploy_card_with_terminal_phrase_only_in_body(
    kanban_home, all_assignees_spawnable,
):
    # The safety net that title-only would have thrown away: a card whose
    # title is ordinary but whose body names an unambiguous terminal action
    # ("go-live auf prod") must STILL be parked. _GO_GATE_BODY_RE keeps the
    # dangerous deploy/go-live/smoke class gated even from a body mention.
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="chat_v2 Queue-API fertigstellen",
            body="Letzter Schritt: go-live auf prod, danach Monitoring prüfen.",
            assignee="hermes-coder",
        )
        spawned = []
        kb.dispatch_once(conn, spawn_fn=lambda t, w, **k: spawned.append(t.id))
        assert spawned == []
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.assignee == "till"
        assert task.block_kind == "needs_input"


def test_go_gate_holds_merge_card_with_merge_word_in_title(
    kanban_home, all_assignees_spawnable,
):
    # Regression: a genuine merge card carrying "merge" in its TITLE is still
    # gated after the title/body split (the merge class stays covered via
    # title + the structural legs, only body-only mentions were dropped).
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="W3-05 nach main mergen",
            body="Feature-Branch in main integrieren.",
            assignee="alice",
        )
        spawned = []
        kb.dispatch_once(conn, spawn_fn=lambda t, w, **k: spawned.append(t.id))
        assert spawned == []
        assert kb.get_task(conn, tid).assignee == "till"


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


@pytest.mark.parametrize(
    ("repository", "remote", "branch", "expected_file", "unexpected_file"),
    [
        (
            "hermes-agent", "fork", "feature/fork-review",
            "fork-feature.txt", "local-decoy.txt",
        ),
        (
            "goya", "origin", "feature/origin-review",
            "origin-feature.txt", "local-decoy.txt",
        ),
    ],
)
def test_review_dispatch_reads_feature_branch_from_repository_profile_remote(
    kanban_home, tmp_path, all_assignees_spawnable,
    repository, remote, branch, expected_file, unexpected_file,
):
    """Review checkout must read the feature ref from the profile's remote.

    The local branch is a deliberately different decoy.  Before the fix, the
    hermes case fetched only origin, missed the fork-only branch, and reviewed
    that local decoy instead.  The goya control continues reading origin.
    """
    origin = tmp_path / "origin.git"
    fork = tmp_path / "fork.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    _run(["git", "init"], seed)
    _run(["git", "config", "user.email", "t@t.com"], seed)
    _run(["git", "config", "user.name", "T"], seed)
    _run(["git", "checkout", "-b", "main"], seed)
    _commit(seed, "README.md", "base")
    _run(["git", "init", "--bare", str(origin)], tmp_path)
    _run(["git", "init", "--bare", str(fork)], tmp_path)
    _run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], origin)
    _run(["git", "remote", "add", "origin", str(origin)], seed)
    _run(["git", "remote", "add", "fork", str(fork)], seed)
    _run(["git", "push", "origin", "main"], seed)
    _run(["git", "push", "fork", "main"], seed)

    feature_file = "fork-feature.txt" if remote == "fork" else "origin-feature.txt"
    _run(["git", "checkout", "-b", branch], seed)
    _commit(seed, feature_file, f"{remote} feature")
    _run(["git", "push", remote, branch], seed)
    _run(["git", "checkout", "main"], seed)

    repo = tmp_path / "repo"
    _run(["git", "clone", str(origin), str(repo)], tmp_path)
    _run(["git", "remote", "add", "fork", str(fork)], repo)
    _run(["git", "config", "user.email", "t@t.com"], repo)
    _run(["git", "config", "user.name", "T"], repo)
    _run(["git", "checkout", "-b", branch], repo)
    _commit(repo, "local-decoy.txt", "local decoy")

    coder_wt = repo.parent / "coder-wt"
    # The root checkout owns the decoy branch, so materialize the coder
    # workspace detached at that same local commit.
    _run(["git", "worktree", "add", "--detach", str(coder_wt), branch], repo)
    tid = None
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title=f"Repository: {repository}", assignee="alice",
            workspace_kind="worktree", workspace_path=str(coder_wt),
            branch_name=branch,
        )
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (tid,))
        assert kb.block_task(conn, tid, reason="review it", kind="review")
        spawned = {}
        kb.dispatch_once(conn, spawn_fn=lambda task, workspace, **kw: spawned.setdefault(task.id, workspace))

    review_ws = Path(spawned[tid])
    assert (review_ws / expected_file).exists()
    assert not (review_ws / unexpected_file).exists()


def test_done_verification_covers_goal_mode_direct_complete(
    kanban_home, repo_with_remote,
):
    """A goal_mode worktree coder completing WITHOUT the review flow is
    still reality-checked — an unpushed branch must not reach done
    (live-repro t_05914cf9: false 'pushed' claim slipped past the gate)."""
    with kb.connect() as conn:
        wt = repo_with_remote.parent / "wt-goal"
        kb._ensure_git_worktree(repo_with_remote, wt, "wt/goal-branch")
        tid = kb.create_task(
            conn, title="goal worktree task", assignee="alice",
            workspace_kind="worktree", workspace_path=str(wt),
            branch_name="wt/goal-branch", tenant="voicera", goal_mode=True,
        )
        conn.execute(
            "UPDATE tasks SET status = 'running' WHERE id = ?", (tid,),
        )
        claimed = kb.claim_task(conn, tid)
        with pytest.raises(kb.DoneVerificationError):
            kb.complete_task(
                conn, tid, result="done", summary="pushed and tested",
            )
