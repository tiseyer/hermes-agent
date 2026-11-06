"""B1.1 Welle-2 (tasks) — faithful-Port-Tests. Dual-mode (pgserver | echtes PG).

    HERMES_KANBAN_PG_DSN=... PYTHONPATH=<repo> python tests/pg/test_tasks.py

Muss-Fälle (Karte b1.1-port-tasks): create→stamp-Trigger; block/unblock-Zyklus
(block_kind); complete triggert recompute_ready auf Kinder + consecutive_failures-
Reset; promote todo→ready nur bei done-Parents; archive; reassign/move_to_human
Status+Events; relabel ändert NUR Titel/Kurzbeschreibung (kein Status).
"""
import os
import pathlib
import shutil
import sys
import tempfile

import psycopg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from kanban_pg import PgBackend  # noqa: E402

PKG = pathlib.Path(__file__).resolve().parents[2] / "kanban_pg"
PROD_DSN = os.environ.get("HERMES_KANBAN_PG_DSN", "").strip() or None
FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class Env:
    def __init__(self, name):
        self.name = name
        self._srv = None
        self._dir = None
        self._schema = None
        if PROD_DSN:
            self._schema = f"b1t_{name}_{os.getpid()}"
            with psycopg.connect(PROD_DSN, autocommit=True) as c:
                c.execute(f'DROP SCHEMA IF EXISTS "{self._schema}" CASCADE')
                c.execute(f'CREATE SCHEMA "{self._schema}"')
            sep = "&" if "?" in PROD_DSN else "?"
            self.uri = f"{PROD_DSN}{sep}options=-c%20search_path%3D{self._schema}%2Cpublic"
        else:
            xdg = pathlib.Path(tempfile.mkdtemp(prefix="xdg-"))
            os.environ.setdefault("XDG_RUNTIME_DIR", str(xdg))
            import pgserver
            self._dir = pathlib.Path(tempfile.mkdtemp(prefix="pgdata-"))
            self._srv = pgserver.get_server(self._dir)
            self.uri = self._srv.get_uri()

    def connect(self, autocommit=False):
        return psycopg.connect(self.uri, autocommit=autocommit)

    def teardown(self):
        if PROD_DSN:
            with psycopg.connect(PROD_DSN, autocommit=True) as c:
                c.execute(f'DROP SCHEMA IF EXISTS "{self._schema}" CASCADE')
        else:
            self._srv.cleanup()
            shutil.rmtree(self._dir, ignore_errors=True)


def _apply_schema(env):
    with env.connect(autocommit=True) as c, c.cursor() as cur:
        cur.execute((PKG / "schema.sql").read_text())
        cur.execute((PKG / "triggers.sql").read_text())


def _events(q, task_id):
    return [r[0] for r in q.execute(
        "SELECT kind FROM task_events WHERE task_id=%s ORDER BY id", (task_id,)
    ).fetchall()]


def _status(q, task_id):
    r = q.execute("SELECT status FROM tasks WHERE id=%s", (task_id,)).fetchone()
    return r[0] if r else None


