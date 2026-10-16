"""Tests for typed block reasons + the unblock-loop breaker.

Covers the built-in fix for the kanban "blocked loop" — a worker blocks a
task, a cron unblocks it, the worker re-blocks for the same reason, repeat
forever. The fix gives ``block_task`` a typed ``kind`` and a persistent
``block_recurrences`` counter:

* ``dependency`` blocks route to ``todo`` (parent-gated, auto-resumed) and
  never enter the human ``blocked`` bucket a cron would keep unblocking.
* ``needs_input`` / ``capability`` / un-typed blocks land in ``blocked``;
  each same-cause re-block after an unblock increments ``block_recurrences``,
  and at ``BLOCK_RECURRENCE_LIMIT`` the task routes to ``triage`` for a human.
* ``unblock_task`` deliberately does NOT reset ``block_recurrences`` (the
  amnesia that let the loop run unbounded).
* A successful ``complete_task`` resets the loop memory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, title="t"):
    """Create a task and drive it to ``running`` so block_task can act."""
    tid = kb.create_task(conn, title=title, assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    claimed = kb.claim_task(conn, tid, claimer="worker")
    assert claimed is not None
    return tid


def _make_running_again(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None


# ---------------------------------------------------------------------------
# Loop breaker
# ---------------------------------------------------------------------------


def test_first_typed_block_lands_in_blocked(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(conn, tid, reason="which key?", kind="needs_input")
        t = kb.get_task(conn, tid)
        assert t.status == "blocked"
        assert t.block_kind == "needs_input"
        assert t.block_recurrences == 1


def test_unblock_does_not_reset_recurrence_counter(kanban_home: Path) -> None:
    """The crux of the fix: unblock must preserve the loop counter."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="x", kind="needs_input")
        assert kb.get_task(conn, tid).block_recurrences == 1
        assert kb.unblock_task(conn, tid)
        t = kb.get_task(conn, tid)
        assert t.status == "ready"
        assert t.block_recurrences == 1  # NOT reset to 0
        assert t.block_kind == "needs_input"  # kind preserved for comparison


def test_same_cause_reblock_routes_to_triage(kanban_home: Path) -> None:
    """Dale's loop: block → unblock → re-block same kind → triage."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="need creds", kind="needs_input")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="still need creds", kind="needs_input")
        t = kb.get_task(conn, tid)
        assert t.status == "triage"
        assert t.block_recurrences == 2


def test_untyped_block_loop_also_protected(kanban_home: Path) -> None:
    """Legacy un-typed blocks (kind=None) still trip the breaker."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="a")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="a again")
        assert kb.get_task(conn, tid).status == "triage"


def test_different_kinds_do_not_compound(kanban_home: Path) -> None:
    """A re-block for a DIFFERENT reason resets the counter to 1."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="a", kind="needs_input")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="b", kind="capability")
        t = kb.get_task(conn, tid)
        assert t.status == "blocked"
        assert t.block_recurrences == 1


def test_block_loop_detected_event_emitted(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="x", kind="capability")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="x", kind="capability")
        events = [e for e in kb.list_events(conn, tid)
                  if e.kind == "block_loop_detected"]
        assert events, "expected a block_loop_detected event"
        payload = events[-1].payload or {}
        assert payload.get("recurrences") == 2
        assert payload.get("kind") == "capability"


def test_owner_zielentscheidung_loop_routes_to_blocked_till(
    kanban_home: Path,
) -> None:
    """F50: a needs_input loop whose card carries a genuine non-technical
    business ``?``-comment is an owner Zielentscheidung — it escalates to
    ``blocked@till`` (Beobachter rule b), NOT to ``triage`` (which would
    auto-decompose the goal-dispute into deadlocking autonomous sub-work)."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn, title="Strategie für Secrets")
        # The factory raised a real owner goal-question on the card.
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) "
                "VALUES (?, ?, ?, ?)",
                (tid, "voicera-reviewer",
                 "Soll die zentrale Verwahrung durch verschlüsselte Ablage je "
                 "Kundeninstanz ersetzt werden, oder nur zur Erprobung?",
                 0),
            )
        kb.block_task(conn, tid, reason="Zielwiderspruch", kind="needs_input")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="Tills Ziele fehlen noch",
                      kind="needs_input")
        t = kb.get_task(conn, tid)
        assert t.status == "blocked"
        assert t.assignee == "till"
        assert t.block_kind == "needs_input"
        # Sticky block → recompute_ready must NOT auto-recover it.
        kb.recompute_ready(conn)
        assert kb.get_task(conn, tid).status == "blocked"
        # Routed to the owner, not quarantined in triage / decomposed.
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert "owner_decision_loop_escalated" in kinds
        assert "block_loop_detected" not in kinds


