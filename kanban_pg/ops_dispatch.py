"""Dispatcher-Domäne (Welle 1) — faithful Port des DB-Zustandsautomaten.

Dies ist das Fundament des P7b-Ports: die geteilten PG-Äquivalente, auf die
Welle 2/3 bauen (``recompute_ready`` / ``_append_event`` /
``get_spawn_rejection_reason`` / ``release_stale_claims``). Jede Invariante der
gelisteten ``hermes_cli/kanban_db.py``-Funktionen ist hier 1:1 nachgebaut —
spawn_rejection, parents_not_done→demote, Event-Anhang, claim_expires/ttl,
Circuit-Breaker (consecutive_failures / DEFAULT_FAILURE_LIMIT), die
recompute_ready-Semantik (F55-Repark, sticky-block, GO-Gate-Release,
human_check→human, Breaker-Guard).

GRENZEN (Integrator-Entscheid D4, siehe HANDOFF-PORT.md): Worker-*Spawn*,
OS-*Prozess*-Terminierung (``_pid_alive`` / SIGTERM/SIGKILL) und die
Infra-*Reroute-Policy* (Merger-/Diagnose-/Repo-Profil-Auflösung in
``_record_task_failure``) sind injizierbare Grenzen — exakt wie der Decompose-
LLM und der amcheck-Health-Pfad. Der reine DB-Kern (Status/Events/Runs/Zähler)
ist faithful; die Policy-Schicht wird von der Watchers-Welle verdrahtet. Default
``kanban.backend=sqlite`` → dieser Pfad bleibt inert.
"""
import contextlib
import json
import re

from ._base import now_epoch, claimer_id, default_pid_alive
from .lock_model import (
    try_board_session_lock,
    release_board_session_lock,
    claim_one_ready,  # noqa: F401 (Struktur-Primitive; von Tests genutzt)
)

# --- Konstanten, faithful zu kanban_db.py ---------------------------------
DEFAULT_FAILURE_LIMIT = 2
DEFAULT_CLAIM_TTL_SECONDS = 15 * 60
DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS = 60 * 60
RECLAIM_DEFER_GRACE_SECONDS = 120
_HUMAN_DECISION_CATEGORIES = frozenset(
    {"frage", "entscheidung", "abnahme", "go_noetig"}
)
_ZIELFRAGE_BLOCK_KINDS = (None, "needs_input")
_ZIELFRAGE_TECH_REGEX = re.compile(
    r"\b(rebase|port|library|bibliothek|dependency|abh[äa]ngigkeit|pfad|path|"
    r"version|framework)\b",
    re.IGNORECASE,
)


