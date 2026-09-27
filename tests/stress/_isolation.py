"""Shared HOME/DB isolation for stress scripts.

WHY THIS EXISTS (RCA 2026-09-27, W3-01):
A stress script that only sets ``HERMES_HOME``/``HOME`` to a temp dir still
runs against the *production* board. ``kanban_db.kanban_db_path()`` honours
``HERMES_KANBAN_DB`` with the **highest** precedence (defence-in-depth for the
dispatcher→worker handoff: the dispatcher injects ``HERMES_KANBAN_DB`` into
every worker env). So when a stress script is launched by a dispatcher-spawned
worker (e.g. a reviewer running ``python tests/stress/<name>.py``), the inherited
``HERMES_KANBAN_DB=~/.hermes/kanban.db`` overrides the temp HOME and the test
mutates the shared board — it created 200+ ``child`` rows and triggered a
``torn-extend`` during ``complete_task`` on the live DB.

RULE (Destillat): stress-/isolation tests must NEVER see the inherited
production DB variable. They must force their own HOME *and* their own temp DB,
independent of any inherited ``HERMES_KANBAN_*`` path pin.

Use ``isolate_home()`` at the very start of every stress ``run()``, before
importing ``hermes_cli.kanban_db``.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Env vars that pin a kanban path directly. Any of these, if inherited from a
# dispatcher-injected worker env, would redirect a stress run at the shared
# production board. They must be dropped before the temp home is pinned.
_LEAKY_KANBAN_ENV = (
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_WORKSPACES_ROOT",
)


def drop_inherited_pins() -> None:
    """Drop inherited kanban path pins so a script derives its DB from HOME.

    Minimal systemic fix for scripts that manage their own (possibly multiple)
    temp HOMEs: call this once at module load / before any DB access. It removes
    the dispatcher-injected ``HERMES_KANBAN_DB`` (and workspaces-root) so
    ``kanban_db_path()`` falls through to the script's ``HERMES_HOME``/``HOME``
    instead of the shared production board. Unlike :func:`isolate_home` it does
    not pin a fixed DB path, so it is safe for scripts that switch HOME between
    several temp dirs during a run.
    """
    for var in _LEAKY_KANBAN_ENV:
        os.environ.pop(var, None)


def isolate_home(prefix: str = "hermes_stress_") -> str:
    """Create a throwaway HOME and pin every kanban path inside it.

    Returns the temp home path. Call this before importing
    ``hermes_cli.kanban_db``. Raises :class:`RuntimeError` if, after
    isolation, the resolved kanban DB path would still land outside the temp
    home — a hard regression guard against the HERMES_KANBAN_DB-inheritance
    bug (RCA 2026-09-27).
    """
    home = tempfile.mkdtemp(prefix=prefix)
    os.environ["HERMES_HOME"] = home
    os.environ["HOME"] = home
    # Drop inherited path pins (the dispatcher injects HERMES_KANBAN_DB=prod).
    for var in _LEAKY_KANBAN_ENV:
        os.environ.pop(var, None)
    # Pin the DB explicitly inside the temp home — defence in depth, so the
    # result is independent of board-slug resolution.
    os.environ["HERMES_KANBAN_DB"] = str(Path(home) / ".hermes" / "kanban.db")

    assert_isolated(home)
    return home


def assert_isolated(home: str) -> None:
    """Fail loudly unless the resolved kanban DB lives inside ``home``.

    This is the regression guard Till asked for: it makes the
    HERMES_KANBAN_DB-inheritance breach impossible to reintroduce silently —
    a stress run pointed at a shared/production board aborts before it can
    open a connection.
    """
    home_res = Path(home).resolve()
    # Import here: the caller must have set the env first.
    from hermes_cli import kanban_db as kb

    resolved = Path(kb.kanban_db_path()).resolve()
    # The temp home comes from mkdtemp() — a freshly created, unique directory.
    # If the resolved DB lives inside it, it cannot be a shared/production
    # board; if it lives anywhere else, an inherited path pin leaked in.
    try:
        resolved.relative_to(home_res)
    except ValueError:
        raise RuntimeError(
            "stress isolation breach: kanban_db_path() resolved to "
            f"{resolved}, outside the temp HOME {home_res}. Refusing to run a "
            "stress test against a shared/production board. Did an inherited "
            "HERMES_KANBAN_DB leak in? Call isolate_home() before importing "
            "hermes_cli.kanban_db."
        )