def main():
    print(f"=== tasks — Modus: {'ECHTES PG (Wegwerf-Schema)' if PROD_DSN else 'pgserver'} ===")
    e = Env("core")
    try:
        _apply_schema(e)
        conn = e.connect(autocommit=False)
        q = e.connect(autocommit=True)
        try:
            b = PgBackend(conn)

            print("[create_task_full → stamp-Trigger + Status-Auflösung]")
            t1 = b.create_task_full(title="solo", assignee="worker")
            row = q.execute("SELECT status, created_at, status_changed_at FROM tasks WHERE id=%s", (t1,)).fetchone()
            check("create ohne Parents → ready", row[0] == "ready")
            check("stamp-Trigger: status_changed_at == created_at", row[2] == row[1] and row[2] is not None)
            check("created-Event", "created" in _events(q, t1))

            print("[create mit Parents: undone→todo, done→ready]")
            p_done = b.create_task_full(title="pdone")
            b.complete_task_full(p_done)  # → done
            c_ready = b.create_task_full(title="cready", parents=[p_done])
            check("create mit done-Parent → ready", _status(q, c_ready) == "ready")
            p_open = b.create_task_full(title="popen")
            c_todo = b.create_task_full(title="ctodo", parents=[p_open])
            check("create mit undone-Parent → todo", _status(q, c_todo) == "todo")

            print("[create triage]")
            t_tr = b.create_task_full(title="triage me", triage=True)
            check("triage=True → triage", _status(q, t_tr) == "triage")

            print("[complete triggert recompute_ready + failure-reset]")
            par = b.create_task_full(title="par", assignee="worker")
            q.execute("UPDATE tasks SET consecutive_failures=2 WHERE id=%s", (par,))
            kid = b.create_task_full(title="kid", parents=[par])
            check("kid startet todo (par offen)", _status(q, kid) == "todo")
            check("complete par → True", b.complete_task_full(par, summary="fertig") is True)
            check("par done + failures=0", q.execute("SELECT status, consecutive_failures FROM tasks WHERE id=%s", (par,)).fetchone() == ("done", 0))
            check("kid via recompute → ready", _status(q, kid) == "ready")
            check("completed-Event", "completed" in _events(q, par))

            print("[block/unblock-Zyklus (block_kind)]")
            bt = b.create_task_full(title="blockme", assignee="worker")
            q.execute("UPDATE tasks SET status='running' WHERE id=%s", (bt,))
            check("block needs_input → blocked+block_kind", b.block_task_full(bt, kind="needs_input", reason="warte") is True
                  and q.execute("SELECT status, block_kind, block_recurrences FROM tasks WHERE id=%s", (bt,)).fetchone() == ("blocked", "needs_input", 1))
            check("sticky blocked-Event", "blocked" in _events(q, bt))
            check("unblock → ready (keine Parents) + unblocked-Event", b.unblock_task(bt) is True
                  and _status(q, bt) == "ready" and "unblocked" in _events(q, bt))

            print("[block dependency → todo]")
            dt = b.create_task_full(title="dep", assignee="worker")
            q.execute("UPDATE tasks SET status='running' WHERE id=%s", (dt,))
            check("block dependency → todo", b.block_task_full(dt, kind="dependency") is True and _status(q, dt) == "todo")
            check("dependency_wait-Event", "dependency_wait" in _events(q, dt))

            print("[block recurrence → triage (Loop-Breaker)]")
            rt = b.create_task_full(title="loopy", assignee="worker")
            q.execute("UPDATE tasks SET status='running' WHERE id=%s", (rt,))
            b.block_task_full(rt, kind="capability", reason="x")  # rec=1 → blocked
            b.unblock_task(rt)
            q.execute("UPDATE tasks SET status='running' WHERE id=%s", (rt,))
            b.block_task_full(rt, kind="capability", reason="x")  # rec=2 ≥ LIMIT → triage
            check("2. gleicher Block → triage", _status(q, rt) == "triage")
            check("block_loop_detected-Event", "block_loop_detected" in _events(q, rt))

            print("[block review → review-Spalte + Reviewer (tenant-fallback)]")
            rv = b.create_task_full(title="implement x", assignee="voicera-coder", tenant="voicera")
            q.execute("UPDATE tasks SET status='running' WHERE id=%s", (rv,))
            check("block review → review@voicera-reviewer", b.block_task_full(rv, kind="review", reason="bitte prüfen") is True
                  and q.execute("SELECT status, assignee, branch_name FROM tasks WHERE id=%s", (rv,)).fetchone() == ("review", "voicera-reviewer", f"wt/{rv}"))

            print("[promote: nur bei done-Parents]")
            pp = b.create_task_full(title="pp")
            cc = b.create_task_full(title="cc", parents=[pp])  # pp offen → cc todo
            ok, why = b.promote_task(cc, actor="till")
            check("promote mit offenem Parent → abgelehnt", ok is False and "unsatisfied" in (why or ""))
            b.complete_task_full(pp)
            # cc wurde via recompute schon ready; teste promote auf frischem todo:
            tp = b.create_task_full(title="tp")
            q.execute("UPDATE tasks SET status='todo' WHERE id=%s", (tp,))
            ok2, _ = b.promote_task(tp, actor="till", reason="go")
            check("promote parent-los todo → ready", ok2 is True and _status(q, tp) == "ready")
            check("promoted_manual-Event", "promoted_manual" in _events(q, tp))

            print("[archive + F50-Schutz]")
            at = b.create_task_full(title="archiveme")
            check("archive → archived", b.archive_task(at) is True and _status(q, at) == "archived")
            check("archived-Event", "archived" in _events(q, at))
            prot = b.create_task_full(title="owner-frage")
            q.execute("UPDATE tasks SET status='blocked', category='frage' WHERE id=%s", (prot,))
            check("F50: blocked owner-Zielfrage NICHT archivierbar", b.archive_task(prot) is False and _status(q, prot) == "blocked")
            check("archive_refused-Event", "archive_refused" in _events(q, prot))

            print("[reassign Status+Event]")
            ra = b.create_task_full(title="reassignme", assignee="alice")
            check("reassign → neuer assignee + assigned-Event", b.reassign_task_full(ra, "bob") is True
                  and q.execute("SELECT assignee FROM tasks WHERE id=%s", (ra,)).fetchone()[0] == "bob"
                  and "assigned" in _events(q, ra))

            print("[move_to_human: moved + F45-Reroute]")
            mh = b.create_task_full(title="escalate", assignee="worker")
            check("move_to_human (keine decision-cat) → moved + human", b.move_to_human(mh, category="blocker", actor="mgr") == "moved"
                  and _status(q, mh) == "human" and "human" in _events(q, mh))
            f45 = b.create_task_full(title="owner decision", assignee="worker")
            q.execute("UPDATE tasks SET status='running' WHERE id=%s", (f45,))
            check("F45: active decision-card → rerouted_blocked@till", b.move_to_human(f45, category="entscheidung", actor="mgr") == "rerouted_blocked"
                  and q.execute("SELECT status, assignee, block_kind FROM tasks WHERE id=%s", (f45,)).fetchone() == ("blocked", "till", "needs_input"))

            print("[relabel: NUR title/short_description, kein Status]")
            rl = b.create_task_full(title="alt", assignee="worker")
            st_before = _status(q, rl)
            check("relabel True", b.relabel_task(rl, title="neu", kurzbeschreibung="kurz", actor="mgr") is True)
            rr = q.execute("SELECT title, short_description, status FROM tasks WHERE id=%s", (rl,)).fetchone()
            check("title+short_description geändert, Status unverändert", rr == ("neu", "kurz", st_before))
            check("relabeled-Event", "relabeled" in _events(q, rl))

            print("[setter + edit_completed]")
            sx = b.create_task_full(title="setme")
            b.set_workspace_path(sx, "/tmp/x"); b.set_branch_name(sx, "wt/x")
            check("setter schreiben", q.execute("SELECT workspace_path, branch_name FROM tasks WHERE id=%s", (sx,)).fetchone() == ("/tmp/x", "wt/x"))
            ec = b.create_task_full(title="donecard")
            b.complete_task_full(ec)
            check("edit_completed_result", b.edit_completed_task_result(ec, result="final", summary="s") is True
                  and q.execute("SELECT result FROM tasks WHERE id=%s", (ec,)).fetchone()[0] == "final")
        finally:
            conn.close(); q.close()
    finally:
        e.teardown()

    print(f"\nTESTS tasks ({'PROD-PG' if PROD_DSN else 'pgserver'}): "
          f"{'ALLE GRÜN' if not FAILS else f'{len(FAILS)} ROT: {FAILS}'}")
    return 0 if not FAILS else 2


if __name__ == "__main__":
    sys.exit(main())
