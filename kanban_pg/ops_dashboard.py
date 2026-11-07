"""Dashboard-Domäne (Welle 4c): die vom Dashboard-REST (:9119) bisher INLINE
roh (sqlite `?`-SQL) ausgeführten Reads/Aggregate/Writer als PG-Methoden.

Faithful zu plugins/kanban/dashboard/plugin_api.py: Batch-Aggregate (Board-Zähler,
Link-/Comment-/Progress-Rollups, Diagnostics-Bulk) und die zusammengesetzten
Status-/Edit-Writer (set_status_direct/Priority/Title-Body) mit denselben
Invarianten (Parent-done-Gate, Run-Close, Child-Demote, Events, recompute).
Reads liefern dicts/Tupel in der Form, die der Dashboard-Code erwartet.
"""
from ._base import now_epoch


def _dicts(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


class DashboardMixin:
    # ----- Batch-Reads für _compute_task_diagnostics ----------------------
    def tasks_by_ids(self, ids):
        ids = list(ids)
        if not ids:
            return []
        ph = ",".join(["%s"] * len(ids))
        return _dicts(self.conn.execute(
            f"SELECT * FROM tasks WHERE id IN ({ph})", ids))

    def tasks_unarchived(self):
        return _dicts(self.conn.execute(
            "SELECT * FROM tasks WHERE status != 'archived'"))

    def events_by_task_ids(self, ids):
        ids = list(ids)
        if not ids:
            return []
        ph = ",".join(["%s"] * len(ids))
        return _dicts(self.conn.execute(
            f"SELECT * FROM task_events WHERE task_id IN ({ph}) ORDER BY id", ids))

    def runs_by_task_ids(self, ids):
        ids = list(ids)
        if not ids:
            return []
        ph = ",".join(["%s"] * len(ids))
        return _dicts(self.conn.execute(
            f"SELECT * FROM task_runs WHERE task_id IN ({ph}) ORDER BY id", ids))

    # ----- Links -----------------------------------------------------------
    def child_ids(self, parent_id):
        cur = self.conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id = %s ORDER BY child_id",
            (parent_id,))
        return [r[0] for r in cur.fetchall()]

    def all_links(self):
        cur = self.conn.execute("SELECT parent_id, child_id FROM task_links")
        return [(r[0], r[1]) for r in cur.fetchall()]

    def comment_counts(self):
        cur = self.conn.execute(
            "SELECT task_id, COUNT(*) FROM task_comments GROUP BY task_id")
        return {r[0]: int(r[1]) for r in cur.fetchall()}

    def child_status_by_parent(self):
        cur = self.conn.execute(
            "SELECT l.parent_id, t.status FROM task_links l "
            "JOIN tasks t ON t.id = l.child_id")
        return [(r[0], r[1]) for r in cur.fetchall()]

    def max_event_id(self):
        return int(self.conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0])

    def distinct_tenants(self):
        cur = self.conn.execute(
            "SELECT DISTINCT tenant FROM tasks WHERE tenant IS NOT NULL ORDER BY tenant")
        return [r[0] for r in cur.fetchall()]

    def distinct_assignees_unarchived(self):
        cur = self.conn.execute(
            "SELECT DISTINCT assignee FROM tasks WHERE assignee IS NOT NULL "
            "AND status != 'archived' ORDER BY assignee")
        return [r[0] for r in cur.fetchall()]

    # ----- Detail-/Flow-Reads ---------------------------------------------
    def deps_for_members(self, member_ids):
        member_ids = list(member_ids)
        if not member_ids:
            return []
        ph = ",".join(["%s"] * len(member_ids))
        return _dicts(self.conn.execute(
            f"SELECT l.child_id AS cid, t.id AS pid, t.title AS ptitle, "
            f"t.status AS pstatus FROM task_links l "
            f"JOIN tasks t ON t.id = l.parent_id WHERE l.child_id IN ({ph})",
            member_ids))

    def parents_not_done(self, task_id):
        return _dicts(self.conn.execute(
            "SELECT t.id, t.title, t.status FROM tasks t "
            "JOIN task_links l ON l.parent_id = t.id "
            "WHERE l.child_id = %s AND t.status != 'done'", (task_id,)))

    def active_workers(self):
        return _dicts(self.conn.execute(
            "SELECT r.id AS run_id, r.task_id, t.title AS task_title, "
            "t.status AS task_status, t.assignee AS task_assignee, r.profile, "
            "r.worker_pid, r.started_at, r.claim_lock, r.claim_expires, "
            "r.last_heartbeat_at, r.max_runtime_seconds "
            "FROM task_runs r JOIN tasks t ON t.id = r.task_id "
            "WHERE r.ended_at IS NULL AND r.worker_pid IS NOT NULL "
            "AND t.status = 'running' ORDER BY r.started_at ASC"))

    def events_after(self, cursor_val, limit=200):
        return _dicts(self.conn.execute(
            "SELECT id, task_id, run_id, kind, payload, created_at "
            "FROM task_events WHERE id > %s ORDER BY id ASC LIMIT %s",
            (cursor_val, int(limit))))

    # ----- Zusammengesetzte Writer (faithful zu plugin_api) ----------------
    def set_status_direct(self, task_id, new_status):
        """Faithful-Port von plugin_api._set_status_direct: Direkt-Statuswechsel
        (Drag-Drop) mit Parent-done-Gate (→ready), Run-Close beim Verlassen von
        running, status-Event, Child-Demote beim Wieder-Öffnen eines erfüllten
        Parents; recompute_ready nach {done,ready}."""
        with self._txn():
            prev = self.conn.execute(
                "SELECT status, current_run_id FROM tasks WHERE id = %s",
                (task_id,)).fetchone()
            if prev is None:
                return False
            prev_status, prev_run = prev[0], prev[1]
            if new_status == "ready":
                pstats = self.conn.execute(
                    "SELECT t.status FROM tasks t JOIN task_links l ON l.parent_id = t.id "
                    "WHERE l.child_id = %s", (task_id,)).fetchall()
                if pstats and not all(p[0] == "done" for p in pstats):
                    return False
            was_running = prev_status == "running"
            reopening = (prev_status in ("done", "archived")
                         and new_status not in ("done", "archived"))
            running = new_status == "running"
            cur = self.conn.execute(
                "UPDATE tasks SET status=%s, "
                "claim_lock = CASE WHEN %s THEN claim_lock ELSE NULL END, "
                "claim_expires = CASE WHEN %s THEN claim_expires ELSE NULL END, "
                "worker_pid = CASE WHEN %s THEN worker_pid ELSE NULL END "
                "WHERE id=%s",
                (new_status, running, running, running, task_id))
            if cur.rowcount != 1:
                return False
            run_id = None
            if was_running and new_status != "running" and prev_run:
                run_id = self._end_run(
                    task_id, outcome="reclaimed", status="reclaimed",
                    summary=f"status changed to {new_status} (dashboard/direct)")
            self._append_event(task_id, "status", {"status": new_status}, run_id=run_id)
            if reopening:
                for (child_id,) in self.conn.execute(
                    "SELECT child_id FROM task_links WHERE parent_id=%s ORDER BY child_id",
                    (task_id,)).fetchall():
                    demoted = self.conn.execute(
                        "UPDATE tasks SET status='todo' WHERE id=%s AND status='ready'",
                        (child_id,))
                    if demoted.rowcount == 1:
                        self._append_event(
                            child_id, "status",
                            {"status": "todo", "reason": "parent_reopened",
                             "parent": task_id})
        if new_status in ("done", "ready"):
            self.recompute_ready()
        return True

    def update_priority(self, task_id, priority):
        with self._txn():
            self.conn.execute("UPDATE tasks SET priority=%s WHERE id=%s",
                              (int(priority), task_id))
            self._append_event(task_id, "reprioritized", {"priority": int(priority)})

    def update_title_body(self, task_id, title=None, body=None):
        sets, vals = [], []
        if title is not None:
            sets.append("title=%s"); vals.append(title.strip())
        if body is not None:
            sets.append("body=%s"); vals.append(body)
        if not sets:
            return
        vals.append(task_id)
        with self._txn():
            self.conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=%s", vals)
            self._append_event(task_id, "edited", None)
