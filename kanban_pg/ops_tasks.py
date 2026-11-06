"""Task-Domäne: create/status/complete/reassign/block."""
from ._base import now_epoch


class TasksMixin:
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
        # consecutive_failures nur bei Erfolg zurücksetzen (Circuit-Breaker-Semantik).
        self.conn.execute(
            "UPDATE tasks SET status='done', completed_at=%s, consecutive_failures=0 WHERE id=%s",
            (now_epoch(), task_id),
        )
        self.conn.commit()

    def reassign_task(self, task_id, profile):
        self.conn.execute("UPDATE tasks SET assignee=%s WHERE id=%s", (profile, task_id))
        self.conn.commit()

    def block_task(self, task_id, kind=None):
        """Karte auf 'blocked' (optional block_kind). Worker-/Tools-Lifecycle (Etappe 5)."""
        self.conn.execute(
            "UPDATE tasks SET status='blocked', block_kind=%s WHERE id=%s",
            (kind, task_id),
        )
        self.conn.commit()
