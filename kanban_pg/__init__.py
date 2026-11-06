"""B1.1 PostgreSQL-Backend fürs Kanban-Board (eigenständig, nur psycopg + stdlib).

Bewusst KEIN Import von hermes_cli — damit die PG-Schicht in einer schlanken
Umgebung (CI/Dry-Run via pgserver) testbar ist, ohne die schweren Fabrik-Deps.
Die Verdrahtung in die Fabrik passiert über ``kanban_backend.py`` (Switch).
"""
from .backend import PgBackend, now_epoch
from .lock_model import board_lock_key, try_board_lock, claim_one_ready
from . import health

__all__ = [
    "PgBackend", "now_epoch",
    "board_lock_key", "try_board_lock", "claim_one_ready",
    "health",
]
