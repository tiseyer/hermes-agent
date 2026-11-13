"""Task-Domäne: create/status/complete/reassign/block.

Welle 2 (faithful): die thin-Kurzformen (create_task/set_status/complete_task/
reassign_task/block_task) bleiben als strukturelle Seed-/Primitive-Helfer (D1,
Etappe-1–6-Regression). DANEBEN die faithful DB-Zustandsautomat-Ports der realen
kanban_db-Funktionen — create_task_full/complete_task_full/block_task_full/
reassign_task_full + move_to_human/relabel_task/assign_task/unblock_task/
promote_task/archive_task/schedule_task/edit_completed_task_result/set_*.

GRENZEN (D5, siehe HANDOFF-PORT.md — konsistent mit D4): der *DB-Zustandsautomat*
(Status-Transitionen, Events, Run-Lebenszyklus, Zähler, recompute-Kaskade,
block_kind-Routing, Family-Propagation) ist faithful portiert. Die Policy-/FS-/
Netz-/Routing-Overlays sind injizierte Grenzen und NICHT hier:
 - create_task: projects_db-Link + Worktree-Pfad/Branch-Derivation, repository_
   profiles-Validierung, skills-Validierung, board default_workdir — laufen in
   kanban_db VOR dem Routing-Guard (Guard sitzt vor der Insert-Schleife).
 - complete_task: done-Evidence-Verifikation (Remote-Commit/Preview = Netz),
   Artefakt-Persistenz (FS), Halluzinations-Scan, workspace-cleanup, Lifecycle-Hook.
 - block_task: Review-Repo-Routing (resolve_task_repo_profiles/profile_exists =
   Config) via injizierbarem reviewer_resolver; die F43/F56-dependency-owner-fold-
   Zweige (brauchen _human_decision_gate_question/_dependency_recut_count) sind
   Grenze. Der Review-Handoff, die Recurrence→triage/merger/owner-loop-Pfade und
   der normale blocked-Pfad sind faithful portiert.
Return-Typ: PG-Reads liefern dicts (nicht Task) — move_to_human/create_task_full
geben denselben str/task_id zurück wie SQLite.
"""
import json
import secrets

from ._base import now_epoch
from .ops_dispatch import (
    _HUMAN_DECISION_CATEGORIES,
    _ZIELFRAGE_TECH_REGEX,
    _ZIELFRAGE_BLOCK_KINDS,
)

VALID_BLOCK_KINDS = {"dependency", "needs_input", "capability", "transient", "review"}
BLOCK_RECURRENCE_LIMIT = 2
VALID_CATEGORIES = {"frage", "entscheidung", "abnahme", "blocker", "go_noetig"}
VALID_WORKSPACE_KINDS = {"scratch", "worktree", "dir"}
VALID_INITIAL_STATUSES = {"running", "blocked"}
HUMAN_CARD_TITLE_MAX = 80
HUMAN_CARD_KURZBESCHREIBUNG_MAX = 160


