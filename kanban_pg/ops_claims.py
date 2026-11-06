"""Claim-/Dispatcher-Domäne: claim/heartbeat/reclaim/record_failure/advisory-lock."""
from ._base import now_epoch
from .lock_model import claim_one_ready, try_board_lock


class ClaimsMixin:
    def claim_ready(self, assignee, claim_lock, worker_pid=None, ttl_seconds=600):
        tid = claim_one_ready(self.conn, assignee, claim_lock, now_epoch(),
                              worker_pid, ttl_seconds)
        self.conn.commit()
        return tid

    def heartbeat(self, task_id, claim_lock, ttl_seconds=600):
        """Verlängert den Claim; False wenn die Karte uns nicht (mehr) gehört."""
        cur = self.conn.execute(
            "UPDATE tasks SET last_heartbeat_at=%s, claim_expires=%s "
            "WHERE id=%s AND status='running' AND claim_lock=%s RETURNING id",
            (now_epoch(), now_epoch() + ttl_seconds, task_id, claim_lock),
        )
        ok = cur.fetchone() is not None
        self.conn.commit()
        return ok

    def bump_heartbeat(self, task_id):
        """Auto-Heartbeat (Worker-Tools, #31752): nur last_heartbeat_at der laufenden
        Karte bumpen (Liveness), ohne claim_lock-Prüfung. Ersetzt den ungelockten
        B-Pfad-Schreiber aus tools/kanban_tools.py."""
        self.conn.execute(
            "UPDATE tasks SET last_heartbeat_at=%s WHERE id=%s AND status='running'",
            (now_epoch(), task_id),
        )
        self.conn.commit()

    def reclaim_stale(self, now=None):
        """Abgelaufene running-Claims zurück auf 'ready' (Dispatcher-Watchdog)."""
        now = now if now is not None else now_epoch()
        cur = self.conn.execute(
            "UPDATE tasks SET status='ready', claim_lock=NULL, worker_pid=NULL, "
            "claim_expires=NULL "
            "WHERE status='running' AND claim_expires IS NOT NULL AND claim_expires < %s "
            "RETURNING id",
            (now,),
        )
        ids = [r[0] for r in cur.fetchall()]
        self.conn.commit()
        return ids

    def record_failure(self, task_id, error, limit=3):
        """Circuit-Breaker: Zähler hoch, bei >= limit auf 'blocked'. -> (count, status)."""
        cur = self.conn.execute(
            "UPDATE tasks SET "
            "  consecutive_failures = consecutive_failures + 1, "
            "  last_failure_error = %s, "
            "  status = CASE WHEN consecutive_failures + 1 >= %s THEN 'blocked' ELSE status END "
            "WHERE id = %s RETURNING consecutive_failures, status",
            (error, limit, task_id),
        )
        row = cur.fetchone()
        self.conn.commit()
        return row

    def try_dispatch_lock(self, board="default"):
        """board-scoped Single-Writer-Sektion (ersetzt <db>.dispatch.lock)."""
        return try_board_lock(self.conn, board)
