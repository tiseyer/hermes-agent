"""Tests for the column-dwell clock (``status_changed_at``) and the escalation
``category`` field — the inbox-metadata build (P1 + P3).

P1: every real status transition stamps ``status_changed_at`` so the inbox can
show "wartet seit N Tagen in dieser Spalte" instead of card age. The stamp is
maintained purely by DB triggers, so no Python UPDATE site (present or future)
can forget it — including the family-root SQL trigger, which changes status
without any Python code path.

P3: a human/inbox card carries a ``category`` (Frage/Entscheidung/Abnahme/
Blocker/GO nötig) set at escalation time, so Till can batch his work.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Schema / migration
# ---------------------------------------------------------------------------

def test_fresh_db_has_both_columns(kanban_home):
    with kb.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert "status_changed_at" in cols
    assert "category" in cols


def test_legacy_migration_adds_and_backfills(tmp_path):
    """A pre-feature ``tasks`` shape gains both columns and seeds the dwell
    clock to ``created_at`` for existing rows (so nothing renders "gerade eben")."""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    # Minimal legacy shape: no status_changed_at, no category.
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
        "status TEXT NOT NULL, created_at INTEGER NOT NULL, "
        "workspace_kind TEXT NOT NULL DEFAULT 'scratch')"
    )
    # task_events must exist — the migration back-fills its run_id column.
    conn.execute(
        "CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "task_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT, "
        "created_at INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at) VALUES "
        "('t_old', 'old card', 'blocked', 1000)"
    )
    conn.commit()

    kb._migrate_add_optional_columns(conn)

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert "status_changed_at" in cols and "category" in cols
    row = conn.execute(
        "SELECT status_changed_at, category FROM tasks WHERE id = 't_old'"
    ).fetchone()
    assert row["status_changed_at"] == 1000, "dwell clock must seed to created_at"
    assert row["category"] is None
    conn.close()


def test_migrate_is_idempotent_on_partial_shape(tmp_path):
    """The #21708 isolation shape (no created_at, no status) must not raise —
    the backfill is guarded on created_at existing."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL)")
    conn.execute(
        "CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "task_id TEXT NOT NULL DEFAULT '', run_id INTEGER, "
        "kind TEXT NOT NULL DEFAULT '', payload TEXT, "
        "created_at INTEGER NOT NULL DEFAULT 0)"
    )
    # Must not raise.
    kb._migrate_add_optional_columns(conn)
    kb._migrate_add_optional_columns(conn)  # second pass = no-op
    conn.close()


# ---------------------------------------------------------------------------
# Dwell clock (status_changed_at)
# ---------------------------------------------------------------------------

