"""Kommentar-/Event-Domäne."""
from ._base import now_epoch


class CommentsEventsMixin:
    def add_comment(self, task_id, author, body):
        cur = self.conn.execute(
            "INSERT INTO task_comments (task_id,author,body,created_at) "
            "VALUES (%s,%s,%s,%s) RETURNING id",
            (task_id, author, body, now_epoch()),
        )
        cid = cur.fetchone()[0]
        self.conn.commit()
        return cid

    def add_comment_full(self, task_id, author, body):
        """Faithful-Spiegel von kanban_db.add_comment: Existenz-Check +
        Insert (gestrippt) + ``commented``-Event in EINER Transaktion.
        Input-Validierung (leerer body/author) macht der kanban_db-Guard
        davor (backend-agnostisch)."""
        with self._txn():
            if not self.conn.execute(
                "SELECT 1 FROM tasks WHERE id=%s", (task_id,)
            ).fetchone():
                raise ValueError(f"unknown task {task_id}")
            cur = self.conn.execute(
                "INSERT INTO task_comments (task_id,author,body,created_at) "
                "VALUES (%s,%s,%s,%s) RETURNING id",
                (task_id, author.strip(), body.strip(), now_epoch()),
            )
            cid = cur.fetchone()[0]
            self._append_event(
                task_id, "commented", {"author": author, "len": len(body)}
            )
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
