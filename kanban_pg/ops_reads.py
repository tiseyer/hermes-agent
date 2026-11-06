"""Lese-Domäne (Etappe 6 / Dashboard-REST + Bridge-Polling).

Dashboard/Bridge sind lesend-dominiert; unter MVCC blockieren Leser die Schreiber
nicht und können nichts korrumpieren (genau das Gegenteil des SQLite-A-Pfad-Problems).
Schreibende Dashboard-Aktionen (Kommentar/Status) laufen über die bestehenden
Schreib-Ops (add_comment/set_status).
"""


def _dicts(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


class ReadsMixin:
    def get_task(self, task_id):
        cur = self.conn.execute(
            "SELECT id,title,status,assignee,priority,created_at,status_changed_at,"
            "block_kind,family_root_id,child_role FROM tasks WHERE id=%s",
            (task_id,),
        )
        rows = _dicts(cur)
        return rows[0] if rows else None

    def list_tasks(self, status=None, assignee=None, limit=200):
        clauses, params = [], []
        if status is not None:
            clauses.append("status=%s"); params.append(status)
        if assignee is not None:
            clauses.append("assignee=%s"); params.append(assignee)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        cur = self.conn.execute(
            "SELECT id,title,status,assignee,priority,created_at FROM tasks"
            f"{where} ORDER BY priority DESC, created_at ASC LIMIT %s",
            params,
        )
        return _dicts(cur)

    def list_comments(self, task_id):
        cur = self.conn.execute(
            "SELECT id,author,body,created_at FROM task_comments "
            "WHERE task_id=%s ORDER BY created_at, id",
            (task_id,),
        )
        return _dicts(cur)

    def list_events(self, task_id, after_id=0, limit=100):
        cur = self.conn.execute(
            "SELECT id,kind,payload,created_at FROM task_events "
            "WHERE task_id=%s AND id > %s ORDER BY id LIMIT %s",
            (task_id, after_id, limit),
        )
        return _dicts(cur)

    def stats_by_status(self):
        cur = self.conn.execute("SELECT status, count(*) FROM tasks GROUP BY status")
        return dict(cur.fetchall())
