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

    def active_work_child_count(self, task_id):
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE id != %s "
            "AND child_role = 'work' AND status != 'archived' "
            "AND (parent_id = %s OR family_root_id = %s)",
            (task_id, task_id, task_id),
        )
        return int(cur.fetchone()[0])

    # ----- Welle-4: Dashboard-Rich-Reads (faithful Spiegel) ----------------
    def latest_summary(self, task_id):
        cur = self.conn.execute(
            "SELECT summary FROM task_runs "
            "WHERE task_id=%s AND summary IS NOT NULL AND summary != '' "
            "ORDER BY COALESCE(ended_at, started_at) DESC, id DESC LIMIT 1",
            (task_id,),
        )
        row = cur.fetchone()
        return row[0] if row else None

    def latest_summaries(self, task_ids):
        ids = list(task_ids)
        if not ids:
            return {}
        ph = ",".join(["%s"] * len(ids))
        cur = self.conn.execute(
            f"SELECT task_id, summary FROM ("
            f"  SELECT task_id, summary,"
            f"    ROW_NUMBER() OVER (PARTITION BY task_id"
            f"      ORDER BY COALESCE(ended_at, started_at) DESC, id DESC) AS rn"
            f"  FROM task_runs"
            f"  WHERE task_id IN ({ph}) AND summary IS NOT NULL AND summary != ''"
            f") sub WHERE rn = 1",
            ids,
        )
        return {r[0]: r[1] for r in cur.fetchall()}

    def list_runs_full(self, task_id, *, include_active=True,
                       state_type=None, state_name=None):
        q = "SELECT * FROM task_runs WHERE task_id = %s"
        params = [task_id]
        if not include_active:
            q += " AND ended_at IS NOT NULL"
        if state_type is not None:
            q += f" AND {state_type} = %s"  # state_type vom Guard validiert
            params.append(state_name)
        q += " ORDER BY started_at ASC, id ASC"
        return _dicts(self.conn.execute(q, params))

    def list_attachments_full(self, task_id):
        return _dicts(self.conn.execute(
            "SELECT * FROM task_attachments WHERE task_id=%s "
            "ORDER BY created_at ASC, id ASC", (task_id,)))

    def get_attachment_full(self, attachment_id):
        rows = _dicts(self.conn.execute(
            "SELECT * FROM task_attachments WHERE id=%s", (int(attachment_id),)))
        return rows[0] if rows else None

    def initiative_members_full(self, initiative_id):
        return _dicts(self.conn.execute(
            "SELECT * FROM tasks WHERE initiative_id=%s AND id != %s "
            "ORDER BY created_at ASC, id ASC", (initiative_id, initiative_id)))

    def initiative_rollup_rows(self, tenant=None):
        where = "WHERE initiative_id IS NOT NULL AND status != 'archived'"
        params = []
        if tenant:
            where += " AND initiative_id IN (SELECT id FROM tasks WHERE tenant = %s)"
            params.append(tenant)
        return _dicts(self.conn.execute(
            f"SELECT id, title, assignee, status, initiative_id FROM tasks {where} "
            "ORDER BY created_at ASC, id ASC", params))

    def go_gated_task_ids(self):
        cur = self.conn.execute(
            "SELECT DISTINCT e.task_id FROM task_events e "
            "JOIN tasks t ON t.id = e.task_id "
            "WHERE e.kind = 'go_gate_held' AND t.assignee = 'till' "
            "AND t.status NOT IN ('done', 'archived')")
        return {r[0] for r in cur.fetchall()}

    def parent_ids(self, child_id):
        cur = self.conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = %s ORDER BY parent_id",
            (child_id,))
        return [r[0] for r in cur.fetchall()]

    def role_history(self, profile, exclude_task_id, limit=5):
        return _dicts(self.conn.execute(
            "SELECT t.id, t.title, r.summary, r.ended_at "
            "FROM task_runs r JOIN tasks t ON r.task_id = t.id "
            "WHERE r.profile = %s AND r.task_id != %s AND r.outcome = 'completed' "
            "ORDER BY r.ended_at DESC LIMIT %s",
            (profile, exclude_task_id, int(limit))))

    # ------------------------------------------------------------------
    # Full-row reads (Welle 3): liefern SELECT-*-dicts, damit die
    # kanban_db-Read-Guards sie 1:1 in Task/Comment/Event wrappen können
    # (Rückgabe-Typ-Parität zum SQLite-Pfad — Consumer bleiben unverändert).
    # Die schlanken Projektionen oben bleiben für Bridge/Direktnutzung.
    # ------------------------------------------------------------------
    def get_task_full(self, task_id):
        cur = self.conn.execute("SELECT * FROM tasks WHERE id=%s", (task_id,))
        rows = _dicts(cur)
        return rows[0] if rows else None

    def list_tasks_full(
        self,
        *,
        assignee=None,
        status=None,
        tenant=None,
        session_id=None,
        workflow_template_id=None,
        current_step_key=None,
        include_archived=False,
        limit=None,
        order_fragment="priority DESC, created_at ASC",
    ):
        """Faithful-Spiegel von kanban_db.list_tasks (Filter + Order).

        ``order_fragment`` ist bereits vom Guard gegen VALID_SORT_ORDERS
        validiert (kein User-Input) und wird unverändert nach ORDER BY
        gesetzt; die Spaltennamen gelten in beiden Backends.
        """
        query = "SELECT * FROM tasks WHERE 1=1"
        params = []
        if assignee is not None:
            query += " AND assignee = %s"; params.append(assignee)
        if status is not None:
            query += " AND status = %s"; params.append(status)
        if tenant is not None:
            query += " AND tenant = %s"; params.append(tenant)
        if session_id is not None:
            query += " AND session_id = %s"; params.append(session_id)
        if workflow_template_id is not None:
            query += " AND workflow_template_id = %s"; params.append(workflow_template_id)
        if current_step_key is not None:
            query += " AND current_step_key = %s"; params.append(current_step_key)
        if not include_archived and status != "archived":
            query += " AND status != 'archived'"
        query += f" ORDER BY {order_fragment}"
        if limit:
            query += f" LIMIT {int(limit)}"
        cur = self.conn.execute(query, params)
        return _dicts(cur)

    def list_comments_full(self, task_id):
        cur = self.conn.execute(
            "SELECT * FROM task_comments WHERE task_id=%s ORDER BY created_at ASC",
            (task_id,),
        )
        return _dicts(cur)

    def list_events_full(self, task_id):
        cur = self.conn.execute(
            "SELECT * FROM task_events WHERE task_id=%s ORDER BY created_at ASC, id ASC",
            (task_id,),
        )
        return _dicts(cur)
