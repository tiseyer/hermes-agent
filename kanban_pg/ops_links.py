"""Abhängigkeits-/Link-Domäne (task_links) — Welle 2 faithful.

Die thin link/unlink-Kurzformen sind auf die faithful kanban_db-Semantik gehoben
(gleiche Signatur): self-link-Guard, missing-parent-Check, Zyklus-Check,
family_root-Propagation parent→child (+family_order), ready-Kind-Demote wenn
Parent≠done, linked/unlinked-Events, recompute_ready-Kopplung. Dazu
specify_triage_task (triage→todo) und decompose_triage_task (triage→Kind-Graph).

GRENZE (D5): die Repo→Profil-Routing-Schicht (resolve_repo_profiles /
_repository_profile_from_name / profile_exists) in decompose/specify ist Config-
Routing = injizierte Grenze — der PG-Kern portiert nur den DB-Schreibpfad; die
Kind-assignees werden wie übergeben verwendet (kein tenant-rewrite). Der LLM, der
die children-Liste erzeugt, war schon immer außerhalb dieser Funktion.
"""
from ._base import now_epoch
from .ops_tasks import _new_task_id


class LinksMixin:
    # ----- interne Helfer -------------------------------------------------
    def _find_missing_parents(self, ids):
        ids = list(ids)
        if not ids:
            return []
        rows = self.conn.execute(
            "SELECT id FROM tasks WHERE id IN (" + ",".join(["%s"] * len(ids)) + ")",
            ids,
        ).fetchall()
        present = {r[0] for r in rows}
        return [i for i in ids if i not in present]

    def _would_cycle(self, parent_id, child_id):
        """True wenn parent→child einen Zyklus erzeugt (Abstieg ab child)."""
        seen = set()
        stack = [child_id]
        while stack:
            node = stack.pop()
            if node == parent_id:
                return True
            if node in seen:
                continue
            seen.add(node)
            rows = self.conn.execute(
                "SELECT child_id FROM task_links WHERE parent_id=%s", (node,),
            ).fetchall()
            stack.extend(r[0] for r in rows)
        return False

    # ----- link / unlink (faithful) --------------------------------------
    def link_tasks(self, parent_id, child_id):
        if parent_id == child_id:
            raise ValueError("a task cannot depend on itself")
        with self._txn():
            missing = self._find_missing_parents([parent_id, child_id])
            if missing:
                raise ValueError(f"unknown task(s): {', '.join(missing)}")
            if self._would_cycle(parent_id, child_id):
                raise ValueError(f"linking {parent_id} -> {child_id} would create a cycle")
            self.conn.execute(
                "INSERT INTO task_links (parent_id,child_id) VALUES (%s,%s) "
                "ON CONFLICT DO NOTHING", (parent_id, child_id),
            )
            parent_family = self.conn.execute(
                "SELECT family_root_id FROM tasks WHERE id=%s", (parent_id,),
            ).fetchone()[0]
            child_family = self.conn.execute(
                "SELECT family_root_id FROM tasks WHERE id=%s", (child_id,),
            ).fetchone()[0]
            if parent_family and child_family and parent_family != child_family:
                raise ValueError("cannot attach one child to multiple card families")
            if parent_family and not child_family:
                family_order = self.conn.execute(
                    "SELECT COALESCE(MAX(family_order),0)+1 FROM tasks "
                    "WHERE family_root_id=%s", (parent_family,),
                ).fetchone()[0]
                self.conn.execute(
                    "UPDATE tasks SET family_root_id=%s, family_order=%s WHERE id=%s",
                    (parent_family, family_order, child_id),
                )
            parent_status = self.conn.execute(
                "SELECT status FROM tasks WHERE id=%s", (parent_id,),
            ).fetchone()[0]
            if parent_status != "done":
                self.conn.execute(
                    "UPDATE tasks SET status='todo' WHERE id=%s AND status='ready'",
                    (child_id,),
                )
            self._append_event(child_id, "linked",
                               {"parent": parent_id, "child": child_id})

    def unlink_tasks(self, parent_id, child_id):
        with self._txn():
            cur = self.conn.execute(
                "DELETE FROM task_links WHERE parent_id=%s AND child_id=%s",
                (parent_id, child_id),
            )
            removed = cur.rowcount > 0
            if removed:
                self._append_event(child_id, "unlinked",
                                   {"parent": parent_id, "child": child_id})
        if removed:
            self.recompute_ready()
        return removed

    # ----- specify_triage_task (triage→todo) -----------------------------
    def specify_triage_task(self, task_id, *, title=None, body=None,
                            assignee=None, author=None):
        if title is not None and not title.strip():
            raise ValueError("title cannot be blank")
        with self._txn():
            existing = self.conn.execute(
                "SELECT title, body, assignee FROM tasks WHERE id=%s AND status='triage'",
                (task_id,),
            ).fetchone()
            if existing is None:
                return False
            sets, params, changed = ["status='todo'"], [], []
            if title is not None and title.strip() != (existing[0] or ""):
                sets.append("title=%s"); params.append(title.strip()); changed.append("title")
            if body is not None and (body or "") != (existing[1] or ""):
                sets.append("body=%s"); params.append(body); changed.append("body")
            if assignee is not None and assignee != (existing[2] or None):
                sets.append("assignee=%s"); params.append(assignee); changed.append("assignee")
            params.append(task_id)
            cur = self.conn.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE id=%s AND status='triage'",
                params,
            )
            if cur.rowcount != 1:
                return False
            if changed and author and author.strip():
                self.conn.execute(
                    "INSERT INTO task_comments (task_id,author,body,created_at) "
                    "VALUES (%s,%s,%s,%s)",
                    (task_id, author.strip(),
                     "Specified — updated " + ", ".join(changed)
                     + " and promoted to todo.", now_epoch()),
                )
            self._append_event(task_id, "specified",
                               {"changed_fields": changed} if changed else None)
        # Eigene txn: parent-done → ready.
        self.recompute_ready()
        with self._txn():
            has_parents = self.conn.execute(
                "SELECT 1 FROM task_links WHERE child_id=%s LIMIT 1", (task_id,),
            ).fetchone()
            if has_parents is None:
                cur = self.conn.execute(
                    "UPDATE tasks SET status='ready' WHERE id=%s AND status='todo'",
                    (task_id,),
                )
                if cur.rowcount == 1:
                    self._append_event(task_id, "promoted", None)
        return True

    # ----- decompose_triage_task (triage→Kind-Graph) ----------------------
    def decompose_triage_task(self, task_id, *, root_assignee, children,
                              author=None, auto_promote=True):
        """Faithful DB-Schreibkern. GRENZE (D5): Repo→Profil-Routing der Kind-
        assignees (resolve_repo_profiles) NICHT portiert — assignees wie übergeben.
        Der auslösende Routing-Preflight-Park (RepositoryProfileError) ist ebenso
        Grenze. Gibt child_ids (Eingabereihenfolge) oder None zurück."""
        if not children:
            return None
        for idx, child in enumerate(children):
            if not isinstance(child, dict):
                raise ValueError(f"child[{idx}] is not a dict")
            title = child.get("title")
            if not isinstance(title, str) or not title.strip():
                raise ValueError(f"child[{idx}].title is required")
            parents_idx = child.get("parents") or []
            if not isinstance(parents_idx, list):
                raise ValueError(f"child[{idx}].parents must be a list")
            for p in parents_idx:
                if not isinstance(p, int) or p < 0 or p >= len(children):
                    raise ValueError(
                        f"child[{idx}].parents[{p}] is not a valid index into children")
                if p == idx:
                    raise ValueError(f"child[{idx}] cannot list itself as a parent")
        # Topo-Zyklus-Check über den Sibling-Graph (Kahn).
        in_deg = [0] * len(children)
        adj = [[] for _ in range(len(children))]
        for i, c in enumerate(children):
            for p in (c.get("parents") or []):
                adj[p].append(i)
                in_deg[i] += 1
        queue = [i for i in range(len(children)) if in_deg[i] == 0]
        seen = 0
        while queue:
            node = queue.pop()
            seen += 1
            for nb in adj[node]:
                in_deg[nb] -= 1
                if in_deg[nb] == 0:
                    queue.append(nb)
        if seen != len(children):
            raise ValueError("cyclic dependency detected in decomposed children list")

        now = now_epoch()
        child_ids = []
        with self._txn():
            root_row = self.conn.execute(
                "SELECT id, title, body, status, tenant, repository, workspace_kind, "
                "workspace_path, initiative_id FROM tasks WHERE id=%s", (task_id,),
            ).fetchone()
            if root_row is None:
                return None
            if root_row[3] != "triage":
                return None
            self.conn.execute(
                "UPDATE tasks SET family_root_id=%s, family_order=0 WHERE id=%s",
                (task_id, task_id),
            )
            tenant = root_row[4]
            root_repository = root_row[5]
            root_ws_kind = root_row[6] or "scratch"
            root_ws_path = root_row[7]
            child_initiative = root_row[8] or root_row[0]
            root_goal_title = (root_row[1] or "").strip()
            goal_pointer = (
                "## Übergeordnetes Ziel\n"
                f"Teil von: {root_goal_title[:120]} (Ziel siehe Wurzelkarte)"
            ) if root_goal_title else ""

            for idx, child in enumerate(children):
                new_id = _new_task_id()
                title = child["title"].strip()
                body = child.get("body")
                assignee = child.get("assignee")  # D5-Grenze: kein tenant-rewrite
                child_ws_kind = child.get("workspace_kind") or root_ws_kind
                if child.get("workspace_path"):
                    child_ws_path = child.get("workspace_path")
                elif child_ws_kind == root_ws_kind:
                    child_ws_path = root_ws_path
                else:
                    child_ws_path = None
                existing_body = body if isinstance(body, str) and body.strip() else ""
                if not goal_pointer or existing_body.startswith("## Übergeordnetes Ziel"):
                    stored_body = body if isinstance(body, str) else None
                elif existing_body:
                    stored_body = goal_pointer + "\n\n" + existing_body
                else:
                    stored_body = goal_pointer
                self.conn.execute(
                    "INSERT INTO tasks (id,title,body,assignee,status,workspace_kind,"
                    "workspace_path,tenant,repository,created_at,created_by,parent_id,"
                    "initiative_id,family_root_id,family_order,child_role) "
                    "VALUES (%s,%s,%s,%s,'todo',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'work')",
                    (new_id, title, stored_body, assignee, child_ws_kind, child_ws_path,
                     tenant, root_repository, now, (author or "decomposer"), task_id,
                     child_initiative, task_id, idx + 1),
                )
                self._append_event(new_id, "created",
                                   {"by": author or "decomposer",
                                    "from_decompose_of": task_id})
                child_ids.append(new_id)

            for idx, child in enumerate(children):
                for p_idx in child.get("parents") or []:
                    parent_id = child_ids[p_idx]
                    cid = child_ids[idx]
                    self.conn.execute(
                        "INSERT INTO task_links (parent_id,child_id) VALUES (%s,%s) "
                        "ON CONFLICT DO NOTHING", (parent_id, cid),
                    )
                    self._append_event(cid, "linked", {"parent": parent_id, "child": cid})

            for cid in child_ids:
                self.conn.execute(
                    "INSERT INTO task_links (parent_id,child_id) VALUES (%s,%s) "
                    "ON CONFLICT DO NOTHING", (cid, task_id),
                )

            if root_assignee is not None:
                self.conn.execute(
                    "UPDATE tasks SET assignee=%s WHERE id=%s", (root_assignee, task_id),
                )

            if author and author.strip():
                self.conn.execute(
                    "INSERT INTO task_comments (task_id,author,body,created_at) "
                    "VALUES (%s,%s,%s,%s)",
                    (task_id, author.strip(),
                     "Decomposed into " + ", ".join(child_ids)
                     + ". Root will wake when all children complete.", now),
                )
            self._append_event(task_id, "decomposed",
                               {"child_ids": child_ids, "root_assignee": root_assignee})

        if auto_promote:
            parent_free = [child_ids[i] for i, c in enumerate(children)
                           if not (c.get("parents") or [])]
            if parent_free:
                with self._txn():
                    for cid in parent_free:
                        cur = self.conn.execute(
                            "UPDATE tasks SET status='ready' WHERE id=%s AND status='todo'",
                            (cid,),
                        )
                        if cur.rowcount == 1:
                            self._append_event(cid, "promoted", None)
        return child_ids
