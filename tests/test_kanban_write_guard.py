"""Negative tests: the kanban write guard refuses production-DB writes.

Proof for Paket 3 (Test-Isolation): a test that tries to open the REAL
``~/.hermes`` kanban DB — explicitly or via environment resolution — must
fail hard at the ``_kanban_write_guard`` autouse fixture instead of
silently polluting the operator's live board (root/child cards and the
seven ``/tmp`` test cards of 2026-09-23 were exactly this failure mode).

``kanban_db`` is imported at MODULE level on purpose: the guard fixture
patches ``connect`` only when the module is already in ``sys.modules`` at
fixture-setup time.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from tests import conftest as _conftest


def test_explicit_db_path_under_real_root_refused():
    """connect(db_path=<real kanban.db>) must be refused by the guard."""
    real_db = _conftest._REAL_KANBAN_ROOT / "kanban.db"
    with pytest.raises(RuntimeError, match="kanban_write_guard"):
        kb.connect(db_path=real_db)


def test_explicit_board_db_path_under_real_root_refused():
    """Board DBs under <real root>/kanban/boards/... are refused too."""
    real_board_db = (
        _conftest._REAL_KANBAN_ROOT / "kanban" / "boards" / "default" / "kanban.db"
    )
    with pytest.raises(RuntimeError, match="kanban_write_guard"):
        kb.connect(db_path=real_board_db)


def test_hermes_home_reset_to_production_refused(monkeypatch):
    """A test that points HERMES_HOME back at the real home must fail.

    This is the Bauplan's canonical negative test: without a tempdir the
    env-based resolution chain (``kanban_home()`` → ``kanban_db_path()``)
    lands under the real ``~/.hermes`` and the guard must refuse.
    """
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(Path.home() / ".hermes"))
    with pytest.raises(RuntimeError, match="kanban_write_guard"):
        kb.connect()


def test_kanban_home_override_to_production_refused(monkeypatch):
    """HERMES_KANBAN_HOME pointing at the real root is refused as well."""
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(_conftest._REAL_KANBAN_ROOT))
    with pytest.raises(RuntimeError, match="kanban_write_guard"):
        kb.connect()


def test_hermetic_tempdir_write_still_allowed(tmp_path, monkeypatch):
    """Positive control: hermetic tests are unaffected by the deny-list."""
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    conn = kb.connect()
    try:
        assert (tmp_path / "kanban-home" / "kanban.db").exists()
    finally:
        conn.close()


def test_session_sandbox_was_active_at_conftest_import():
    """Collection-time imports must never have seen a production HERMES_HOME.

    ``HERMES_HOME_AT_CONFTEST_IMPORT`` is the value AFTER the session
    sandbox ran — if it still points at production, the pre-collection
    sandbox is broken (logging handlers and frozen DB paths would target
    the real ``~/.hermes``).
    """
    assert _conftest.HERMES_HOME_AT_CONFTEST_IMPORT, (
        "HERMES_HOME was empty at conftest import — session sandbox missing"
    )
    assert not _conftest._hermes_home_points_at_production(
        _conftest.HERMES_HOME_AT_CONFTEST_IMPORT
    ), (
        "HERMES_HOME pointed at the production root when conftest was "
        "imported — the session sandbox did not run before collection"
    )


def test_isolation_marker_exported(monkeypatch):
    """HERMES_TEST_ISOLATION is set so spawned children inherit isolation."""
    import os

    assert os.environ.get("HERMES_TEST_ISOLATION"), (
        "HERMES_TEST_ISOLATION missing — subprocesses would not inherit "
        "the test-isolation signal"
    )
