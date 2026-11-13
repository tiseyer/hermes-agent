"""B1.1 Postgres-Board — P6 Migrationsskript: SQLite kanban.db -> PostgreSQL.

Wiederverwendbar für Dry-Run (gegen Kopie) UND späteren Cutover (gegen Prod-PG):
nur --sqlite und --pg-uri wechseln.

Vorgehen (bit-genaue 1:1-Übernahme):
  * SQLite read-only + immutable, jede Tabelle per `SELECT * ... NOT INDEXED`
    (Full-Table-Scan — umgeht korrupte Indizes der Kopie 9c67e857).
  * Bulk-Load via COPY (psycopg3) in der Spaltenreihenfolge der Quelle.
  * Trigger werden NICHT angelegt/aktiviert -> Werte (status, status_changed_at,
    completed_at ...) bleiben exakt wie in SQLite (sonst würde sync_family_root
    beim Insert von Kindern die Eltern umrechnen).
  * IDENTITY-Sequenzen nach dem Load auf max(id)+1.
  * Verifikation: Zeilen je Tabelle + tasks-Status-Verteilung vorher/nachher gleich.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys

from .adapter import PgKanban

# Alle Nutztabellen (ohne sqlite_sequence; Reihenfolge egal, keine FK-Constraints).
TABLES = [
    "tasks",
    "task_comments",
    "task_events",
    "task_runs",
    "task_links",
    "kanban_notify_subs",
    "task_attachments",
    "manager_action_guard",
    "manager_action_comments",
    "manager_alert_outbox",
]
IDENTITY_TABLES = {
    "task_comments", "task_events", "task_runs",
    "task_attachments", "manager_alert_outbox",
}


def open_sqlite_ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


def read_table(sconn: sqlite3.Connection, table: str):
    cur = sconn.cursor()
    cur.execute(f"SELECT * FROM {table} NOT INDEXED")
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    cur.close()
    return cols, rows


# SQLite erlaubt eingebettete NUL (0x00) in TEXT, PostgreSQL nicht. In der Kopie
# betrifft das genau 2 task_comments.body-Werte (Artefakt). Wir strippen NUL beim
# Laden — Zeilenzahl bleibt identisch, nur das ungültige Byte entfällt — und zählen
# jede Bereinigung, damit die Abweichung im Bericht sichtbar ist.
def sanitize_rows(rows):
    nul = 0
    out = []
    for row in rows:
        if any(isinstance(c, str) and "\x00" in c for c in row):
            new = []
            for c in row:
                if isinstance(c, str) and "\x00" in c:
                    nul += 1
                    c = c.replace("\x00", "")
                new.append(c)
            out.append(tuple(new))
        else:
            out.append(row)
    return out, nul


def sqlite_counts(sconn: sqlite3.Connection) -> dict[str, int]:
    out = {}
    for t in TABLES:
        out[t] = sconn.execute(f"SELECT count(*) FROM {t} NOT INDEXED").fetchone()[0]
    return out


def sqlite_status_dist(sconn: sqlite3.Connection) -> dict[str, int]:
    return dict(sconn.execute(
        "SELECT status, count(*) FROM tasks NOT INDEXED GROUP BY status"
    ).fetchall())


def migrate(sqlite_path: str, pg: PgKanban) -> dict[str, int]:
    sconn = open_sqlite_ro(sqlite_path)
    loaded: dict[str, int] = {}
    nul_total = 0
    try:
        for t in TABLES:
            cols, rows = read_table(sconn, t)
            rows, nul = sanitize_rows(rows)
            nul_total += nul
            if rows:
                n = pg.copy_in(t, cols, rows)
            else:
                n = 0
            pg.conn.commit()
            loaded[t] = n
            extra = f"  (NUL bereinigt: {nul})" if nul else ""
            print(f"  geladen {t}: {n}{extra}", flush=True)
        if nul_total:
            print(f"  >> {nul_total} TEXT-Wert(e) gesamt um eingebettete NUL-Bytes bereinigt", flush=True)
        for t in IDENTITY_TABLES:
            pg.reset_identity(t)
    finally:
        sconn.close()
    return loaded


def verify(sqlite_path: str, pg: PgKanban) -> tuple[bool, dict]:
    sconn = open_sqlite_ro(sqlite_path)
    try:
        s_counts = sqlite_counts(sconn)
        s_status = sqlite_status_dist(sconn)
    finally:
        sconn.close()
    p_counts = {t: pg.scalar(f"SELECT count(*) FROM {t}") for t in TABLES}
    p_status = dict(pg.query(
        "SELECT status, count(*) FROM tasks GROUP BY status"
    ))
    counts_ok = s_counts == p_counts
    status_ok = s_status == p_status
    return (counts_ok and status_ok), {
        "sqlite_counts": s_counts, "pg_counts": p_counts, "counts_ok": counts_ok,
        "sqlite_status": s_status, "pg_status": p_status, "status_ok": status_ok,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sqlite", required=True)
    ap.add_argument("--pg-uri", required=True)
    ap.add_argument("--schema", required=True, help="pg_schema.sql")
    a = ap.parse_args(argv)

    with open(a.schema, encoding="utf-8") as f:
        schema_sql = f.read()

    pg = PgKanban(a.pg_uri).connect()
    try:
        pg.executescript(schema_sql)
        print("Schema angelegt.", flush=True)
        loaded = migrate(a.sqlite, pg)
        ok, report = verify(a.sqlite, pg)
        print("\n=== VERIFIKATION ===", flush=True)
        for t in TABLES:
            s, p = report["sqlite_counts"][t], report["pg_counts"][t]
            flag = "OK" if s == p else "!! DIFF"
            print(f"  {t:26} sqlite={s:>7}  pg={p:>7}  {flag}", flush=True)
        print(f"  tasks-Status sqlite={report['sqlite_status']}", flush=True)
        print(f"  tasks-Status pg    ={report['pg_status']}", flush=True)
        print(f"\nZeilen gleich: {report['counts_ok']} | Status gleich: {report['status_ok']}", flush=True)
        print(f"DRY-RUN {'GRÜN' if ok else 'ROT'}", flush=True)
        return 0 if ok else 2
    finally:
        pg.close()


if __name__ == "__main__":
    sys.exit(main())
