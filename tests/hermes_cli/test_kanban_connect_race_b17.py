"""B1.7 — steady-state kanban connect() must never WRITE journal_mode.

Regression for kanban.db corruption #8 (2026-11-06): the fast connect() path
runs without the cross-process init lock. When gateway dispatcher + voicera-api
+ dashboard open connections at once and the file is not already WAL, each one
races to run ``PRAGMA journal_mode=WAL`` — a schema/header write that clobbers
``sqlite_master`` (``malformed database schema (sync_family_root_after_insert)
- unrecognized token: ""``). The fix: the fast path passes
``enforce_preset=True`` so it only READS journal_mode and fails loudly if the
file is not WAL (WAL is set once offline / on the cross-process-locked slow
path and persists in the header).
"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from hermes_state import apply_wal_with_fallback
from hermes_cli import kanban_db


def _count_wal_write_factory(counter: list):
    """sqlite3.Connection subclass that counts ``PRAGMA journal_mode=WAL`` writes."""

    class _Counting(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):  # type: ignore[override]
            if "journal_mode=wal" in sql.lower().replace(" ", ""):
                counter[0] += 1
            return super().execute(sql, *args, **kwargs)

    return _Counting


def test_enforce_preset_returns_wal_without_writing(tmp_path):
    """On a WAL file, the guard returns 'wal' via a read-only probe, no write."""
    db = tmp_path / "k.db"
    conn0 = sqlite3.connect(str(db))
    conn0.execute("PRAGMA journal_mode=WAL")
    conn0.close()

    writes = [0]
    conn = sqlite3.connect(str(db), factory=_count_wal_write_factory(writes))
    try:
        assert apply_wal_with_fallback(conn, db_label="kanban.db (k.db)",
                                       enforce_preset=True) == "wal"
    finally:
        conn.close()
    assert writes[0] == 0, "enforce_preset path must not run PRAGMA journal_mode=WAL"


def test_enforce_preset_raises_loudly_on_non_wal(tmp_path):
    """On a non-WAL file, the guard fails loudly instead of self-healing."""
    db = tmp_path / "k.db"
    conn0 = sqlite3.connect(str(db))
    conn0.execute("PRAGMA journal_mode=DELETE")
    conn0.close()

    writes = [0]
    conn = sqlite3.connect(str(db), factory=_count_wal_write_factory(writes))
    try:
        with pytest.raises(RuntimeError, match="journal_mode"):
            apply_wal_with_fallback(conn, db_label="kanban.db (k.db)",
                                    enforce_preset=True)
    finally:
        conn.close()
    assert writes[0] == 0, "must not attempt the WAL write before failing loudly"


def test_default_still_sets_wal(tmp_path):
    """Without enforce_preset (slow path / SessionDB) WAL is still activated."""
    db = tmp_path / "k.db"
    conn = sqlite3.connect(str(db))
    try:
        assert apply_wal_with_fallback(conn, db_label="state.db") == "wal"
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
    finally:
        conn.close()


def test_20_parallel_connect_preserves_schema(tmp_path):
    """20 concurrent connect() calls against one WAL kanban.db leave the schema
    parseable, integrity 'ok', and journal_mode still 'wal'.

    The first connect() inits + sets WAL on the cross-process-locked slow path;
    all subsequent ones (including the 20 threads) take the enforce_preset fast
    path. Before the fix those raced on PRAGMA journal_mode=WAL and corrupted
    sqlite_master.
    """
    db = tmp_path / "kanban.db"
    # Warm up: init schema + activate WAL (slow path), populate _INITIALIZED_PATHS.
    with kanban_db.connect_closing(db_path=db) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    errors: list = []

    def hammer(_i):
        try:
            for _ in range(25):
                with kanban_db.connect_closing(db_path=db) as c:
                    # Force a full schema parse (triggers + tables) on every open.
                    c.execute("SELECT name FROM sqlite_master "
                              "WHERE type='trigger'").fetchall()
                    c.execute("SELECT count(*) FROM tasks").fetchone()
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(repr(exc))

    with ThreadPoolExecutor(max_workers=20) as ex:
        futures = [ex.submit(hammer, i) for i in range(20)]
        for f in as_completed(futures):
            f.result()

    assert not errors, f"concurrent connect() raised: {errors[:3]}"

    with kanban_db.connect_closing(db_path=db) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        # Schema still parses: listing triggers would raise "malformed database
        # schema" on the corruption we are guarding against.
        conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()
