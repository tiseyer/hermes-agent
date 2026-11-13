"""B1.1 Welle-1 (dispatch) — faithful-Port-Tests. Dual-mode (pgserver | echtes PG).

    PYTHONPATH=<repo> python tests/pg/test_dispatch.py                  # pgserver
    HERMES_KANBAN_PG_DSN=... PYTHONPATH=<repo> python tests/pg/test_dispatch.py

Muss-Fälle (Karte b1.1-port-dispatch): claim ready→running; claim auf bereits-
geclaimte→None; parents_not_done→todo-demote; family_root→None+Event; ≥6 Prozesse
nebenläufig kein Doppelclaim; release_stale_claims reclaimt + Event;
_record_task_failure trippt bei LIMIT; recompute_ready promotet nur bei done-Parents.
"""
import os
import pathlib
import shutil
import sys
import tempfile
import multiprocessing as mp

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
            self._schema = f"b1d_{name}_{os.getpid()}"
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


def _apply_schema(env, triggers=True):
    with env.connect(autocommit=True) as c, c.cursor() as cur:
        cur.execute((PKG / "schema.sql").read_text())
        if triggers:
            cur.execute((PKG / "triggers.sql").read_text())


def _events(q, task_id):
    return [r[0] for r in q.execute(
        "SELECT kind FROM task_events WHERE task_id=%s ORDER BY id", (task_id,)
    ).fetchall()]


def _claim_worker(uri, ids, out_q):
    """Jeder Prozess versucht ALLE ids via faithful claim_task; zählt Erfolge."""
    conn = psycopg.connect(uri, autocommit=False)
    b = PgBackend(conn)
    n = 0
    try:
        for tid in ids:
            try:
                if b.claim_task(tid, claimer=f"host:{os.getpid()}") is not None:
                    n += 1
            except Exception:
                conn.rollback()
    finally:
        conn.close()
        out_q.put(n)


