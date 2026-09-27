"""Regression guard for the stress-test HERMES_KANBAN_DB inheritance bug.

RCA 2026-09-27 (W3-01): a dispatcher-spawned worker ran a stress script with
HERMES_KANBAN_DB inherited from the dispatcher (= the production board). Because
that env var has the highest precedence in ``kanban_db.kanban_db_path()``, the
stress script mutated the shared board (200+ ``child`` rows, torn-extend during
``complete_task``).

These tests run in the normal suite (NOT under tests/stress/, which the stress
conftest skips) so the fix can never silently regress.
"""
import os
import sys
from pathlib import Path

import pytest

_STRESS_DIR = Path(__file__).resolve().parent / "stress"
sys.path.insert(0, str(_STRESS_DIR))
import _isolation  # noqa: E402


@pytest.fixture
def restore_env():
    saved = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_isolate_home_ignores_inherited_prod_db(restore_env, tmp_path):
    """isolate_home() must win over an inherited (prod) HERMES_KANBAN_DB."""
    prod = tmp_path / "prod" / "kanban.db"
    os.environ["HERMES_KANBAN_DB"] = str(prod)  # simulate dispatcher injection

    home = _isolation.isolate_home(prefix="hermes_isolation_test_")

    from hermes_cli import kanban_db as kb
    resolved = Path(kb.kanban_db_path()).resolve()
    # The DB must live inside the fresh temp HOME, never the injected prod path.
    assert str(resolved).startswith(str(Path(home).resolve()))
    assert resolved != prod.resolve()


def test_drop_inherited_pins_removes_kanban_db(restore_env):
    """drop_inherited_pins() clears every inherited kanban path pin."""
    os.environ["HERMES_KANBAN_DB"] = "/home/whoever/.hermes/kanban.db"
    os.environ["HERMES_KANBAN_WORKSPACES_ROOT"] = "/somewhere/ws"

    _isolation.drop_inherited_pins()

    assert "HERMES_KANBAN_DB" not in os.environ
    assert "HERMES_KANBAN_WORKSPACES_ROOT" not in os.environ


def test_assert_isolated_raises_when_db_escapes_home(restore_env, tmp_path):
    """The guard aborts if the resolved DB would land outside the temp HOME."""
    home = tmp_path / "home"
    home.mkdir()
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HOME"] = str(home)
    # A leaked pin pointing outside HOME must be caught, not silently used.
    os.environ["HERMES_KANBAN_DB"] = str(tmp_path / "elsewhere" / "kanban.db")

    with pytest.raises(RuntimeError, match="isolation breach"):
        _isolation.assert_isolated(str(home))
