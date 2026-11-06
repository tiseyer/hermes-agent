"""B1.1 — P4 Health-Äquivalent für PostgreSQL (ersetzt SQLite `PRAGMA integrity_check`).

PG korrumpiert nicht durch Mehr-Prozess-Schreiber (das war der SQLite-Defekt), daher
ist „Health" hier anders geschnitten:
  1. Konnektivität (SELECT 1).
  2. amcheck (`bt_index_check`) über die B-Baum-Indizes der Board-Tabellen — wenn die
     contrib-Extension da ist (prod: `postgresql-contrib`). Fängt echte Index-Schäden.
  3. Fallback, falls amcheck fehlt (z. B. im gebündelten pgserver): alle Tabellen
     scannbar + alle Indizes `indisvalid`.
Gedacht als Canary-Ersatz (statt Datei-integrity_check) + für die Backup-/E1-Events.
"""
from __future__ import annotations

KANBAN_TABLES = [
    "tasks", "task_comments", "task_events", "task_runs", "task_links",
    "kanban_notify_subs", "task_attachments", "manager_action_guard",
    "manager_action_comments", "manager_alert_outbox",
]


def amcheck_available(conn) -> bool:
    try:
        conn.execute("CREATE EXTENSION IF NOT EXISTS amcheck")
        row = conn.execute(
            "SELECT 1 FROM pg_extension WHERE extname='amcheck'"
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _btree_indexes(conn) -> list[str]:
    rows = conn.execute(
        """
        SELECT i.indexrelid::regclass::text
        FROM pg_index i
        JOIN pg_class c ON c.oid = i.indrelid
        JOIN pg_am am ON am.oid = (SELECT relam FROM pg_class WHERE oid = i.indexrelid)
        WHERE c.relname = ANY(%s) AND am.amname = 'btree' AND i.indisvalid
        """,
        (KANBAN_TABLES,),
    ).fetchall()
    return [r[0] for r in rows]


def run_amcheck(conn) -> dict:
    failures = []
    checked = 0
    for idx in _btree_indexes(conn):
        try:
            conn.execute(f"SELECT bt_index_check('{idx}'::regclass)")
            checked += 1
        except Exception as e:  # pragma: no cover - nur bei echtem Schaden
            failures.append(f"{idx}: {e}")
    return {"mode": "amcheck", "indexes_checked": checked, "failures": failures,
            "ok": not failures}


def fallback_consistency(conn) -> dict:
    problems = []
    for t in KANBAN_TABLES:
        try:
            conn.execute(f"SELECT count(*) FROM {t}")
        except Exception as e:
            problems.append(f"table {t} nicht scannbar: {e}")
    invalid = conn.execute(
        "SELECT indexrelid::regclass::text FROM pg_index i "
        "JOIN pg_class c ON c.oid=i.indrelid "
        "WHERE c.relname = ANY(%s) AND NOT i.indisvalid",
        (KANBAN_TABLES,),
    ).fetchall()
    if invalid:
        problems.append(f"invalide Indizes: {[r[0] for r in invalid]}")
    return {"mode": "fallback", "problems": problems, "ok": not problems}


def check_health(conn) -> dict:
    """Gesamt-Health. conn: psycopg-Verbindung (autocommit empfohlen). -> dict mit 'ok'."""
    out: dict = {"checks": {}}
    try:
        conn.execute("SELECT 1")
        out["checks"]["connectivity"] = True
    except Exception as e:
        out["checks"]["connectivity"] = False
        out["error"] = str(e)
        out["ok"] = False
        return out

    if amcheck_available(conn):
        res = run_amcheck(conn)
    else:
        res = fallback_consistency(conn)
    out["checks"]["integrity"] = res
    out["ok"] = bool(out["checks"]["connectivity"] and res.get("ok"))
    return out