def _new_task_id():
    return "t_" + secrets.token_hex(4)


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

    # ===== Faithful DB-Zustandsautomat-Ports (Welle 2) ====================

    # ----- gemeinsame Helfer ----------------------------------------------
    def _synthesize_ended_run(self, task_id, *, outcome, summary=None,
                              error=None, metadata=None):
        """Null-Dauer-Run (bereits geschlossen) für Terminal-Transition ohne
        laufenden Run. Faithful zu kanban_db._synthesize_ended_run. Kein Commit."""
        now = now_epoch()
        trow = self.conn.execute(
            "SELECT assignee, current_step_key FROM tasks WHERE id=%s", (task_id,),
        ).fetchone()
        profile = trow[0] if trow else None
        step_key = trow[1] if trow else None
        cur = self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, step_key, status, outcome, "
            "summary, error, metadata, started_at, ended_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (task_id, profile, step_key, outcome, outcome, summary, error,
             json.dumps(metadata, ensure_ascii=False) if metadata else None, now, now),
        )
        return int(cur.fetchone()[0])

    def _card_has_nontech_zielfrage(self, task_id):
        comments = self.conn.execute(
            "SELECT body FROM task_comments WHERE task_id=%s ORDER BY created_at ASC",
            (task_id,),
        ).fetchall()
        for (body,) in reversed(comments):
            b = body or ""
            if "?" in b and not _ZIELFRAGE_TECH_REGEX.search(b):
                return True
        return False

    @staticmethod
    def _owner_decision_zielfrage(title, body):
        core = (body or "").strip() or (title or "").strip() or "Zielentscheidung"
        return (
            "ZIELENTSCHEID (Geschäftsentscheidung des Inhabers, keine Technikfrage):\n\n"
            f"{core}\n\n"
            "Welcher der genannten Wege soll gelten? Die Fabrik trifft diese "
            "Abnahme-/Umfangsentscheidung nicht selbst und baut nach der Antwort "
            "gezielt weiter; sie verlangt dafür KEINE vom Inhaber zu erstellende "
            "Vollständigkeitsliste — genügt es, einzelne Punkte zu benennen, baut "
            "die Fabrik gezielt nach."
        )

    def _is_protected_owner_decision(self, task_id):
        row = self.conn.execute(
            "SELECT status, category FROM tasks WHERE id=%s", (task_id,),
        ).fetchone()
        if row is None or row[0] != "blocked":
            return False
        if row[1] in _HUMAN_DECISION_CATEGORIES:
            return True
        return self._blocked_card_has_zielfrage(task_id)

    # ----- create_task (DB-Kern; Policy-Resolve liegt vor dem Guard) ------
    def create_task_full(self, *, title, body=None, assignee=None, created_by=None,
                         workspace_kind="scratch", workspace_path=None,
                         branch_name=None, tenant=None, repository=None, priority=0,
                         parents=(), triage=False, idempotency_key=None,
                         max_runtime_seconds=None, skills_list=None, max_retries=None,
                         goal_mode=False, goal_max_turns=None, merge_group=None,
                         initial_status="running", session_id=None, project_id=None,
                         parent_task_id=None, initiative_id=None, child_role="work"):
        parents = tuple(p for p in parents if p)
        if idempotency_key:
            row = self.conn.execute(
                "SELECT id FROM tasks WHERE idempotency_key=%s AND status!='archived' "
                "ORDER BY created_at DESC LIMIT 1", (idempotency_key,),
            ).fetchone()
            if row:
                self.conn.commit()
                return row[0]
        now = now_epoch()
        import psycopg
        for attempt in range(2):
            task_id = _new_task_id()
            try:
                with self._txn():
                    if initial_status == "blocked":
                        task_status = "blocked"
                    elif triage:
                        task_status = "triage"
                    else:
                        task_status = "ready"
                        if parents:
                            rows = self.conn.execute(
                                "SELECT status FROM tasks WHERE id IN ("
                                + ",".join(["%s"] * len(parents)) + ")", parents,
                            ).fetchall()
                            if len(rows) != len(set(parents)):
                                raise ValueError("unknown parent task(s)")
                            if any(r[0] != "done" for r in rows):
                                task_status = "todo"
                    # Parent-Existenz auch für blocked/triage validieren.
                    if parents and task_status in ("blocked", "triage"):
                        rows = self.conn.execute(
                            "SELECT id FROM tasks WHERE id IN ("
                            + ",".join(["%s"] * len(parents)) + ")", parents,
                        ).fetchall()
                        if len({r[0] for r in rows}) != len(set(parents)):
                            raise ValueError("unknown parent task(s)")
                    family_roots = set()
                    if parents:
                        for (fr,) in self.conn.execute(
                            "SELECT family_root_id FROM tasks WHERE id IN ("
                            + ",".join(["%s"] * len(parents))
                            + ") AND family_root_id IS NOT NULL", parents,
                        ).fetchall():
                            family_roots.add(fr)
                    if len(family_roots) > 1:
                        raise ValueError("cannot attach one child to multiple card families")
                    family_root_id = next(iter(family_roots), None)
                    family_order = None
                    if family_root_id is not None:
                        family_order = self.conn.execute(
                            "SELECT COALESCE(MAX(family_order),0)+1 FROM tasks "
                            "WHERE family_root_id=%s", (family_root_id,),
                        ).fetchone()[0]
                    # Hierarchie (orthogonal zu parents) — portabel.
                    hier_parent = (str(parent_task_id).strip() or None) if parent_task_id else None
                    hier_initiative = (str(initiative_id).strip() or None) if initiative_id else None
                    _tenant = tenant
                    if hier_parent:
                        prow = self.conn.execute(
                            "SELECT id, initiative_id, tenant FROM tasks WHERE id=%s",
                            (hier_parent,),
                        ).fetchone()
                        if prow is None:
                            raise ValueError(f"unknown hierarchy parent task: {hier_parent}")
                        if hier_initiative is None:
                            hier_initiative = prow[1] or prow[0]
                        if _tenant is None:
                            _tenant = prow[2]
                    if hier_initiative:
                        irow = self.conn.execute(
                            "SELECT id, tenant FROM tasks WHERE id=%s", (hier_initiative,),
                        ).fetchone()
                        if irow is None:
                            raise ValueError(f"unknown initiative task: {hier_initiative}")
                        if hier_parent is None:
                            hier_parent = hier_initiative
                        if _tenant is None:
                            _tenant = irow[1]
                    self.conn.execute(
                        "INSERT INTO tasks (id,title,body,assignee,status,priority,"
                        "created_by,created_at,workspace_kind,workspace_path,branch_name,"
                        "project_id,tenant,repository,merge_group,idempotency_key,"
                        "max_runtime_seconds,skills,max_retries,goal_mode,goal_max_turns,"
                        "session_id,parent_id,initiative_id,family_root_id,family_order,"
                        "child_role) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                        "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (task_id, title.strip(), body, assignee, task_status, priority,
                         created_by, now, workspace_kind, workspace_path, branch_name,
                         project_id, _tenant, repository, merge_group, idempotency_key,
                         int(max_runtime_seconds) if max_runtime_seconds is not None else None,
                         json.dumps(skills_list) if skills_list is not None else None,
                         int(max_retries) if max_retries is not None else None,
                         1 if goal_mode else 0,
                         int(goal_max_turns) if goal_max_turns is not None else None,
                         session_id, hier_parent, hier_initiative, family_root_id,
                         family_order, child_role),
                    )
                    for pid in parents:
                        self.conn.execute(
                            "INSERT INTO task_links (parent_id,child_id) VALUES (%s,%s) "
                            "ON CONFLICT DO NOTHING", (pid, task_id),
                        )
                    self._append_event(
                        task_id, "created",
                        {"assignee": assignee, "status": task_status,
                         "parents": list(parents), "tenant": _tenant,
                         "branch_name": branch_name,
                         "skills": list(skills_list) if skills_list else None,
                         "goal_mode": bool(goal_mode) or None,
                         "parent_task_id": hier_parent, "initiative_id": hier_initiative,
                         "child_role": child_role if family_root_id else None},
                    )
                    if task_status == "blocked":
                        self._append_event(
                            task_id, "blocked",
                            {"reason": "created blocked (explicit handoff)",
                             "kind": "needs_input"},
                        )
                return task_id
            except psycopg.errors.UniqueViolation:
                if attempt == 1:
                    raise
                continue
        raise RuntimeError("unreachable")

    # ----- assign / reassign ----------------------------------------------
    def assign_task(self, task_id, profile):
        with self._txn():
            row = self.conn.execute(
                "SELECT status, claim_lock, assignee FROM tasks WHERE id=%s", (task_id,),
            ).fetchone()
            if not row:
                return False
            status, claim_lock, cur_assignee = row
            if claim_lock is not None and status == "running":
                raise RuntimeError(
                    f"cannot reassign {task_id}: currently running (claimed)."
                )
            if cur_assignee != profile:
                self.conn.execute(
                    "UPDATE tasks SET assignee=%s, consecutive_failures=0, "
                    "last_failure_error=NULL WHERE id=%s", (profile, task_id),
                )
            else:
                self.conn.execute(
                    "UPDATE tasks SET assignee=%s WHERE id=%s", (profile, task_id),
                )
            self._append_event(task_id, "assigned", {"assignee": profile})
            return True

    def reassign_task_full(self, task_id, profile, *, reclaim_first=False, reason=None):
        if reclaim_first:
            self.reclaim_task(task_id, reason=reason or "reassign")
        try:
            return self.assign_task(task_id, profile)
        except RuntimeError:
            return False

    # ----- complete (DB-Kern; Verify/Artefakte/Cleanup = Grenze) ----------
    def complete_task_full(self, task_id, *, result=None, summary=None,
                           metadata=None, expected_run_id=None):
        now = now_epoch()
        with self._txn():
            _pre = self.conn.execute(
                "SELECT assignee, block_kind FROM tasks WHERE id=%s", (task_id,),
            ).fetchone()
            _pre_assignee = (_pre[0] if _pre else None) or ""
            _pre_block_kind = (_pre[1] if _pre else None) or ""
            if expected_run_id is None:
                cur = self.conn.execute(
                    "UPDATE tasks SET status='done', result=%s, completed_at=%s, "
                    "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, "
                    "block_kind=NULL, block_recurrences=0 "
                    "WHERE id=%s AND status IN ('running','ready','blocked')",
                    (result, now, task_id),
                )
            else:
                cur = self.conn.execute(
                    "UPDATE tasks SET status='done', result=%s, completed_at=%s, "
                    "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, "
                    "block_kind=NULL, block_recurrences=0 "
                    "WHERE id=%s AND status IN ('running','ready','blocked') "
                    "AND current_run_id=%s",
                    (result, now, task_id, int(expected_run_id)),
                )
            if cur.rowcount != 1:
                return False
            run_id = self._end_run(
                task_id, outcome="completed", status="done",
                summary=summary if summary is not None else result, metadata=metadata,
            )
            if run_id is None and (summary or metadata or result):
                run_id = self._synthesize_ended_run(
                    task_id, outcome="completed",
                    summary=summary if summary is not None else result, metadata=metadata,
                )
            # F20 review-verdict-mirror (reine DB): Reviewer-Verdict als Kommentar spiegeln.
            _verdict = ((summary if summary is not None else result) or "").strip()
            if _pre_block_kind == "review" and "review" in _pre_assignee.lower() and _verdict:
                has_rev = any(
                    "review" in (c.get("author") or "").lower()
                    and "coder" not in (c.get("author") or "").lower()
                    for c in self.list_comments(task_id)
                )
                if not has_rev:
                    self.conn.execute(
                        "INSERT INTO task_comments (task_id,author,body,created_at) "
                        "VALUES (%s,%s,%s,%s)", (task_id, _pre_assignee, _verdict, now),
                    )
                    self._append_event(task_id, "commented",
                                       {"author": _pre_assignee, "len": len(_verdict),
                                        "source": "review-verdict-mirror"})
            ev_summary = (summary if summary is not None else result) or ""
            ev_summary = ev_summary.strip().splitlines()[0][:400] if ev_summary else ""
            self._append_event(task_id, "completed",
                               {"result_len": len(result) if result else 0,
                                "summary": ev_summary or None}, run_id=run_id)
        # consecutive_failures-Reset + Promotion der Abhängigen (eigene txn).
        self._clear_failure_counter(task_id)
        self.recompute_ready()
        return True

    def edit_completed_task_result(self, task_id, *, result, summary=None, metadata=None):
        handoff = summary if summary is not None else result
        with self._txn():
            row = self.conn.execute(
                "SELECT status FROM tasks WHERE id=%s", (task_id,),
            ).fetchone()
            if not row or row[0] != "done":
                return False
            self.conn.execute("UPDATE tasks SET result=%s WHERE id=%s", (result, task_id))
            run = self.conn.execute(
                "SELECT id FROM task_runs WHERE task_id=%s AND outcome='completed' "
                "ORDER BY COALESCE(ended_at,started_at,0) DESC, id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            run_id = int(run[0]) if run else None
            if run_id is None:
                run_id = self._synthesize_ended_run(
                    task_id, outcome="completed", summary=handoff, metadata=metadata)
            else:
                self.conn.execute("UPDATE task_runs SET summary=%s WHERE id=%s",
                                  (handoff, run_id))
                if metadata is not None:
                    self.conn.execute("UPDATE task_runs SET metadata=%s WHERE id=%s",
                                      (json.dumps(metadata, ensure_ascii=False), run_id))
            ev_summary = handoff.strip().splitlines()[0][:400] if handoff else ""
            self._append_event(task_id, "edited",
                               {"fields": ["result", "summary"]
                                + (["metadata"] if metadata is not None else []),
                                "result_len": len(result) if result else 0,
                                "summary": ev_summary or None}, run_id=run_id)
        return True

    # ----- unblock / promote / archive / schedule -------------------------
    def unblock_task(self, task_id):
        now = now_epoch()
        with self._txn():
            stale = self.conn.execute(
                "SELECT current_run_id FROM tasks WHERE id=%s "
                "AND status IN ('blocked','scheduled')", (task_id,),
            ).fetchone()
            if stale and stale[0]:
                self.conn.execute(
                    "UPDATE task_runs SET status='reclaimed', outcome='reclaimed', "
                    "summary=COALESCE(summary,'invariant recovery on unblock'), "
                    "ended_at=%s, claim_lock=NULL, claim_expires=NULL, worker_pid=NULL "
                    "WHERE id=%s AND ended_at IS NULL", (now, int(stale[0])),
                )
            undone = self.conn.execute(
                "SELECT 1 FROM task_links l JOIN tasks p ON p.id=l.parent_id "
                "WHERE l.child_id=%s AND p.status!='done' LIMIT 1", (task_id,),
            ).fetchone()
            new_status = "todo" if undone else "ready"
            cur = self.conn.execute(
                "UPDATE tasks SET status=%s, current_run_id=NULL, "
                "consecutive_failures=0, last_failure_error=NULL "
                "WHERE id=%s AND status IN ('blocked','scheduled')", (new_status, task_id),
            )
            if cur.rowcount != 1:
                return False
            self._append_event(task_id, "unblocked",
                               {"status": new_status} if new_status != "ready" else None)
            return True

    def promote_task(self, task_id, *, actor, reason=None, force=False, dry_run=False):
        row = self.conn.execute(
            "SELECT status FROM tasks WHERE id=%s", (task_id,),
        ).fetchone()
        if row is None:
            self.conn.commit()
            return False, f"task {task_id} not found"
        if row[0] not in ("backlog", "todo", "blocked"):
            self.conn.commit()
            return False, (f"task {task_id} is {row[0]!r}; promote only applies to "
                           "'backlog', 'todo', or 'blocked'")
        if not force:
            parents = self.conn.execute(
                "SELECT t.id, t.status FROM tasks t JOIN task_links l ON l.parent_id=t.id "
                "WHERE l.child_id=%s", (task_id,),
            ).fetchall()
            unsat = [p[0] for p in parents if p[1] not in ("done", "archived")]
            if unsat:
                self.conn.commit()
                return False, (f"unsatisfied parent dependencies: {', '.join(unsat)} "
                               "(use --force to override)")
        if dry_run:
            self.conn.commit()
            return True, None
        with self._txn():
            upd = self.conn.execute(
                "UPDATE tasks SET status='ready' "
                "WHERE id=%s AND status IN ('backlog','todo','blocked')", (task_id,),
            )
            if upd.rowcount != 1:
                return False, f"task {task_id} status changed during promotion"
            self._append_event(task_id, "promoted_manual",
                               {"actor": actor, "reason": reason, "forced": force})
        return True, None

    def archive_task(self, task_id):
        if self._is_protected_owner_decision(task_id):
            with self._txn():
                self._append_event(task_id, "archive_refused",
                                   {"reason": "blocked owner-Zielfrage may not be "
                                              "archived (F50)"})
            return False
        with self._txn():
            cur = self.conn.execute(
                "UPDATE tasks SET status='archived', claim_lock=NULL, "
                "claim_expires=NULL, worker_pid=NULL "
                "WHERE id=%s AND status!='archived'", (task_id,),
            )
            if cur.rowcount != 1:
                return False
            run_id = self._end_run(task_id, outcome="reclaimed", status="reclaimed",
                                   summary="task archived with run still active")
            self._append_event(task_id, "archived", None, run_id=run_id)
        self.recompute_ready()
        return True

    def schedule_task(self, task_id, *, reason=None, expected_run_id=None):
        with self._txn():
            if self._is_owner_decision_card(task_id):
                return False
            params = [task_id]
            sql = ("UPDATE tasks SET status='scheduled', claim_lock=NULL, "
                   "claim_expires=NULL, worker_pid=NULL "
                   "WHERE id=%s AND status IN ('todo','ready','running','blocked')")
            if expected_run_id is not None:
                sql += " AND current_run_id=%s"
                params.append(int(expected_run_id))
            cur = self.conn.execute(sql, params)
            if cur.rowcount != 1:
                return False
            run_id = self._end_run(task_id, outcome="scheduled", status="scheduled",
                                   summary=reason)
            if run_id is None and reason:
                run_id = self._synthesize_ended_run(task_id, outcome="scheduled",
                                                    summary=reason)
            self._append_event(task_id, "scheduled", {"reason": reason}, run_id=run_id)
            return True

    # ----- setters ---------------------------------------------------------
    def set_workspace_path(self, task_id, path):
        self.conn.execute("UPDATE tasks SET workspace_path=%s WHERE id=%s",
                          (str(path), task_id))
        self.conn.commit()

    def set_workspace_kind(self, task_id, kind):
        if kind not in VALID_WORKSPACE_KINDS:
            raise ValueError(f"workspace_kind must be one of {sorted(VALID_WORKSPACE_KINDS)}")
        self.conn.execute("UPDATE tasks SET workspace_kind=%s WHERE id=%s", (kind, task_id))
        self.conn.commit()

    def set_branch_name(self, task_id, branch_name):
        self.conn.execute("UPDATE tasks SET branch_name=%s WHERE id=%s",
                          (str(branch_name), task_id))
        self.conn.commit()

    # ----- relabel (ändert NUR title/short_description) -------------------
    def relabel_task(self, task_id, *, title=None, kurzbeschreibung=None, actor=None):
        if title is None and kurzbeschreibung is None:
            raise ValueError("relabel needs at least one of title / kurzbeschreibung")
        if title is not None:
            if not title.strip():
                raise ValueError("title is required")
            if len(title) > HUMAN_CARD_TITLE_MAX:
                raise ValueError(f"title too long: {len(title)} > {HUMAN_CARD_TITLE_MAX}")
        if kurzbeschreibung is not None and len(kurzbeschreibung) > HUMAN_CARD_KURZBESCHREIBUNG_MAX:
            raise ValueError(f"kurzbeschreibung too long: {len(kurzbeschreibung)} > "
                             f"{HUMAN_CARD_KURZBESCHREIBUNG_MAX}")
        with self._txn():
            row = self.conn.execute(
                "SELECT title, short_description FROM tasks WHERE id=%s", (task_id,),
            ).fetchone()
            if row is None:
                return False
            sets, params, payload = [], [], {"actor": actor}
            if title is not None:
                sets.append("title=%s"); params.append(title)
                payload["title"] = {"old": row[0], "new": title}
            if kurzbeschreibung is not None:
                sets.append("short_description=%s"); params.append(kurzbeschreibung)
                payload["kurzbeschreibung"] = {"old": row[1], "new": kurzbeschreibung}
            params.append(task_id)
            self.conn.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE id=%s", params)
            self._append_event(task_id, "relabeled", payload)
        return True

    # ----- move_to_human (F38/F45 faithful; reine DB-Transitions) ---------
    def move_to_human(self, task_id, *, title=None, kurzbeschreibung=None,
                      category=None, actor=None, planned_step=False):
        if title is not None:
            if not title.strip():
                raise ValueError("title is required")
            if len(title) > HUMAN_CARD_TITLE_MAX:
                raise ValueError(f"title too long: {len(title)} > {HUMAN_CARD_TITLE_MAX}")
        if kurzbeschreibung is not None and len(kurzbeschreibung) > HUMAN_CARD_KURZBESCHREIBUNG_MAX:
            raise ValueError("kurzbeschreibung too long")
        if category is not None and category not in VALID_CATEGORIES:
            raise ValueError(f"category must be one of {sorted(VALID_CATEGORIES)} or None")
        now = now_epoch()
        with self._txn():
            row = self.conn.execute(
                "SELECT status, current_run_id FROM tasks WHERE id=%s", (task_id,),
            ).fetchone()
            if row is None:
                return "notfound"
            status, current_run_id = row
            if status == "human":
                if category is not None:
                    cur_cat = self.conn.execute(
                        "SELECT category FROM tasks WHERE id=%s", (task_id,),
                    ).fetchone()[0]
                    if cur_cat != category:
                        self.conn.execute("UPDATE tasks SET category=%s WHERE id=%s",
                                          (category, task_id))
                        self._append_event(task_id, "categorized",
                                           {"from": cur_cat, "category": category,
                                            "actor": actor})
                        return "categorized"
                return "noop"
            if status in ("done", "archived"):
                return "terminal"
            if status == "blocked" and self._blocked_card_has_zielfrage(task_id):
                if category is not None:
                    cur_cat = self.conn.execute(
                        "SELECT category FROM tasks WHERE id=%s", (task_id,),
                    ).fetchone()[0]
                    if cur_cat != category:
                        self.conn.execute("UPDATE tasks SET category=%s WHERE id=%s",
                                          (category, task_id))
                        self._append_event(task_id, "categorized",
                                           {"from": cur_cat, "category": category,
                                            "actor": actor, "kept_blocked": True})
                return "stays_blocked"
            _crow = self.conn.execute(
                "SELECT child_role, title, body, category FROM tasks WHERE id=%s",
                (task_id,),
            ).fetchone()
            _eff_category = category or _crow[3]
            _is_planned = planned_step or (_crow[0] or "work") == "human_check"
            if (status not in ("done", "archived", "blocked", "human")
                    and not _is_planned and _eff_category in _HUMAN_DECISION_CATEGORIES):
                if current_run_id:
                    self.conn.execute(
                        "UPDATE task_runs SET status='reclaimed', outcome='reclaimed', "
                        "summary=COALESCE(summary,'invariant recovery on blocked "
                        "(F45 reroute)'), ended_at=%s, claim_lock=NULL, "
                        "claim_expires=NULL, worker_pid=NULL "
                        "WHERE id=%s AND ended_at IS NULL", (now, int(current_run_id)),
                    )
                bsets = ["status='blocked'", "assignee='till'",
                         "block_kind='needs_input'", "current_run_id=NULL",
                         "claim_lock=NULL", "claim_expires=NULL", "worker_pid=NULL"]
                bparams = []
                if title is not None:
                    bsets.append("title=%s"); bparams.append(title)
                if kurzbeschreibung is not None:
                    bsets.append("body=%s"); bparams.append(kurzbeschreibung)
                if category is not None:
                    bsets.append("category=%s"); bparams.append(category)
                bparams.append(task_id)
                self.conn.execute(
                    f"UPDATE tasks SET {', '.join(bsets)} WHERE id=%s", bparams)
                if not self._card_has_nontech_zielfrage(task_id):
                    self.conn.execute(
                        "INSERT INTO task_comments (task_id,author,body,created_at) "
                        "VALUES (%s,%s,%s,%s)",
                        (task_id, actor or "manager",
                         self._owner_decision_zielfrage(
                             title if title is not None else _crow[1],
                             kurzbeschreibung if kurzbeschreibung is not None else _crow[2]),
                         now),
                    )
                self._append_event(task_id, "blocked",
                                   {"kind": "needs_input", "from": status,
                                    "category": _eff_category, "actor": actor,
                                    "reason": "F45 owner-decision reroute"})
                return "rerouted_blocked"
            if current_run_id:
                self.conn.execute(
                    "UPDATE task_runs SET status='reclaimed', outcome='reclaimed', "
                    "summary=COALESCE(summary,'invariant recovery on human'), "
                    "ended_at=%s, claim_lock=NULL, claim_expires=NULL, worker_pid=NULL "
                    "WHERE id=%s AND ended_at IS NULL", (now, int(current_run_id)),
                )
            sets = ["status='human'", "current_run_id=NULL"]
            params = []
            if title is not None:
                sets.append("title=%s"); params.append(title)
            if kurzbeschreibung is not None:
                sets.append("body=%s"); params.append(kurzbeschreibung)
            if category is not None:
                sets.append("category=%s"); params.append(category)
            params.append(task_id)
            self.conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=%s", params)
            self._append_event(task_id, "human",
                               {"reason": "escalated to human", "from": status,
                                "category": category, "actor": actor})
        return "moved"

    # ----- block_task (DB-Kern + Review-Handoff; owner-fold = Grenze) -----
    def block_task_full(self, task_id, *, reason=None, kind=None, expected_run_id=None,
                        reviewer_resolver=None):
        if kind is not None and kind not in VALID_BLOCK_KINDS:
            raise ValueError(f"block kind must be one of {sorted(VALID_BLOCK_KINDS)} or None")
        _rid_clause = "" if expected_run_id is None else " AND current_run_id=%s"

        def _rid_params(*base):
            return tuple(base) if expected_run_id is None else tuple(base) + (int(expected_run_id),)

        with self._txn():
            cur_row = self.conn.execute(
                "SELECT status, block_kind, block_recurrences, tenant, assignee, "
                "branch_name FROM tasks WHERE id=%s", (task_id,),
            ).fetchone()
            if cur_row is None:
                return False
            c_status, prev_kind, prev_rec, c_tenant, c_assignee, c_branch = cur_row
            prev_rec = int(prev_rec or 0)

            # dependency → todo (GRENZE: F43/F56 owner-fold-Varianten nicht portiert).
            if kind == "dependency":
                cur = self.conn.execute(
                    "UPDATE tasks SET status='todo', claim_lock=NULL, claim_expires=NULL, "
                    "worker_pid=NULL, block_kind=%s "
                    "WHERE id=%s AND status IN ('running','ready')" + _rid_clause,
                    _rid_params(kind, task_id),
                )
                if cur.rowcount != 1:
                    return False
                run_id = self._end_run(task_id, outcome="blocked", status="blocked",
                                       summary=reason)
                if run_id is None and reason:
                    run_id = self._synthesize_ended_run(task_id, outcome="blocked",
                                                        summary=reason)
                self._append_event(task_id, "dependency_wait",
                                   {"reason": reason, "kind": kind}, run_id=run_id)
                return True

            # review → review-Spalte (Reviewer via injizierbarem Resolver = Grenze).
            if kind == "review":
                reviewer = None
                if reviewer_resolver is not None:
                    reviewer = reviewer_resolver(task_id, c_tenant)
                if not reviewer:
                    reviewer = f"{c_tenant}-reviewer" if c_tenant else "reviewer"
                if c_assignee == reviewer:
                    # Reviewer selbst blockt mit kind=review → Rejection → Implementer.
                    impl = self.conn.execute(
                        "SELECT profile FROM task_runs WHERE task_id=%s AND profile IS NOT NULL "
                        "AND profile!=%s ORDER BY id DESC LIMIT 1", (task_id, reviewer),
                    ).fetchone()
                    implementer = impl[0] if impl else None
                    if implementer:
                        cur = self.conn.execute(
                            "UPDATE tasks SET status='ready', assignee=%s, claim_lock=NULL, "
                            "claim_expires=NULL, worker_pid=NULL, block_kind=NULL "
                            "WHERE id=%s AND status IN ('running','ready')" + _rid_clause,
                            _rid_params(implementer, task_id),
                        )
                        if cur.rowcount != 1:
                            return False
                        run_id = self._end_run(task_id, outcome="blocked", status="blocked",
                                               error=f"review rejected → {implementer}: {reason}")
                        if run_id is None:
                            run_id = self._synthesize_ended_run(
                                task_id, outcome="blocked",
                                summary=f"review rejected → {implementer}")
                        self._append_event(task_id, "review_rejected",
                                           {"reason": reason, "reviewer": reviewer,
                                            "implementer": implementer, "tenant": c_tenant},
                                           run_id=run_id)
                        self.conn.execute(
                            "INSERT INTO task_comments (task_id,author,body,created_at) "
                            "VALUES (%s,%s,%s,%s)",
                            (task_id, reviewer,
                             f"REVIEW REJECTED — zurück an {implementer}: {reason}", now_epoch()),
                        )
                        return True
                    cur = self.conn.execute(
                        "UPDATE tasks SET status='blocked', assignee='till', claim_lock=NULL, "
                        "claim_expires=NULL, worker_pid=NULL, block_kind='needs_input' "
                        "WHERE id=%s AND status IN ('running','ready')" + _rid_clause,
                        _rid_params(task_id),
                    )
                    if cur.rowcount != 1:
                        return False
                    run_id = self._end_run(task_id, outcome="blocked", status="blocked",
                                           error=f"review rejected, no implementer: {reason}")
                    self._append_event(task_id, "blocked",
                                       {"reason": f"review rejected, no implementer "
                                        f"derivable: {reason}", "kind": "needs_input",
                                        "reviewer": reviewer}, run_id=run_id)
                    return True
                cur = self.conn.execute(
                    "UPDATE tasks SET status='review', assignee=%s, claim_lock=NULL, "
                    "claim_expires=NULL, worker_pid=NULL, block_kind=%s "
                    "WHERE id=%s AND status IN ('running','ready')" + _rid_clause,
                    _rid_params(reviewer, kind, task_id),
                )
                if cur.rowcount != 1:
                    return False
                if (c_assignee or "").endswith("-coder") and not (c_branch or "").strip():
                    self.conn.execute("UPDATE tasks SET branch_name=%s WHERE id=%s",
                                      (f"wt/{task_id}", task_id))
                    self._append_event(task_id, "branch_recorded",
                                       {"branch": f"wt/{task_id}", "source": "review-handoff",
                                        "implementer": c_assignee})
                run_id = self._end_run(task_id, outcome="blocked", status="blocked",
                                       error=f"review requested → {reviewer}")
                if run_id is None:
                    run_id = self._synthesize_ended_run(task_id, outcome="blocked",
                                                        summary=f"review requested → {reviewer}")
                self._append_event(task_id, "blocked",
                                   {"reason": reason, "kind": kind, "reviewer": reviewer,
                                    "tenant": c_tenant}, run_id=run_id)
                return True

            # truly-blocked: Recurrence-Loop-Breaker.
            same_cause = prev_kind == kind
            recurrences = prev_rec + 1 if same_cause else 1
            if recurrences >= BLOCK_RECURRENCE_LIMIT:
                _owner_loop = (kind in _ZIELFRAGE_BLOCK_KINDS
                               and self._card_has_nontech_zielfrage(task_id))
                _merger_loop = bool(c_assignee) and (
                    c_assignee.strip().lower() == "merger"
                    or c_assignee.strip().lower().endswith("-merger"))
                if _merger_loop:
                    cur = self.conn.execute(
                        "UPDATE tasks SET status='blocked', claim_lock=NULL, "
                        "claim_expires=NULL, worker_pid=NULL, block_kind=%s, "
                        "block_recurrences=%s WHERE id=%s AND status IN ('running','ready')"
                        + _rid_clause, _rid_params(kind, recurrences, task_id))
                    if cur.rowcount != 1:
                        return False
                    run_id = self._end_run(task_id, outcome="blocked", status="blocked",
                                           summary=reason)
                    if run_id is None and reason:
                        run_id = self._synthesize_ended_run(task_id, outcome="blocked",
                                                            summary=reason)
                    self._append_event(task_id, "blocked",
                                       {"reason": reason, "kind": kind,
                                        "recurrences": recurrences, "escalation": "merger_loop"},
                                       run_id=run_id)
                    self._append_event(task_id, "merger_loop_escalated",
                                       {"routed_to": "blocked", "recurrences": recurrences,
                                        "limit": BLOCK_RECURRENCE_LIMIT}, run_id=run_id)
                    return True
                if _owner_loop:
                    cur = self.conn.execute(
                        "UPDATE tasks SET status='blocked', assignee='till', claim_lock=NULL, "
                        "claim_expires=NULL, worker_pid=NULL, block_kind='needs_input', "
                        "category=CASE WHEN category IN "
                        "('frage','entscheidung','abnahme','go_noetig') THEN category "
                        "ELSE 'entscheidung' END, block_recurrences=%s "
                        "WHERE id=%s AND status IN ('running','ready')" + _rid_clause,
                        _rid_params(recurrences, task_id))
                    if cur.rowcount != 1:
                        return False
                    run_id = self._end_run(task_id, outcome="blocked", status="blocked",
                                           summary="owner Zielentscheidung loop → blocked@till (F50)")
                    if run_id is None:
                        run_id = self._synthesize_ended_run(
                            task_id, outcome="blocked",
                            summary="owner Zielentscheidung loop → blocked@till (F50)")
                    self._append_event(task_id, "blocked",
                                       {"reason": reason, "kind": "needs_input",
                                        "recurrences": recurrences,
                                        "escalation": "owner_decision_loop"}, run_id=run_id)
                    self._append_event(task_id, "owner_decision_loop_escalated",
                                       {"routed_to": "till", "recurrences": recurrences,
                                        "limit": BLOCK_RECURRENCE_LIMIT}, run_id=run_id)
                    return True
                cur = self.conn.execute(
                    "UPDATE tasks SET status='triage', claim_lock=NULL, claim_expires=NULL, "
                    "worker_pid=NULL, block_kind=%s, block_recurrences=%s "
                    "WHERE id=%s AND status IN ('running','ready')" + _rid_clause,
                    _rid_params(kind, recurrences, task_id))
                if cur.rowcount != 1:
                    return False
                run_id = self._end_run(task_id, outcome="blocked", status="blocked",
                                       summary=reason)
                if run_id is None and reason:
                    run_id = self._synthesize_ended_run(task_id, outcome="blocked",
                                                        summary=reason)
                self._append_event(task_id, "block_loop_detected",
                                   {"reason": reason, "kind": kind, "recurrences": recurrences,
                                    "limit": BLOCK_RECURRENCE_LIMIT}, run_id=run_id)
                return True
            # Normaler blocked-Pfad (unter Limit).
            cur = self.conn.execute(
                "UPDATE tasks SET status='blocked', claim_lock=NULL, claim_expires=NULL, "
                "worker_pid=NULL, block_kind=%s, block_recurrences=%s "
                "WHERE id=%s AND status IN ('running','ready')" + _rid_clause,
                _rid_params(kind, recurrences, task_id))
            if cur.rowcount != 1:
                return False
            run_id = self._end_run(task_id, outcome="blocked", status="blocked", summary=reason)
            if run_id is None and reason:
                run_id = self._synthesize_ended_run(task_id, outcome="blocked", summary=reason)
            self._append_event(task_id, "blocked",
                               {"reason": reason, "kind": kind, "recurrences": recurrences},
                               run_id=run_id)
            return True