def test_insert_seeds_status_changed_at_to_created_at(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="seed me", tenant="voicera")
        row = conn.execute(
            "SELECT created_at, status_changed_at FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()
    assert row["status_changed_at"] == row["created_at"]


def test_status_change_bumps_clock(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="move me", tenant="voicera")
        before = conn.execute(
            "SELECT status, status_changed_at FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        time.sleep(1)
        new_status = "running" if before["status"] != "running" else "review"
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = ? WHERE id = ?", (new_status, tid)
            )
        after = conn.execute(
            "SELECT status_changed_at FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
    assert after["status_changed_at"] > before["status_changed_at"]


def test_non_status_update_does_not_bump_clock(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="rename me", tenant="voicera")
        before = conn.execute(
            "SELECT status_changed_at FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        time.sleep(1)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET title = 'renamed' WHERE id = ?", (tid,)
            )
        after = conn.execute(
            "SELECT status_changed_at FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
    assert after["status_changed_at"] == before["status_changed_at"]


def test_same_status_write_does_not_bump_clock(kanban_home):
    """Writing status to its current value (a no-op transition) must not reset
    the dwell clock."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="idempotent status", tenant="voicera")
        st = conn.execute(
            "SELECT status, status_changed_at FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        time.sleep(1)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = ? WHERE id = ?", (st["status"], tid)
            )
        after = conn.execute(
            "SELECT status_changed_at FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
    assert after["status_changed_at"] == st["status_changed_at"]


def test_family_root_clock_only_bumps_on_aggregate_change(kanban_home):
    """The family-root trigger changes the root's status in pure SQL (no Python
    path). Its dwell stamp must advance ONLY when the root's aggregate status
    actually changes — a member move that leaves the aggregate unchanged must
    not jitter the root clock."""
    now = int(time.time())
    with kb.connect() as conn:
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO tasks(id,title,status,created_at,workspace_kind,"
                "family_root_id,child_role) VALUES"
                "('root','R','running',?, 'scratch','root','work')", (now - 1000,)
            )
            conn.execute(
                "INSERT INTO tasks(id,title,status,created_at,workspace_kind,"
                "family_root_id,family_order,child_role) VALUES"
                "('c1','C1','running',?, 'scratch','root',1,'work')", (now - 1000,)
            )
            conn.execute(
                "INSERT INTO tasks(id,title,status,created_at,workspace_kind,"
                "family_root_id,family_order,child_role) VALUES"
                "('c2','C2','running',?, 'scratch','root',2,'work')", (now - 1000,)
            )
        # Pin the root clock to a known-old value.
        conn.execute(
            "UPDATE tasks SET status_changed_at = ? WHERE id = 'root'", (now - 1000,)
        )
        root0 = conn.execute(
            "SELECT status, status_changed_at FROM tasks WHERE id = 'root'"
        ).fetchone()

        # c1 running->review: c2 still running => root stays 'running'. No bump.
        time.sleep(1)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = 'c1'")
        root1 = conn.execute(
            "SELECT status, status_changed_at FROM tasks WHERE id = 'root'"
        ).fetchone()
        assert root1["status"] == "running"
        assert root1["status_changed_at"] == root0["status_changed_at"]

        # c2 running->blocked: root aggregate becomes 'blocked'. Bump expected.
        time.sleep(1)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = 'c2'")
        root2 = conn.execute(
            "SELECT status, status_changed_at FROM tasks WHERE id = 'root'"
        ).fetchone()
        assert root2["status"] == "blocked"
        assert root2["status_changed_at"] > root0["status_changed_at"]


# ---------------------------------------------------------------------------
# Category threading
# ---------------------------------------------------------------------------

def test_move_to_human_sets_category_and_event(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="decide this", tenant="voicera")
        out = kb.move_to_human(conn, tid, category="entscheidung", actor="mgr")
        row = conn.execute(
            "SELECT status, category FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        assert out == "moved"
        assert row["status"] == "human"
        assert row["category"] == "entscheidung"
        events = [e for e in kb.list_events(conn, tid) if e.kind == "human"]
        assert events and events[-1].payload.get("category") == "entscheidung"


def test_move_to_human_rejects_unknown_category(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="bad cat", tenant="voicera")
        with pytest.raises(ValueError, match="category must be one of"):
            kb.move_to_human(conn, tid, category="nonsense")
        # Rejected before any write: card stays put.
        assert kb.get_task(conn, tid).status != "human"


def test_move_to_human_without_category_leaves_null(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="no cat", tenant="voicera")
        kb.move_to_human(conn, tid, actor="mgr")
        assert kb.get_task(conn, tid).category is None


def test_create_human_card_sets_category(kanban_home):
    with kb.connect() as conn:
        hid = kb.create_human_card(
            conn, title="need an answer", category="frage", tenant="voicera"
        )
        row = conn.execute(
            "SELECT status, category FROM tasks WHERE id = ?", (hid,)
        ).fetchone()
    assert row["status"] == "human"
    assert row["category"] == "frage"


def test_create_human_card_rejects_unknown_category(kanban_home):
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="category must be one of"):
            kb.create_human_card(conn, title="x", category="nope")


def test_go_gate_park_tags_go_noetig(kanban_home, all_assignees_spawnable):
    """A terminal card parked by the GO-gate gets category='go_noetig' so the
    inbox pill shows it needs Till's explicit GO."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="Smoke: Preview-Proxy prüfen und deployen"
        )
        kb.dispatch_once(
            conn, spawn_fn=lambda t, w, **k: None, default_assignee="default"
        )
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.assignee == "till"
        assert task.category == "go_noetig"


def test_category_exposed_on_task_dataclass(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="t", tenant="voicera")
        kb.move_to_human(conn, tid, category="abnahme")
        task = kb.get_task(conn, tid)
    assert task.category == "abnahme"
    assert task.status_changed_at is not None