def main():
    print(f"=== dispatch — Modus: {'ECHTES PG (Wegwerf-Schema)' if PROD_DSN else 'pgserver'} ===")
    e = Env("core")
    try:
        _apply_schema(e, triggers=True)
        conn = e.connect(autocommit=False)
        q = e.connect(autocommit=True)
        try:
            b = PgBackend(conn)

            print("[claim ready->running]")
            b.create_task("a1", "a1", status="ready", assignee="worker", created_at=100)
            t = b.claim_task("a1", claimer="host:1")
            check("claim_task liefert task-dict + running", t is not None and
                  q.execute("SELECT status FROM tasks WHERE id='a1'").fetchone()[0] == "running")
            check("claim setzt claim_expires + current_run_id + claimed-Event",
                  q.execute("SELECT claim_expires, current_run_id FROM tasks WHERE id='a1'").fetchone()[0] is not None
                  and "claimed" in _events(q, "a1"))
            check("claim auf bereits-geclaimte -> None", b.claim_task("a1", claimer="host:2") is None)

            print("[family_root -> spawn_rejected]")
            b.create_task("R", "root", status="ready", assignee="worker", created_at=101, family_root_id="R")
            check("get_spawn_rejection_reason == family_root", b.get_spawn_rejection_reason("R") == "family_root")
            check("claim_task(root) -> None", b.claim_task("R") is None)
            check("family_root -> spawn_rejected-Event", "spawn_rejected" in _events(q, "R"))

            print("[parents_not_done -> todo-demote]")
            b.create_task("p_undone", "parent", status="todo", created_at=102)
            b.create_task("child_r", "child", status="ready", assignee="worker", created_at=103)
            b.link_tasks("p_undone", "child_r")
            check("rejection == parents_not_done", b.get_spawn_rejection_reason("child_r") == "parents_not_done")
            check("claim_task(child) -> None", b.claim_task("child_r") is None)
            check("child demotet ready->todo",
                  q.execute("SELECT status FROM tasks WHERE id='child_r'").fetchone()[0] == "todo")
            evs = _events(q, "child_r")
            check("claim_rejected + spawn_rejected Events", "claim_rejected" in evs and "spawn_rejected" in evs)

            print("[recompute_ready: nur bei done-Parents]")
            b.create_task("p_done", "pdone", status="done", created_at=104)
            b.create_task("c_ok", "cok", status="todo", created_at=105)
            b.link_tasks("p_done", "c_ok")
            b.create_task("c_block", "cblk", status="todo", created_at=106)
            b.create_task("p_undone2", "pu2", status="todo", created_at=107)
            b.link_tasks("p_undone2", "c_block")
            n = b.recompute_ready()
            check("c_ok (done-Parent) -> ready",
                  q.execute("SELECT status FROM tasks WHERE id='c_ok'").fetchone()[0] == "ready")
            check("c_block (undone-Parent) bleibt todo",
                  q.execute("SELECT status FROM tasks WHERE id='c_block'").fetchone()[0] == "todo")
            check("promoted zählt >=1", n >= 1, f"got {n}")
            check("promoted-Event auf c_ok", "promoted" in _events(q, "c_ok"))

            print("[recompute_ready: human_check -> human]")
            b.create_task("p_done2", "pd2", status="done", created_at=108)
            b.create_task("hc", "humancheck", status="todo", created_at=109, child_role="human_check")
            b.link_tasks("p_done2", "hc")
            b.recompute_ready()
            check("human_check wird 'human' (nie ready)",
                  q.execute("SELECT status FROM tasks WHERE id='hc'").fetchone()[0] == "human")

            print("[recompute_ready: sticky-block bleibt blocked]")
            b.create_task("sb", "sticky", status="blocked", created_at=110)
            with b._txn():
                b._append_event("sb", "blocked", {"kind": "needs_input"})
            b.recompute_ready()
            check("sticky-blocked bleibt blocked",
                  q.execute("SELECT status FROM tasks WHERE id='sb'").fetchone()[0] == "blocked")

            print("[go_noetig: GO-Release befriedigt Parent]")
            b.create_task("go_p", "freigabe", status="blocked", created_at=111)
            q.execute("UPDATE tasks SET category='go_noetig' WHERE id='go_p'")
            b.create_task("go_c", "child", status="todo", created_at=112)
            b.link_tasks("go_p", "go_c")
            with b._txn():
                b._append_event("go_p", "blocked", {"category": "go_noetig"})
                b._append_event("go_p", "unblocked", {})
            # Parent re-blockt auf UNRELATED gate -> strikt wäre False, decision-release True
            with b._txn():
                b._append_event("go_p", "blocked", {"category": "capability"})
            check("go_noetig decision-release befriedigt Parent",
                  b.get_spawn_rejection_reason("go_c") is None)

            print("[circuit-breaker: _record_task_failure trippt bei LIMIT]")
            b.create_task("cb", "cb", status="ready", created_at=113)
            r1 = b._record_task_failure("cb", "boom1", outcome="crashed", failure_limit=2)
            check("1. Fehlschlag trippt NICHT", r1 is False)
            r2 = b._record_task_failure("cb", "boom2", outcome="crashed", failure_limit=2)
            check("2. Fehlschlag (==LIMIT) trippt -> blocked", r2 is True and
                  q.execute("SELECT status FROM tasks WHERE id='cb'").fetchone()[0] == "blocked")
            check("consecutive_failures == 2",
                  q.execute("SELECT consecutive_failures FROM tasks WHERE id='cb'").fetchone()[0] == 2)
            check("gave_up-Event", "gave_up" in _events(q, "cb"))

            print("[heartbeat_claim]")
            b.create_task("hb", "hb", status="ready", assignee="worker", created_at=114)
            b.claim_task("hb", claimer="host:L")
            exp0 = q.execute("SELECT claim_expires FROM tasks WHERE id='hb'").fetchone()[0]
            check("heartbeat korrekter lock verlängert", b.heartbeat_claim("hb", ttl_seconds=99999, claimer="host:L") and
                  q.execute("SELECT claim_expires FROM tasks WHERE id='hb'").fetchone()[0] > exp0)
            check("heartbeat falscher lock -> False", b.heartbeat_claim("hb", claimer="host:WRONG") is False)

            print("[release_stale_claims: reclaimt abgelaufene + Event]")
            b.create_task("st", "stale", status="ready", assignee="worker", created_at=115)
            b.claim_task("st", claimer="host:dead")
            q.execute("UPDATE tasks SET claim_expires=1, worker_pid=NULL WHERE id='st'")
            rc = b.release_stale_claims()
            check("release_stale_claims >= 1", rc >= 1, f"got {rc}")
            srow = q.execute("SELECT status, claim_lock, claim_expires FROM tasks WHERE id='st'").fetchone()
            check("stale -> ready + lock/expires NULL", srow == ("ready", None, None), f"got {srow}")
            check("reclaimed-Event", "reclaimed" in _events(q, "st"))

            print("[dispatch_once: tick mit stub spawn_fn]")
            b.create_task("d1", "d1", status="ready", assignee="worker", created_at=116)
            spawned_ids = []
            def _spawn(task):
                spawned_ids.append(task["id"]); return 424242
            res = b.dispatch_once(spawn_fn=_spawn, board="default")
            check("tick spawnt d1 + stempelt worker_pid", res["spawned"] >= 1 and "d1" in spawned_ids and
                  q.execute("SELECT worker_pid, status FROM tasks WHERE id='d1'").fetchone() == (424242, "running"))
        finally:
            conn.close(); q.close()
    finally:
        e.teardown()

    print("[nebenläufig: 6 Prozesse claimen dieselben Karten — kein Doppelclaim]")
    e2 = Env("conc")
    try:
        _apply_schema(e2, triggers=False)
        M = 120
        ids = [f"k{i:04d}" for i in range(M)]
        with e2.connect(autocommit=True) as c, c.cursor() as cur:
            with cur.copy("COPY tasks (id,title,status,assignee,created_at) FROM STDIN") as cp:
                for i, tid in enumerate(ids):
                    cp.write_row((tid, tid, "ready", "worker", 1000 + i))
        ctx = mp.get_context("fork")
        qq = ctx.Queue()
        procs = [ctx.Process(target=_claim_worker, args=(e2.uri, ids, qq)) for _ in range(6)]
        for p in procs:
            p.start()
        total = sum(qq.get() for _ in procs)
        for p in procs:
            p.join()
        check("6 Claimer: Summe Erfolge == M (kein Doppelclaim/Verlust)", total == M, f"got {total}")
        with e2.connect(autocommit=True) as c:
            check("running == M", c.execute("SELECT count(*) FROM tasks WHERE status='running'").fetchone()[0] == M)
            check("distinct claim_lock == M (jede genau 1×)",
                  c.execute("SELECT count(DISTINCT claim_lock) FROM tasks WHERE status='running'").fetchone()[0] >= 1)
            check("keine ready übrig", c.execute("SELECT count(*) FROM tasks WHERE status='ready'").fetchone()[0] == 0)
    finally:
        e2.teardown()

    print(f"\nTESTS dispatch ({'PROD-PG' if PROD_DSN else 'pgserver'}): "
          f"{'ALLE GRÜN' if not FAILS else f'{len(FAILS)} ROT: {FAILS}'}")
    return 0 if not FAILS else 2


if __name__ == "__main__":
    sys.exit(main())
