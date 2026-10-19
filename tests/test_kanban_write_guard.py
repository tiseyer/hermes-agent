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

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from tests import conftest as _conftest


_CHILD_CREATE_SENTINEL = """
from hermes_cli import kanban_db as kb

conn = kb.connect()
try:
    kb.create_task(conn, title="child-process-sentinel")
finally:
    conn.close()
"""


_CHILD_INIT_DB = """
from hermes_cli import kanban_db as kb

kb.init_db()
"""


def _isolated_child_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """Return a test-marked child env with every Hermes DB/home pin removed."""
    env = os.environ.copy()
    simulated_home = tmp_path / "simulated-user-home"
    env["HOME"] = str(simulated_home)
    env["HERMES_TEST_ISOLATION"] = "1"
    for name in tuple(env):
        if name.startswith("PYTEST_"):
            env.pop(name)
    for name in (
        "HERMES_HOME",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
    ):
        env.pop(name, None)
    return env, simulated_home


def test_child_process_requires_explicit_test_db(tmp_path):
    """The test marker fails closed in children that lose every DB/home pin."""
    env, simulated_home = _isolated_child_env(tmp_path)

    unpinned = subprocess.run(
        [sys.executable, "-c", _CHILD_CREATE_SENTINEL],
        cwd=_conftest.PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    simulated_prod_db = simulated_home / ".hermes" / "kanban.db"
    assert unpinned.returncode != 0
    assert "HERMES_TEST_ISOLATION" in unpinned.stderr
    assert not simulated_prod_db.exists()

    isolated_db = tmp_path / "explicit-test-kanban.db"
    env["HERMES_KANBAN_DB"] = str(isolated_db)
    pinned = subprocess.run(
        [sys.executable, "-c", _CHILD_CREATE_SENTINEL],
        cwd=_conftest.PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert pinned.returncode == 0, pinned.stderr
    with sqlite3.connect(isolated_db) as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM tasks WHERE title = ?",
            ("child-process-sentinel",),
        ).fetchone()[0] == 1


def test_child_init_db_requires_explicit_test_db(tmp_path):
    """init_db must not turn an implicit production path explicit."""
    env, simulated_home = _isolated_child_env(tmp_path)
    command = [sys.executable, "-c", _CHILD_INIT_DB]

    unpinned = subprocess.run(
        command,
        cwd=_conftest.PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    simulated_prod_db = simulated_home / ".hermes" / "kanban.db"
    assert unpinned.returncode != 0
    assert "HERMES_TEST_ISOLATION" in unpinned.stderr
    assert not simulated_prod_db.exists()

    isolated_db = tmp_path / "explicit-init-test-kanban.db"
    env["HERMES_KANBAN_DB"] = str(isolated_db)
    pinned = subprocess.run(
        command,
        cwd=_conftest.PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert pinned.returncode == 0, pinned.stderr
    assert isolated_db.exists()
    with sqlite3.connect(isolated_db) as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


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
