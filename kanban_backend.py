"""B1.1 — Backend-Switch fürs Kanban-Board.

Liest ``kanban.backend`` (``sqlite`` | ``postgres``). **Default ``sqlite`` →
komplett inert**: solange nicht umgestellt wird, ändert sich nichts am Bestand
(SQLite bleibt der Rollback-Pfad). Erst beim Cutover setzt Till ``postgres``.

Stufe 1 (Keystone): zentrale Entscheidung + PG-Backend-Konstruktor. Die
call-site-Migration (Dispatcher, Bridge, Watchers, Tools, Dashboard) hängt die
jeweiligen Aufrufe schrittweise an ``make_pg_backend()`` — jede Etappe eigener
Commit (siehe P5-CHECKLIST.md). Bis eine Stelle migriert ist, läuft sie
unverändert über ``hermes_cli.kanban_db`` (SQLite).
"""
from __future__ import annotations

import os

SQLITE = "sqlite"
POSTGRES = "postgres"
_VALID = {SQLITE, POSTGRES}
DEFAULT_BACKEND = SQLITE


def resolve_backend_name(config: dict | None = None) -> str:
    """Backend-Name aus config (``kanban.backend``) bzw. Env, sonst Default sqlite.

    Reihenfolge: Env ``HERMES_KANBAN_BACKEND`` > config['kanban']['backend'] > 'sqlite'.
    Unbekannter Wert → ValueError (fail-fast statt stilles Falsch-Routing).
    """
    val = os.environ.get("HERMES_KANBAN_BACKEND", "").strip().lower()
    if not val and config:
        val = str((config.get("kanban") or {}).get("backend", "")).strip().lower()
    if not val:
        val = DEFAULT_BACKEND
    if val not in _VALID:
        raise ValueError(
            f"kanban.backend={val!r} ungültig — erlaubt: {sorted(_VALID)}"
        )
    return val


def is_postgres(config: dict | None = None) -> bool:
    return resolve_backend_name(config) == POSTGRES


def make_pg_backend(conn):
    """PG-Backend über eine psycopg3-Verbindung (autocommit=False). Lazy-Import,
    damit psycopg nur geladen wird, wenn PG wirklich aktiv ist."""
    from kanban_pg.backend import PgBackend
    return PgBackend(conn)
