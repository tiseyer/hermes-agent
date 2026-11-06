"""B1.1 Postgres-Board — P5 Referenz-Backend (psycopg3), EIN Schreibpfad.

Zweck: beweist das Portier-Muster für die rohen sqlite3-Schreiber end-to-end
gegen PG (Schema + Trigger + Lock-Modell zusammen). Deckt die Haupt-Operation-
Klassen ab, an denen sich der vollständige P5-Port orientiert. SQLite bleibt
unangetastet (Rollback bis PG stabil).

NICHT der Voll-Port: kanban_db.py hat ~30 öffentliche Schreib-Funktionen / 72
write_txn-Stellen + Bridge(5) + watchers(42) + tools + dashboard — siehe
P5-CHECKLIST.md. Jede dieser Stellen mappt auf genau dieses Muster
(parametrisiertes SQL über die Verbindung, DB-seitige Serialisierung statt flock).
"""
from __future__ import annotations

import time

from .lock_model import claim_one_ready, try_board_lock  # noqa: F401 (re-export)


def now_epoch() -> int:
    return int(time.time())


class PgBackend:
    """Schmaler Schreib-/Lese-Backend über eine psycopg3-Verbindung (autocommit=False)."""

    def __init__(self, conn):
        self.conn = conn

    # ---- tasks ----
    def create_task(self, task_id, title, *, status="todo", assignee=None,
                    created_at=None, family_root_id=None, child_role="work",
                    priority=0, status_changed_at=None):
        self.conn.execute(
            "INSERT INTO tasks (id,title,status,assignee,priority,created_at,"
            "family_root_id,child_role,status_changed_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (task_id, title, status, assignee, priority,
             created_at if created_at is not None else now_epoch(),
             family_root_id, child_role, status_changed_at),
        )
        self.conn.commit()
        return task_id

    def set_status(self, task_id, status):
        self.conn.execute("UPDATE tasks SET status=%s WHERE id=%s", (status, task_id))
        self.conn.commit()

    def complete_task(self, task_id):
        # consecutive_failures wird nur bei Erfolg zurückgesetzt (Circuit-Breaker-Semantik).
        self.conn.execute(
            "UPDATE tasks SET status='done', completed_at=%s, consecutive_failures=0 WHERE id=%s",
            (now_epoch(), task_id),
        )
        self.conn.commit()

    def record_failure(self, task_id, error, limit=3):
        """Dispatcher-Circuit-Breaker: Zähler hoch, bei >= limit auf 'blocked'.
        Gibt (consecutive_failures, status) zurück."""
        cur = self.conn.execute(
            "UPDATE tasks SET "
            "  consecutive_failures = consecutive_failures + 1, "
            "  last_failure_error = %s, "
            "  status = CASE WHEN consecutive_failures + 1 >= %s THEN 'blocked' ELSE status END "
            "WHERE id = %s "
            "RETURNING consecutive_failures, status",
            (error, limit, task_id),
        )
        row = cur.fetchone()
        self.conn.commit()
        return row

    def reassign_task(self, task_id, profile):
        self.conn.execute("UPDATE tasks SET assignee=%s WHERE id=%s", (profile, task_id))
        self.conn.commit()

    # ---- claim (DB-seitig serialisiert, ersetzt flock-Claim) ----
    def claim_ready(self, assignee, claim_lock, worker_pid=None):
        tid = claim_one_ready(self.conn, assignee, claim_lock, now_epoch(), worker_pid)
        self.conn.commit()
        return tid

    # ---- comments / events ----
    def add_comment(self, task_id, author, body):
        cur = self.conn.execute(
            "INSERT INTO task_comments (task_id,author,body,created_at) "
            "VALUES (%s,%s,%s,%s) RETURNING id",
            (task_id, author, body, now_epoch()),
        )
        cid = cur.fetchone()[0]
        self.conn.commit()
        return cid

    def add_event(self, task_id, kind, payload=None, run_id=None):
        cur = self.conn.execute(
            "INSERT INTO task_events (task_id,run_id,kind,payload,created_at) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (task_id, run_id, kind, payload, now_epoch()),
        )
        eid = cur.fetchone()[0]
        self.conn.commit()
        return eid

    # ---- links ----
    def link_tasks(self, parent_id, child_id):
        self.conn.execute(
            "INSERT INTO task_links (parent_id,child_id) VALUES (%s,%s) "
            "ON CONFLICT DO NOTHING",
            (parent_id, child_id),
        )
        self.conn.commit()

    # ---- runs ----
    def start_run(self, task_id, profile, status="running", started_at=None):
        cur = self.conn.execute(
            "INSERT INTO task_runs (task_id,profile,status,started_at) "
            "VALUES (%s,%s,%s,%s) RETURNING id",
            (task_id, profile, status, started_at if started_at is not None else now_epoch()),
        )
        rid = cur.fetchone()[0]
        self.conn.commit()
        return rid

    def end_run(self, run_id, status, outcome, ended_at=None):
        self.conn.execute(
            "UPDATE task_runs SET status=%s, outcome=%s, ended_at=%s WHERE id=%s",
            (status, outcome, ended_at if ended_at is not None else now_epoch(), run_id),
        )
        self.conn.commit()

    # ---- board single-writer-Sektion (ersetzt <db>.dispatch.lock) ----
    def try_dispatch_lock(self, board="default"):
        return try_board_lock(self.conn, board)
