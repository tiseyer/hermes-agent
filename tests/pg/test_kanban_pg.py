"""B1.1 Etappe-1-Tests (kanban_pg + Switch). Läuft gegen eine ephemere pgserver-DB.

Ausführung (braucht psycopg + pgserver, z. B. Dry-Run-venv):
    PYTHONPATH=<repo> python tests/pg/test_kanban_pg.py

In CI wird das später (P7) gegen eine ephemere PG-Instanz gehängt. amcheck fehlt
im pgserver-Bundle -> Health läuft hier über den Fallback-Pfad (amcheck-Pfad wird
gegen das Prod-PG 16 verifiziert, das die Extension hat).
"""
import os
import pathlib
import shutil
import sys
import tempfile
import multiprocessing as mp

import psycopg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from kanban_pg import PgBackend, health  # noqa: E402
from kanban_pg.lock_model import try_board_lock, claim_one_ready  # noqa: E402
import kanban_backend  # noqa: E402

PKG = pathlib.Path(__file__).resolve().parents[2] / "kanban_pg"
FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def _server():
    xdg = pathlib.Path(tempfile.mkdtemp(prefix="xdg-"))
    os.environ.setdefault("XDG_RUNTIME_DIR", str(xdg))
    import pgserver
    d = pathlib.Path(tempfile.mkdtemp(prefix="pgdata-"))
    return pgserver.get_server(d), d


def _schema(conn, triggers=True):
    conn.execute((PKG / "schema.sql").read_text())
    if triggers:
        conn.execute((PKG / "triggers.sql").read_text())


# ---------------------------------------------------------------- switch (kein PG)
def suite_switch():
    print("[switch]")
    os.environ.pop("HERMES_KANBAN_BACKEND", None)
    check("Default = sqlite", kanban_backend.resolve_backend_name() == "sqlite")
    check("config postgres", kanban_backend.resolve_backend_name({"kanban": {"backend": "postgres"}}) == "postgres")
    check("is_postgres default False", kanban_backend.is_postgres() is False)
    os.environ["HERMES_KANBAN_BACKEND"] = "postgres"
    check("Env override", kanban_backend.resolve_backend_name({"kanban": {"backend": "sqlite"}}) == "postgres")
    os.environ.pop("HERMES_KANBAN_BACKEND", None)
    try:
        kanban_backend.resolve_backend_name({"kanban": {"backend": "mysql"}})
        check("ungültig -> ValueError", False)
    except ValueError:
        check("ungültig -> ValueError", True)


def _worker(uri, tag, q):
    conn = psycopg.connect(uri, autocommit=False)
    n = 0
    try:
        while True:
            tid = claim_one_ready(conn, "worker", f"lock-{tag}", 1000, tag)
            conn.commit()
            if tid is None:
                break
            n += 1
    finally:
        conn.close()
        q.put(n)


