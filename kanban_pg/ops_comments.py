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

    def add_event(self, task_id, kind, payload=None, run_id=None):
        cur = self.conn.execute(
            "INSERT INTO task_events (task_id,run_id,kind,payload,created_at) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (task_id, run_id, kind, payload, now_epoch()),
        )
        eid = cur.fetchone()[0]
        self.conn.commit()
        return eid