def test_needs_input_loop_without_zielfrage_still_triages(
    kanban_home: Path,
) -> None:
    """A bare needs_input wall ("need creds") carries no business question, so
    the F50 owner-escalation must NOT fire — it keeps the triage quarantine."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="need creds", kind="needs_input")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="still need creds", kind="needs_input")
        assert kb.get_task(conn, tid).status == "triage"


# ---------------------------------------------------------------------------
# Dependency routing
# ---------------------------------------------------------------------------


def test_dependency_block_routes_to_todo(kanban_home: Path) -> None:
    """Dependency waits never enter the human 'blocked' bucket."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(conn, tid, reason="need X first", kind="dependency")
        t = kb.get_task(conn, tid)
        assert t.status == "todo"
        assert t.block_kind == "dependency"


def test_dependency_then_parent_done_promotes(kanban_home: Path) -> None:
    """A dependency-parked child becomes ready once its parent completes."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = _running_task(conn, title="child")
        kb.link_tasks(conn, parent_id=parent, child_id=child)
        kb.block_task(conn, child, reason="wait", kind="dependency")
        assert kb.get_task(conn, child).status == "todo"
        # Finish the parent, then let recompute_ready run.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        kb.claim_task(conn, parent, claimer="worker")
        kb.complete_task(conn, parent, result="done")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"


def test_dependency_go_released_parent_promotes_child(kanban_home: Path) -> None:
    """A child gated on a GO-released go_noetig parent promotes without `done`.

    F41 (todo-dependency starvation): a work card sequenced behind a Freigabe/
    merge parent (``category='go_noetig'``) must not starve in ``todo``. Such a
    parent never reaches ``done``/``archived`` — once the human GO is given it
    rests at ``ready @ merger`` (F46). recompute_ready must treat the explicit
    unblock (the human GO) as satisfying the dependency; otherwise the child
    rots in ``todo`` until the Beobachter trips ``rot-stuck`` (live repro:
    Karte 33 t_c2a3f4da ↦ t_d991a49d).
    """
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="Freigabe: config", assignee="worker")
        child = _running_task(conn, title="child")
        kb.link_tasks(conn, parent_id=parent, child_id=child)
        kb.block_task(conn, child, reason="wait Freigabe", kind="dependency")
        assert kb.get_task(conn, child).status == "todo"
        # Parent is a GO-gated Freigabe card parked at blocked@till, then the
        # human gives GO (unblock) — it rests at ready, never reaching `done`.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='ready' WHERE id=?", (parent,)
            )
        kb.claim_task(conn, parent, claimer="worker")
        assert kb.block_task(conn, parent, reason="Merge-GO nötig",
                             kind="needs_input")
        # The GO-gate park stamps the durable 'needs human GO' marker.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET category='go_noetig' WHERE id=?", (parent,)
            )
        assert kb.unblock_task(conn, parent)
        # Parent is NOT done — but go_noetig + GO-released ⇒ child promotes.
        assert kb.get_task(conn, parent).status != "done"
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"


def test_dependency_go_held_parent_keeps_child_waiting(kanban_home: Path) -> None:
    """A go_noetig parent still AWAITING GO keeps gating the child.

    The release signal is the human's explicit unblock. A Freigabe card parked
    at blocked@till that nobody has unblocked yet is an unmade decision — the
    child must keep waiting, not jump ahead.
    """
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="Freigabe: config", assignee="worker")
        child = _running_task(conn, title="child")
        kb.link_tasks(conn, parent_id=parent, child_id=child)
        kb.block_task(conn, child, reason="wait Freigabe", kind="dependency")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='ready' WHERE id=?", (parent,)
            )
        kb.claim_task(conn, parent, claimer="worker")
        assert kb.block_task(conn, parent, reason="Merge-GO nötig",
                             kind="needs_input")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET category='go_noetig' WHERE id=?", (parent,)
            )
        # No unblock yet → GO not given → child stays in todo.
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "todo"


def test_dependency_go_released_then_parent_reblocks_other_gate_promotes_child(
    kanban_home: Path,
) -> None:
    """GO released, parent later re-blocks on an UNRELATED gate ⇒ child promotes.

    F41 recurrence (live repro: child t_e5183b37 ↦ parent t_d991a49d): the
    go_noetig parent's GO was granted (unblock), then the parent re-blocked on
    a *different* downstream gate — a ``capability`` push-approval block
    ("Push-Gate verlangt externe Approval"). The strict last-event predicate
    flipped back to ``blocked`` and the child starved in ``todo`` until the
    Beobachter tripped ``rot-stuck``. The child's dependency was the GO
    *decision*, which was made; a subsequent non-go_noetig re-block on the
    parent must not re-gate it.
    """
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="Freigabe: config", assignee="worker")
        child = _running_task(conn, title="child")
        kb.link_tasks(conn, parent_id=parent, child_id=child)
        kb.block_task(conn, child, reason="wait Freigabe", kind="dependency")
        assert kb.get_task(conn, child).status == "todo"
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='blocked', category='go_noetig' "
                "WHERE id=?", (parent,)
            )
            # The GO gate stamps category in the block-event payload (as the
            # F45 reroute does live), so the decision-aware predicate can find
            # it among later, unrelated blocks.
            kb._append_event(
                conn, parent, "blocked",
                {"kind": "needs_input", "category": "go_noetig",
                 "reason": "Merge-GO nötig"},
            )
        assert kb.unblock_task(conn, parent)
        # Parent now hits a SEPARATE downstream gate on its own execution.
        kb.claim_task(conn, parent, claimer="worker")
        assert kb.block_task(
            conn, parent,
            reason="Push-Gate verlangt externe Approval für git push",
            kind="capability",
        )
        assert kb.get_task(conn, parent).status != "done"
        # GO decision stands despite the later push-gate block ⇒ child promotes.
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"
        # …and the structural spawn gate must AGREE with the promotion path.
        # Before the fix, get_spawn_rejection_reason used a raw
        # status-NOT-IN-('done','archived') check and returned
        # "parents_not_done" for the GO-released-then-reblocked parent, so the
        # dispatcher's claim path demoted the child ready→todo every tick while
        # recompute_ready re-promoted it — a todo↔ready bounce that never spawns
        # and tripped the Beobachter's rot-stuck frist (live repro: Karte 33
        # t_5772dbbd behind t_d991a49d).
        assert kb.get_spawn_rejection_reason(conn, child) is None
        # The atomic claim must succeed and keep the child out of todo.
        assert kb.claim_task(conn, child, claimer="worker") is not None
        assert kb.get_task(conn, child).status == "running"


def test_dependency_go_reheld_keeps_child_waiting(kanban_home: Path) -> None:
    """A FRESH go_noetig re-hold after an unblock re-gates the child.

    Distinguishes the decision-aware predicate from "any later unblock wins":
    if the parent's most recent ``go_noetig`` block has no ``unblocked`` after
    it, the new GO is genuinely unmade and the child must keep waiting.
    """
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="Freigabe: config", assignee="worker")
        child = _running_task(conn, title="child")
        kb.link_tasks(conn, parent_id=parent, child_id=child)
        kb.block_task(conn, child, reason="wait Freigabe", kind="dependency")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='blocked', category='go_noetig' "
                "WHERE id=?", (parent,)
            )
            # First GO granted …
            kb._append_event(
                conn, parent, "blocked",
                {"kind": "needs_input", "category": "go_noetig"},
            )
        assert kb.unblock_task(conn, parent)
        # … then a NEW go_noetig hold with no unblock since → decision unmade.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (parent,))
            kb._append_event(
                conn, parent, "blocked",
                {"kind": "needs_input", "category": "go_noetig"},
            )
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "todo"


# ---------------------------------------------------------------------------
# F43 dependency-recut convergence ceiling
# ---------------------------------------------------------------------------


def test_dependency_recut_ceiling_escalates_to_blocked(kanban_home: Path) -> None:
    """A card re-parked as a dependency past the ceiling escalates to the owner.

    The open-ended "alle verbleibenden Begriffe" spiral: the orchestrator keeps
    linking a fresh autonomous recon child and re-parking THIS card in ``todo``.
    Those parents are not held human decisions, so the gate-fold declines; the
    N+1th park must instead convert to blocked@till with a prepared Freigabe
    Zielfrage (the Beobachter greens it rule b) rather than rot in ``todo``.
    """
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        # First two parks are allowed — they stay in ``todo`` (converging grace).
        assert kb.block_task(conn, tid, reason="wait recon 1", kind="dependency")
        assert kb.get_task(conn, tid).status == "todo"
        _make_running_again(conn, tid)
        assert kb.block_task(conn, tid, reason="wait recon 2", kind="dependency")
        assert kb.get_task(conn, tid).status == "todo"
        # Third re-cut park has provably failed to converge → owner escalation.
        _make_running_again(conn, tid)
        assert kb.block_task(conn, tid, reason="wait recon 3", kind="dependency")
        t = kb.get_task(conn, tid)
        assert t.status == "blocked"
        assert t.assignee == "till"
        assert t.block_kind == "needs_input"
        evs = [e for e in kb.list_events(conn, tid)
               if e.kind == "scope_recut_ceiling"]
        assert evs, "expected a scope_recut_ceiling event"
        assert (evs[-1].payload or {}).get("variant") == "dependency_park"
        # The prepared Freigabe Zielfrage is on the card as a comment.
        comments = kb.list_comments(conn, tid)
        assert any("ABNAHME/FREIGABE (GO)" in (c.body or "") for c in comments)


def test_dependency_recut_below_ceiling_stays_todo(kanban_home: Path) -> None:
    """Two dependency parks (a genuine recon wait) are NOT escalated."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="wait recon 1", kind="dependency")
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="wait recon 2", kind="dependency")
        t = kb.get_task(conn, tid)
        assert t.status == "todo"
        assert t.block_kind == "dependency"


# ---------------------------------------------------------------------------
# Completion resets loop memory
# ---------------------------------------------------------------------------


def test_completion_clears_block_memory(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="x", kind="capability")
        kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).block_recurrences == 1
        kb.complete_task(conn, tid, result="done")
        t = kb.get_task(conn, tid)
        assert t.status == "done"
        assert t.block_recurrences == 0
        assert t.block_kind is None


# ---------------------------------------------------------------------------
# Validation + back-compat
# ---------------------------------------------------------------------------


def test_invalid_kind_rejected(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        with pytest.raises(ValueError):
            kb.block_task(conn, tid, reason="x", kind="bogus")


def test_block_without_kind_is_backward_compatible(kanban_home: Path) -> None:
    """Existing callers that pass no kind keep the old single-block behaviour."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(conn, tid, reason="legacy")
        t = kb.get_task(conn, tid)
        assert t.status == "blocked"
        assert t.block_kind is None