def main():
    suite_switch()
    srv, d = _server()
    try:
        uri = srv.get_uri()

        # ---- triggers + backend (eine DB) ----
        print("[triggers+backend]")
        with psycopg.connect(uri, autocommit=True) as c0, c0.cursor() as cur:
            _schema(cur, triggers=True)
        conn = psycopg.connect(uri, autocommit=False)
        q = psycopg.connect(uri, autocommit=True)
        try:
            b = PgBackend(conn)
            b.create_task("solo", "solo", status="todo", created_at=500)
            check("stamp_insert scat=created_at", q.execute("SELECT status_changed_at FROM tasks WHERE id='solo'").fetchone()[0] == 500)
            b.create_task("R", "root", status="todo", created_at=1000, family_root_id="R")
            b.create_task("c1", "c1", status="ready", assignee="worker", created_at=1001, family_root_id="R")
            b.create_task("c2", "c2", status="ready", assignee="worker", created_at=1002, family_root_id="R")
            check("rollup: Kinder ready -> Wurzel ready", q.execute("SELECT status FROM tasks WHERE id='R'").fetchone()[0] == "ready")
            b.add_comment("R", "charlie", "k")
            b.add_event("R", "commented")
            rid = b.start_run("c1", "worker"); b.end_run(rid, "done", "completed")
            check("run lifecycle", q.execute("SELECT outcome FROM task_runs WHERE id=%s", (rid,)).fetchone()[0] == "completed")
            # circuit breaker
            b.create_task("f1", "f", status="ready", created_at=1003)
            r = None
            for _ in range(3):
                r = b.record_failure("f1", "boom", limit=3)
            check("record_failure trips -> blocked", r[1] == "blocked", f"got {r}")
            b.complete_task("f1")
            check("complete resetet consecutive_failures", q.execute("SELECT consecutive_failures FROM tasks WHERE id='f1'").fetchone()[0] == 0)
            b.complete_task("c1"); b.complete_task("c2")
            rr = q.execute("SELECT status, completed_at FROM tasks WHERE id='R'").fetchone()
            check("rollup: alle done -> Wurzel done + completed_at", rr[0] == "done" and rr[1] and rr[1] > 0)
        finally:
            conn.close(); q.close()

        # ---- health (Fallback-Pfad auf pgserver) ----
        print("[health]")
        with psycopg.connect(uri, autocommit=True) as hc:
            h = health.check_health(hc)
            check("health ok auf sauberer DB", h["ok"] is True, f"got {h}")
            check("health-mode gesetzt", h["checks"]["integrity"]["mode"] in ("amcheck", "fallback"))

        # ---- locks (frische DB) ----
        print("[locks]")
        srv2, d2 = _server()
        try:
            uri2 = srv2.get_uri()
            with psycopg.connect(uri2, autocommit=True) as c0, c0.cursor() as cur:
                _schema(cur, triggers=False)
                with cur.copy("COPY tasks (id,title,status,assignee,created_at) FROM STDIN") as cp:
                    for i in range(200):
                        cp.write_row((f"t{i:04d}", f"t{i}", "ready", "worker", 1000 + i))
            ctx = mp.get_context("fork")
            qq = ctx.Queue()
            procs = [ctx.Process(target=_worker, args=(uri2, t, qq)) for t in range(8)]
            for p in procs:
                p.start()
            total = sum(qq.get() for _ in procs)
            for p in procs:
                p.join()
            check("8 Claimer: Summe == 200 (kein Doppelclaim/Verlust)", total == 200, f"got {total}")
            with psycopg.connect(uri2, autocommit=True) as c:
                check("running == 200", c.execute("SELECT count(*) FROM tasks WHERE status='running'").fetchone()[0] == 200)
                check("distinct running-ids == 200", c.execute("SELECT count(DISTINCT id) FROM tasks WHERE status='running'").fetchone()[0] == 200)
                check("ready == 0", c.execute("SELECT count(*) FROM tasks WHERE status='ready'").fetchone()[0] == 0)
            c1 = psycopg.connect(uri2, autocommit=False)
            c2 = psycopg.connect(uri2, autocommit=False)
            try:
                a1 = try_board_lock(c1, "default"); a2 = try_board_lock(c2, "default")
                check("advisory: Ausschluss (1=True,2=False)", a1 is True and a2 is False)
                c1.commit()
                check("advisory: Auto-Release nach commit", try_board_lock(c2, "default") is True)
                c2.commit()
            finally:
                c1.close(); c2.close()
        finally:
            srv2.cleanup(); shutil.rmtree(d2, ignore_errors=True)
    finally:
        srv.cleanup(); shutil.rmtree(d, ignore_errors=True)

    print(f"\nETAPPE-1-TESTS: {'ALLE GRÜN' if not FAILS else f'{len(FAILS)} ROT: {FAILS}'}")
    return 0 if not FAILS else 2


if __name__ == "__main__":
    sys.exit(main())
