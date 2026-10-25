"""F55 — an owner/human DECISION card must rest in ``blocked@till`` (the
Beobachter's ``blocked_zielfrage`` bucket) and may NEVER sit in ``scheduled``.

Live repro: Karte 47 ``t_cbddd945`` — an answered owner Zielentscheidung
("Strategie fuer Environment Variables sicher speichern", category
``entscheidung``) was parked into ``scheduled`` by a cleanup/defer pass
("Bau nach 31.10. -> geparkt"). ``scheduled`` is non-dispatchable and invisible
to both ``recompute_ready`` and the GO-gate re-park, so the card stranded for
84.8h and the board scan read it as ``rot-stuck`` (the Beobachter greens a
decision card only in blocked-with-Zielfrage / done / ready@merger).

Two legs, mirroring the F52 prevent+rescue pattern:

* ``schedule_task`` REFUSES to park a decision card (prevent).
* ``recompute_ready`` RESCUES a decision card that already leaked into
  ``scheduled`` back to ``blocked@till`` with a sticky ``blocked`` event
  (rescue + defense in depth).

Both are narrowly scoped — a legit time-deferred WORK card is untouched.
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


def _set(conn, tid: str, **cols) -> None:
    sets = ", ".join(f"{c} = ?" for c in cols)
    conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?", [*cols.values(), tid])


# ---------------------------------------------------------------------------
# Leg 1 — schedule_task refuses a decision card
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("category", sorted(kb._HUMAN_DECISION_CATEGORIES))
def test_schedule_task_refuses_decision_category(kanban_home: Path, category: str) -> None:
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="owner decision")
        _set(conn, tid, status="todo", category=category, assignee="till")

        assert kb.schedule_task(conn, tid, reason="defer") is False
        # Card stays where it was — NOT scheduled.
        assert kb.get_task(conn, tid).status == "todo"


def test_schedule_task_refuses_blocked_zielfrage_card(kanban_home: Path) -> None:
    """Even without a decision category, an already-blocked business
    Zielfrage (the Beobachter's resting bucket) must not be scheduled."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="where to store secrets")
        kb.claim_task(conn, tid)
        kb.block_task(
            conn, tid,
            reason="Welches Verfuegbarkeitsziel soll vor dem ersten Kundeneinsatz gelten?",
            kind="needs_input",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        kb.add_comment(
            conn, tid, "specifier",
            "Soll der Ausfall eines einzelnen Tresor-Rechners keine "
            "Kundenunterbrechung verursachen duerfen?",
        )
        assert kb.get_task(conn, tid).status == "blocked"

        assert kb.schedule_task(conn, tid, reason="defer") is False
        assert kb.get_task(conn, tid).status == "blocked"


def test_schedule_task_allows_plain_work_card(kanban_home: Path) -> None:
    """A normal card (no decision marker) can still be time-deferred."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="rotate logs nightly")
        _set(conn, tid, status="ready")

        assert kb.schedule_task(conn, tid, reason="run after midnight") is True
        assert kb.get_task(conn, tid).status == "scheduled"


# ---------------------------------------------------------------------------
# Leg 2 — recompute_ready rescues a leaked scheduled decision card
# ---------------------------------------------------------------------------


def test_recompute_ready_rescues_scheduled_decision_card(kanban_home: Path) -> None:
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="owner decision")
        # Force the illegitimate state the guard would normally block (e.g. a
        # direct DB write / dashboard drag that predates the guard).
        _set(conn, tid, status="scheduled", category="entscheidung", assignee="till")

        changed = kb.recompute_ready(conn)
        assert changed >= 1

        t = kb.get_task(conn, tid)
        assert t.status == "blocked"
        assert t.assignee == "till"
        assert t.category == "entscheidung"

        # Sticky: it rests in blocked across subsequent ticks (never re-promoted,
        # never re-scheduled).
        for _ in range(3):
            kb.recompute_ready(conn)
            assert kb.get_task(conn, tid).status == "blocked"


def test_recompute_ready_rescues_blank_category_held_owner_decision(kanban_home: Path) -> None:
    """F54-style: a held owner decision with a BLANK category is still rescued
    via the sticky ``owner_decision_loop`` blocked event."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="owner decision")
        _set(conn, tid, status="scheduled", category=None, assignee="till")
        kb._append_event(
            conn, tid, "blocked",
            {"kind": "needs_input", "escalation": "owner_decision_loop"},
        )

        kb.recompute_ready(conn)
        t = kb.get_task(conn, tid)
        assert t.status == "blocked"
        assert t.category == "entscheidung"  # stamped, mirrors F54


def test_recompute_ready_leaves_plain_scheduled_card(kanban_home: Path) -> None:
    """A legit time-deferred WORK card stays scheduled — the rescue is narrow."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="rotate logs nightly")
        _set(conn, tid, status="scheduled")

        kb.recompute_ready(conn)
        assert kb.get_task(conn, tid).status == "scheduled"
