"""Abhängigkeits-/Link-Domäne (task_links)."""


class LinksMixin:
    def link_tasks(self, parent_id, child_id):
        self.conn.execute(
            "INSERT INTO task_links (parent_id,child_id) VALUES (%s,%s) "
            "ON CONFLICT DO NOTHING",
            (parent_id, child_id),
        )
        self.conn.commit()

    def unlink_tasks(self, parent_id, child_id):
        cur = self.conn.execute(
            "DELETE FROM task_links WHERE parent_id=%s AND child_id=%s RETURNING parent_id",
            (parent_id, child_id),
        )
        ok = cur.fetchone() is not None
        self.conn.commit()
        return ok
