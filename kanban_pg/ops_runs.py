"""Run-Lebenszyklus-Domäne (task_runs)."""
from ._base import now_epoch


class RunsMixin:
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