def _resolve_claim_ttl_seconds(ttl_seconds=None, env=None):
    """Faithful zu kanban_db._resolve_claim_ttl_seconds."""
    if ttl_seconds is not None:
        return max(1, int(ttl_seconds))
    import os
    raw = (env or os.environ).get("HERMES_KANBAN_CLAIM_TTL_SECONDS", "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = 0
        if parsed > 0:
            return parsed
    return DEFAULT_CLAIM_TTL_SECONDS


class DispatchMixin:
    """Faithful Claim-/Dispatcher-/Promotion-Logik auf einer psycopg3-Verbindung."""

    # ----- Transaktions-Helfer (autocommit=False) -------------------------
    @contextlib.contextmanager
    def _txn(self):
        """EINE DB-Transaktion; commit bei Erfolg, rollback bei Fehler.

        Ersatz für write_txn(conn) (BEGIN IMMEDIATE/COMMIT) — unter psycopg3
        (autocommit=False) läuft bereits implizit eine Transaktion; wir
        committen am Ende bzw. rollen bei Exception zurück.
        """
        try:
            yield
        except Exception:
            try:
                self.conn.rollback()
            except Exception:
                pass
            raise
        else:
            self.conn.commit()

    # ----- geteilte Helfer (Welle 2 nutzt diese) --------------------------
    def _append_event(self, task_id, kind, payload=None, *, run_id=None):
        """Event-Zeile anhängen. Läuft INNERHALB einer offenen Transaktion
        (kein Commit) — exakt wie kanban_db._append_event."""
        pl = json.dumps(payload, ensure_ascii=False) if payload else None
        self.conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (task_id, run_id, kind, pl, now_epoch()),
        )

    def _current_run_id(self, task_id):
        row = self.conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = %s", (task_id,),
        ).fetchone()
        return int(row[0]) if row and row[0] else None

    def _end_run(self, task_id, *, outcome, summary=None, error=None,
                 metadata=None, status=None):
        """Aktiven Run schließen + Pointer räumen. Faithful zu _end_run.
        Läuft INNERHALB einer offenen Transaktion (kein Commit)."""
        now = now_epoch()
        row = self.conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = %s", (task_id,),
        ).fetchone()
        if not row or not row[0]:
            return None
        run_id = int(row[0])
        self.conn.execute(
            "UPDATE task_runs SET status=%s, outcome=%s, summary=%s, error=%s, "
            "metadata=%s, ended_at=%s, claim_lock=NULL, claim_expires=NULL, "
            "worker_pid=NULL WHERE id=%s AND ended_at IS NULL",
            (status or outcome, outcome, summary, error,
             json.dumps(metadata, ensure_ascii=False) if metadata else None,
             now, run_id),
        )
        self.conn.execute(
            "UPDATE tasks SET current_run_id=NULL WHERE id=%s", (task_id,),
        )
        return run_id

    # ----- read-only Prädikate (kein Commit) ------------------------------
    def _has_sticky_block(self, task_id):
        row = self.conn.execute(
            "SELECT kind FROM task_events "
            "WHERE task_id=%s AND kind IN ('blocked','unblocked') "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return bool(row) and row[0] == "blocked"

    def _go_gate_released(self, task_id):
        row = self.conn.execute(
            "SELECT kind FROM task_events "
            "WHERE task_id=%s AND kind IN ('blocked','unblocked') "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return bool(row) and row[0] == "unblocked"

    def _go_gate_decision_released(self, task_id):
        rows = self.conn.execute(
            "SELECT id, kind, payload FROM task_events "
            "WHERE task_id=%s AND kind IN ('blocked','unblocked') "
            "ORDER BY id DESC",
            (task_id,),
        ).fetchall()
        go_block_id = None
        for rid, kind, payload in rows:
            if kind != "blocked":
                continue
            try:
                pl = json.loads(payload) if payload else {}
            except (ValueError, TypeError):
                pl = {}
            if isinstance(pl, dict) and pl.get("category") == "go_noetig":
                go_block_id = rid
                break
        if go_block_id is None:
            return self._go_gate_released(task_id)
        return any(k == "unblocked" and i > go_block_id for i, k, _ in rows)

    def _dependency_parent_satisfied(self, pid, pstatus, pcategory):
        if pstatus in ("done", "archived"):
            return True
        if (pcategory or "") == "go_noetig" and self._go_gate_decision_released(pid):
            return True
        return False

    def _held_owner_decision(self, task_id):
        row = self.conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id=%s AND kind='blocked' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if not row:
            return False
        try:
            pl = json.loads(row[0]) if row[0] else {}
        except (ValueError, TypeError):
            return False
        return isinstance(pl, dict) and pl.get("escalation") == "owner_decision_loop"

    def _blocked_card_has_zielfrage(self, task_id):
        row = self.conn.execute(
            "SELECT block_kind FROM tasks WHERE id=%s", (task_id,),
        ).fetchone()
        if row is None:
            return False
        if row[0] not in _ZIELFRAGE_BLOCK_KINDS:
            return False
        comments = self.conn.execute(
            "SELECT body FROM task_comments WHERE task_id=%s ORDER BY created_at ASC",
            (task_id,),
        ).fetchall()
        frage = ""
        for (body,) in reversed(comments):
            if "?" in (body or ""):
                frage = body
                break
        if not frage:
            return False
        return not _ZIELFRAGE_TECH_REGEX.search(frage)

    def _is_owner_decision_card(self, task_id, *, category=None, status=None):
        if category is None or status is None:
            row = self.conn.execute(
                "SELECT status, category FROM tasks WHERE id=%s", (task_id,),
            ).fetchone()
            if row is None:
                return False
            if status is None:
                status = row[0]
            if category is None:
                category = row[1]
        if (category or "").strip() in _HUMAN_DECISION_CATEGORIES:
            return True
        if self._held_owner_decision(task_id):
            return True
        if status == "blocked" and self._blocked_card_has_zielfrage(task_id):
            return True
        return False

    def get_spawn_rejection_reason(self, task_id):
        """Struktureller Worker-Ablehnungsgrund oder None. Faithful."""
        row = self.conn.execute(
            "SELECT family_root_id FROM tasks WHERE id=%s", (task_id,),
        ).fetchone()
        if row is None:
            return "missing_task"
        if row[0] == task_id:
            return "family_root"
        parents = self.conn.execute(
            "SELECT p.id, p.status, p.category FROM task_links l "
            "JOIN tasks p ON p.id = l.parent_id WHERE l.child_id=%s",
            (task_id,),
        ).fetchall()
        if not all(
            self._dependency_parent_satisfied(pid, pstatus, pcat)
            for pid, pstatus, pcat in parents
        ):
            return "parents_not_done"
        return None

    # ----- recompute_ready (todo/blocked -> ready), faithful --------------
    def recompute_ready(self, failure_limit=None):
        if failure_limit is None:
            failure_limit = DEFAULT_FAILURE_LIMIT
        promoted = 0
        with self._txn():
            # F55: owner-decision-Karte, die in 'scheduled' geleakt ist, zurück
            # nach blocked@till (sticky), Kategorie bewahrend.
            for sid, scat in self.conn.execute(
                "SELECT id, category FROM tasks WHERE status='scheduled'"
            ).fetchall():
                if not self._is_owner_decision_card(
                    sid, category=scat, status="scheduled"
                ):
                    continue
                _scat = (scat or "").strip()
                _park = _scat if _scat in _HUMAN_DECISION_CATEGORIES else "entscheidung"
                cur = self.conn.execute(
                    "UPDATE tasks SET status='blocked', assignee='till', "
                    "block_kind='needs_input', category=%s, current_run_id=NULL, "
                    "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL "
                    "WHERE id=%s AND status='scheduled'",
                    (_park, sid),
                )
                if cur.rowcount == 1:
                    self._append_event(
                        sid, "blocked",
                        {"kind": "needs_input", "from": "scheduled",
                         "category": _park, "escalation": "owner_decision_loop",
                         "reason": "F55: owner-decision card re-parked "
                                   "scheduled→blocked@till"},
                    )
                    promoted += 1
            todo_rows = self.conn.execute(
                "SELECT id, status, consecutive_failures, max_retries, child_role "
                "FROM tasks WHERE status IN ('todo','blocked')"
            ).fetchall()
            for tid, cur_status, cfail, max_retries, child_role in todo_rows:
                if cur_status == "blocked" and self._has_sticky_block(tid):
                    continue
                parents = self.conn.execute(
                    "SELECT t.id, t.status, t.category FROM tasks t "
                    "JOIN task_links l ON l.parent_id = t.id WHERE l.child_id=%s",
                    (tid,),
                ).fetchall()
                if not all(
                    self._dependency_parent_satisfied(pid, ps, pc)
                    for pid, ps, pc in parents
                ):
                    continue
                if child_role == "human_check" and cur_status == "todo":
                    self.conn.execute(
                        "UPDATE tasks SET status='human' WHERE id=%s AND status='todo'",
                        (tid,),
                    )
                    self._append_event(
                        tid, "human",
                        {"reason": "geplanter Till-Schritt ist dran", "from": "todo"},
                    )
                    promoted += 1
                    continue
                if cur_status == "blocked":
                    failures = int(cfail or 0)
                    effective = (
                        int(max_retries) if max_retries is not None
                        else int(failure_limit)
                    )
                    if failures >= effective:
                        continue
                    self.conn.execute(
                        "UPDATE tasks SET status='ready' WHERE id=%s AND status='blocked'",
                        (tid,),
                    )
                else:
                    self.conn.execute(
                        "UPDATE tasks SET status='ready' WHERE id=%s AND status='todo'",
                        (tid,),
                    )
                self._append_event(tid, "promoted", None)
                promoted += 1
        return promoted

    # ----- claim ready -> running, faithful -------------------------------
    def claim_task(self, task_id, *, ttl_seconds=None, claimer=None):
        now = now_epoch()
        lock = claimer or claimer_id()
        expires = now + _resolve_claim_ttl_seconds(ttl_seconds)
        with self._txn():
            reason = self.get_spawn_rejection_reason(task_id)
            if reason == "family_root":
                self._append_event(task_id, "spawn_rejected", {"reason": reason})
                return None
            if reason == "parents_not_done":
                self.conn.execute(
                    "UPDATE tasks SET status='todo' WHERE id=%s AND status='ready'",
                    (task_id,),
                )
                self._append_event(task_id, "claim_rejected",
                                    {"reason": "parents_not_done"})
                self._append_event(task_id, "spawn_rejected",
                                    {"reason": "parents_not_done"})
                return None
            # Defensiv: ein geleakter Run auf einer noch-ready Karte -> schließen.
            stale = self.conn.execute(
                "SELECT current_run_id FROM tasks WHERE id=%s AND status='ready'",
                (task_id,),
            ).fetchone()
            if stale and stale[0]:
                self.conn.execute(
                    "UPDATE task_runs SET status='reclaimed', outcome='reclaimed', "
                    "summary=COALESCE(summary,'invariant recovery on re-claim'), "
                    "ended_at=%s, claim_lock=NULL, claim_expires=NULL, worker_pid=NULL "
                    "WHERE id=%s AND ended_at IS NULL",
                    (now, int(stale[0])),
                )
            cur = self.conn.execute(
                "UPDATE tasks SET status='running', claim_lock=%s, claim_expires=%s, "
                "started_at=COALESCE(started_at,%s) "
                "WHERE id=%s AND status='ready' AND claim_lock IS NULL",
                (lock, expires, now, task_id),
            )
            if cur.rowcount != 1:
                return None
            trow = self.conn.execute(
                "SELECT assignee, max_runtime_seconds, current_step_key "
                "FROM tasks WHERE id=%s", (task_id,),
            ).fetchone()
            run_cur = self.conn.execute(
                "INSERT INTO task_runs (task_id, profile, step_key, status, "
                "claim_lock, claim_expires, max_runtime_seconds, started_at) "
                "VALUES (%s,%s,%s,'running',%s,%s,%s,%s) RETURNING id",
                (task_id, trow[0] if trow else None, trow[2] if trow else None,
                 lock, expires, trow[1] if trow else None, now),
            )
            run_id = int(run_cur.fetchone()[0])
            self.conn.execute(
                "UPDATE tasks SET current_run_id=%s WHERE id=%s", (run_id, task_id),
            )
            self._append_event(task_id, "claimed",
                               {"lock": lock, "expires": expires, "run_id": run_id},
                               run_id=run_id)
        return self.get_task(task_id)

    def claim_review_task(self, task_id, *, ttl_seconds=None, claimer=None):
        now = now_epoch()
        lock = claimer or claimer_id()
        expires = now + _resolve_claim_ttl_seconds(ttl_seconds)
        with self._txn():
            fam = self.conn.execute(
                "SELECT family_root_id FROM tasks WHERE id=%s", (task_id,),
            ).fetchone()
            if fam and fam[0] == task_id:
                return None
            cur = self.conn.execute(
                "UPDATE tasks SET status='running', claim_lock=%s, claim_expires=%s, "
                "started_at=COALESCE(started_at,%s) "
                "WHERE id=%s AND status='review' AND claim_lock IS NULL",
                (lock, expires, now, task_id),
            )
            if cur.rowcount != 1:
                return None
            trow = self.conn.execute(
                "SELECT assignee, max_runtime_seconds, current_step_key "
                "FROM tasks WHERE id=%s", (task_id,),
            ).fetchone()
            run_cur = self.conn.execute(
                "INSERT INTO task_runs (task_id, profile, step_key, status, "
                "claim_lock, claim_expires, max_runtime_seconds, started_at) "
                "VALUES (%s,%s,%s,'running',%s,%s,%s,%s) RETURNING id",
                (task_id, trow[0] if trow else None, trow[2] if trow else None,
                 lock, expires, trow[1] if trow else None, now),
            )
            run_id = int(run_cur.fetchone()[0])
            self.conn.execute(
                "UPDATE tasks SET current_run_id=%s WHERE id=%s", (run_id, task_id),
            )
            self._append_event(task_id, "claimed",
                               {"lock": lock, "expires": expires, "run_id": run_id,
                                "source_status": "review"}, run_id=run_id)
        return self.get_task(task_id)

    def heartbeat_claim(self, task_id, *, ttl_seconds=None, claimer=None):
        expires = now_epoch() + _resolve_claim_ttl_seconds(ttl_seconds)
        lock = claimer or claimer_id()
        with self._txn():
            cur = self.conn.execute(
                "UPDATE tasks SET claim_expires=%s "
                "WHERE id=%s AND status='running' AND claim_lock=%s",
                (expires, task_id, lock),
            )
            if cur.rowcount == 1:
                run_id = self._current_run_id(task_id)
                if run_id is not None:
                    self.conn.execute(
                        "UPDATE task_runs SET claim_expires=%s WHERE id=%s",
                        (expires, run_id),
                    )
                return True
            return False

    def heartbeat_worker(self, task_id, *, note=None, expected_run_id=None):
        now = now_epoch()
        with self._txn():
            if expected_run_id is None:
                cur = self.conn.execute(
                    "UPDATE tasks SET last_heartbeat_at=%s "
                    "WHERE id=%s AND status='running'",
                    (now, task_id),
                )
            else:
                cur = self.conn.execute(
                    "UPDATE tasks SET last_heartbeat_at=%s "
                    "WHERE id=%s AND status='running' AND current_run_id=%s",
                    (now, task_id, int(expected_run_id)),
                )
            if cur.rowcount != 1:
                return False
            run_id = (int(expected_run_id) if expected_run_id is not None
                      else self._current_run_id(task_id))
            if run_id is not None:
                self.conn.execute(
                    "UPDATE task_runs SET last_heartbeat_at=%s WHERE id=%s",
                    (now, run_id),
                )
            self._append_event(task_id, "heartbeat",
                               {"note": note} if note else None, run_id=run_id)
            return True

    # ----- Watchdog: abgelaufene Claims zurückholen, faithful DB-Kern -----
    def release_stale_claims(self, *, signal_fn=None, pid_alive_fn=None,
                             terminate_fn=None):
        """Abgelaufene running-Claims reclaimen. OS-Prozessverwaltung injizierbar:
        ``pid_alive_fn(pid)`` (default schlank, POSIX), ``terminate_fn`` liefert
        das termination-dict (default best-effort no-op). Gibt die Zahl der
        tatsächlich reclaimten Claims zurück (live-pid-Extensions zählen nicht)."""
        pid_alive = pid_alive_fn or default_pid_alive
        terminate = terminate_fn or self._default_terminate
        now = now_epoch()
        reclaimed = 0
        host_prefix = f"{claimer_id().split(':', 1)[0]}:"
        stale = self.conn.execute(
            "SELECT id, claim_lock, worker_pid, claim_expires, last_heartbeat_at "
            "FROM tasks WHERE status='running' AND claim_expires IS NOT NULL "
            "AND claim_expires < %s",
            (now,),
        ).fetchall()
        for tid, lock, pid, cexp, hb in stale:
            lock = lock or ""
            host_local = lock.startswith(host_prefix)
            heartbeat_stale = (
                hb is not None
                and (now - int(hb)) > DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS
            )
            if host_local and pid and pid_alive(pid) and not heartbeat_stale:
                new_expires = now + _resolve_claim_ttl_seconds()
                with self._txn():
                    cur = self.conn.execute(
                        "UPDATE tasks SET claim_expires=%s WHERE id=%s "
                        "AND status='running' "
                        "AND claim_lock IS NOT DISTINCT FROM %s "
                        "AND claim_expires IS NOT NULL AND claim_expires < %s",
                        (new_expires, tid, lock or None, now),
                    )
                    if cur.rowcount != 1:
                        continue
                    run_id = self._current_run_id(tid)
                    if run_id is not None:
                        self.conn.execute(
                            "UPDATE task_runs SET claim_expires=%s WHERE id=%s",
                            (new_expires, run_id),
                        )
                    self._append_event(
                        tid, "claim_extended",
                        {"reason": "pid_alive", "worker_pid": int(pid),
                         "claim_lock": lock, "claim_expires_was": int(cexp),
                         "claim_expires_now": new_expires,
                         "last_heartbeat_at": int(hb) if hb is not None else None},
                        run_id=run_id,
                    )
                continue

            termination = terminate(pid, lock, signal_fn=signal_fn)
            if _worker_survived_termination(termination):
                self._defer_reclaim_for_live_worker(
                    tid, lock or None, now, termination,
                    reason="ttl_expired_worker_alive",
                )
                continue
            with self._txn():
                cur = self.conn.execute(
                    "UPDATE tasks SET status='ready', claim_lock=NULL, "
                    "claim_expires=NULL, worker_pid=NULL WHERE id=%s "
                    "AND status='running' AND claim_lock IS NOT DISTINCT FROM %s "
                    "AND claim_expires IS NOT NULL AND claim_expires < %s",
                    (tid, lock or None, now),
                )
                if cur.rowcount != 1:
                    continue
                run_id = self._end_run(
                    tid, outcome="reclaimed", status="reclaimed",
                    error=f"stale_lock={lock}", metadata=termination,
                )
                payload = {
                    "stale_lock": lock,
                    "worker_pid": int(pid) if pid is not None else None,
                    "claim_expires": int(cexp),
                    "last_heartbeat_at": int(hb) if hb is not None else None,
                    "now": now, "host_local": host_local,
                    "heartbeat_stale": bool(heartbeat_stale),
                }
                payload.update(termination)
                self._append_event(tid, "reclaimed", payload, run_id=run_id)
                reclaimed += 1
        return reclaimed

    def _defer_reclaim_for_live_worker(self, task_id, claim_lock, now,
                                       termination, *, reason):
        grace = now + RECLAIM_DEFER_GRACE_SECONDS
        with self._txn():
            cur = self.conn.execute(
                "UPDATE tasks SET claim_expires=%s WHERE id=%s "
                "AND status='running' AND claim_lock IS NOT DISTINCT FROM %s",
                (grace, task_id, claim_lock),
            )
            if cur.rowcount != 1:
                return
            run_id = self._current_run_id(task_id)
            if run_id is not None:
                self.conn.execute(
                    "UPDATE task_runs SET claim_expires=%s WHERE id=%s",
                    (grace, run_id),
                )
            payload = {"reason": reason, "claim_lock": claim_lock,
                       "claim_expires_now": grace}
            payload.update(termination)
            self._append_event(task_id, "reclaim_deferred", payload, run_id=run_id)

    def reclaim_task(self, task_id, *, reason=None, signal_fn=None,
                     terminate_fn=None):
        terminate = terminate_fn or self._default_terminate
        row = self.conn.execute(
            "SELECT status, claim_lock, worker_pid FROM tasks WHERE id=%s",
            (task_id,),
        ).fetchone()
        if not row:
            return False
        status, prev_lock, pid = row
        if status != "running" and prev_lock is None:
            return False
        termination = terminate(pid, prev_lock, signal_fn=signal_fn)
        with self._txn():
            cur = self.conn.execute(
                "UPDATE tasks SET status='ready', claim_lock=NULL, "
                "claim_expires=NULL, worker_pid=NULL WHERE id=%s "
                "AND status IN ('running','ready','blocked') "
                "AND claim_lock IS NOT DISTINCT FROM %s",
                (task_id, prev_lock),
            )
            if cur.rowcount != 1:
                return False
            run_id = self._end_run(
                task_id, outcome="reclaimed", status="reclaimed",
                error=(f"manual_reclaim: {reason}" if reason
                       else f"manual_reclaim lock={prev_lock}"),
                metadata=termination,
            )
            payload = {"manual": True, "reason": reason, "prev_lock": prev_lock}
            payload.update(termination)
            self._append_event(task_id, "reclaimed", payload, run_id=run_id)
        self._clear_failure_counter(task_id)
        return True

    def _clear_failure_counter(self, task_id):
        with self._txn():
            self.conn.execute(
                "UPDATE tasks SET consecutive_failures=0, last_failure_error=NULL "
                "WHERE id=%s", (task_id,),
            )

    @staticmethod
    def _default_terminate(pid, claim_lock, *, signal_fn=None):
        """Best-effort termination-dict ohne echten Kill (DB-Kern-Default).

        Die Watchers-Welle injiziert die volle host-lokale SIGTERM/SIGKILL-
        Variante. Hier: kein host-local → nie 'survived' → normaler Release."""
        return {"prev_pid": int(pid) if pid else None, "host_local": False,
                "termination_attempted": False, "terminated": False,
                "sigkill": False}

    # ----- Circuit-Breaker (DB-Kern), faithful counter+trip ---------------
    def _record_task_failure(self, task_id, error, *, outcome=None,
                             failure_limit=None, force_trip=False,
                             event_payload_extra=None):
        """Zählt einen Fehlschlag und trippt ggf. den Breaker (DB-Kern).

        GRENZE (D4): die Infra-Reroute-Policy (Merger/Diagnose/Repo-Profil) aus
        kanban_db._record_task_failure ist bewusst NICHT hier — sie ist
        injizierbare Dispatcher-Policy. Der DB-Kern (Zähler hoch, bei >= Limit
        auf 'blocked', 'gave_up'-Event) ist faithful. Gibt True bei Trip."""
        if failure_limit is None:
            failure_limit = DEFAULT_FAILURE_LIMIT
        blocked = False
        with self._txn():
            row = self.conn.execute(
                "SELECT consecutive_failures, status, max_retries "
                "FROM tasks WHERE id=%s", (task_id,),
            ).fetchone()
            if row is None:
                return False
            failures = int(row[0]) + 1
            cur_status = row[1]
            task_override = row[2]
            if task_override is not None:
                effective = int(task_override)
                limit_source = "task"
            else:
                effective = int(failure_limit)
                limit_source = "dispatcher"
            blocked = bool(force_trip or failures >= effective)
            self.conn.execute(
                "UPDATE tasks SET consecutive_failures=%s, last_failure_error=%s, "
                "status = CASE WHEN %s THEN 'blocked' ELSE status END WHERE id=%s",
                (failures, error, blocked, task_id),
            )
            if blocked:
                payload = {"outcome": outcome, "failures": failures,
                           "effective_limit": effective,
                           "limit_source": limit_source,
                           "prev_status": cur_status}
                if event_payload_extra:
                    payload.update(event_payload_extra)
                self._append_event(task_id, "gave_up", payload)
        return blocked

    # ----- Tick (DB-Kern): advisory-lock -> reclaim -> promote -> claim ---
    def dispatch_once(self, *, spawn_fn=None, ttl_seconds=None,
                      failure_limit=DEFAULT_FAILURE_LIMIT, board="default",
                      max_spawn=None, pid_alive_fn=None):
        """EIN Dispatcher-Tick (DB-Kern). Session-Advisory-Lock statt Datei-flock.

        GRENZE (D4): Crash-/Timeout-Detektoren (detect_crashed_workers /
        enforce_max_runtime / reap_worker_zombies) und der Worker-*Spawn* sind
        OS-seitige Grenzen. ``spawn_fn(task_dict) -> Optional[int]`` wird
        injiziert (default no-op); der Rückgabewert wird als worker_pid
        gestempelt. Gibt {'reclaimed','promoted','spawned','spawn_failed'} zurück.
        """
        result = {"reclaimed": 0, "promoted": 0, "spawned": 0, "spawn_failed": 0,
                  "spawned_ids": []}
        if not try_board_session_lock(self.conn, board):
            # Commit, damit der (lesende) Lock-Versuch keine Transaktion offen lässt.
            self.conn.commit()
            result["locked_out"] = True
            return result
        try:
            result["reclaimed"] = self.release_stale_claims(pid_alive_fn=pid_alive_fn)
            result["promoted"] = self.recompute_ready(failure_limit=failure_limit)
            running_count = 0
            if max_spawn is not None:
                running_count = int(self.conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status='running' "
                    "AND (family_root_id IS NULL OR id <> family_root_id)"
                ).fetchone()[0])
            self.conn.commit()
            ready_rows = self.conn.execute(
                "SELECT id FROM tasks WHERE status='ready' AND claim_lock IS NULL "
                "AND assignee IS NOT NULL "
                "AND (family_root_id IS NULL OR id <> family_root_id) "
                "ORDER BY priority DESC, created_at ASC"
            ).fetchall()
            self.conn.commit()
            for (tid,) in ready_rows:
                if max_spawn is not None and running_count >= max_spawn:
                    break
                task = self.claim_task(tid, ttl_seconds=ttl_seconds)
                if task is None:
                    continue
                pid = None
                if spawn_fn is not None:
                    try:
                        pid = spawn_fn(task)
                    except Exception:
                        pid = None
                if spawn_fn is not None and pid is None:
                    # Spawn fehlgeschlagen: Claim lösen + Breaker zählen (DB-Kern).
                    self.reclaim_task(tid, reason="spawn_failed")
                    self._record_task_failure(
                        tid, "spawn_failed", outcome="spawn_failed",
                        failure_limit=failure_limit,
                    )
                    result["spawn_failed"] += 1
                    continue
                if pid is not None:
                    with self._txn():
                        self.conn.execute(
                            "UPDATE tasks SET worker_pid=%s WHERE id=%s", (pid, tid),
                        )
                result["spawned"] += 1
                result["spawned_ids"].append(tid)
                running_count += 1
            return result
        finally:
            release_board_session_lock(self.conn, board)
            self.conn.commit()


def _worker_survived_termination(termination):
    """True, wenn wir unseren host-lokalen Worker killen wollten, er aber lebt."""
    return bool(
        termination.get("termination_attempted")
        and termination.get("host_local")
        and not termination.get("terminated")
    )
