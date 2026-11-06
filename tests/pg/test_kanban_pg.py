"""B1.1 Etappe-1/2-Tests (kanban_pg + Switch). Dual-mode:

  * Default: ephemere pgserver-DB (CI/Dry-Run, Health-Fallback-Pfad).
  * Wenn ``HERMES_KANBAN_PG_DSN`` gesetzt: gegen das ECHTE Prod-PG, aber in einem
    Wegwerf-SCHEMA (public bleibt bis Cutover unberührt). Dort läuft der amcheck-
    Health-Pfad echt.

Ausführung:
    PYTHONPATH=<repo> python tests/pg/test_kanban_pg.py              # pgserver
    HERMES_KANBAN_PG_DSN=... PYTHONPATH=<repo> python …              # echtes PG
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
PROD_DSN = os.environ.get("HERMES_KANBAN_PG_DSN", "").strip() or None
FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class Env:
    """Isolierte DB-Umgebung: Prod-Schema (wenn DSN) sonst pgserver."""
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
            # search_path = Wegwerf-Schema + public (public für amcheck-Funktionen/Extension)
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


def suite_switch():
    print("[switch]")
    os.environ.pop("HERMES_KANBAN_BACKEND", None)
    check("Default = sqlite", kanban_backend.resolve_backend_name() == "sqlite")
    check("config postgres", kanban_backend.resolve_backend_name({"kanban": {"backend": "postgres"}}) == "postgres")
    os.environ["HERMES_KANBAN_BACKEND"] = "postgres"
    check("Env override", kanban_backend.resolve_backend_name({"kanban": {"backend": "sqlite"}}) == "postgres")
    os.environ.pop("HERMES_KANBAN_BACKEND", None)
    try:
        kanban_backend.resolve_backend_name({"kanban": {"backend": "mysql"}}); check("ungültig->ValueError", False)
    except ValueError:
        check("ungültig->ValueError", True)


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
    print(f"=== Modus: {'ECHTES PG (Wegwerf-Schema)' if PROD_DSN else 'pgserver'} ===")
    suite_switch()

    e1 = Env("core")
    try:
        _apply_schema(e1, triggers=True)
        conn = e1.connect(autocommit=False)
        q = e1.connect(autocommit=True)
        try:
            b = PgBackend(conn)
            print("[triggers+backend]")
            b.create_task("solo", "solo", status="todo", created_at=500)
            check("stamp_insert scat=created_at", q.execute("SELECT status_changed_at FROM tasks WHERE id='solo'").fetchone()[0] == 500)
            b.create_task("R", "root", status="todo", created_at=1000, family_root_id="R")
            b.create_task("c1", "c1", status="ready", assignee="worker", created_at=1001, family_root_id="R")
            b.create_task("c2", "c2", status="ready", assignee="worker", created_at=1002, family_root_id="R")
            check("rollup: Kinder ready -> Wurzel ready", q.execute("SELECT status FROM tasks WHERE id='R'").fetchone()[0] == "ready")
            b.add_comment("R", "charlie", "k"); b.add_event("R", "commented")
            rid = b.start_run("c1", "worker"); b.end_run(rid, "done", "completed")
            check("run lifecycle", q.execute("SELECT outcome FROM task_runs WHERE id=%s", (rid,)).fetchone()[0] == "completed")

            print("[etappe2: claim-lifecycle]")
            tid = b.claim_ready("worker", "lock-A", worker_pid=4242, ttl_seconds=600)
            check("claim_ready -> running + claim_expires gesetzt", tid in ("c1", "c2") and
                  q.execute("SELECT claim_expires FROM tasks WHERE id=%s", (tid,)).fetchone()[0] is not None)
            exp0 = q.execute("SELECT claim_expires FROM tasks WHERE id=%s", (tid,)).fetchone()[0]
            check("heartbeat verlängert claim_expires", b.heartbeat(tid, "lock-A", ttl_seconds=9999) and
                  q.execute("SELECT claim_expires FROM tasks WHERE id=%s", (tid,)).fetchone()[0] > exp0)
            check("heartbeat mit falschem lock -> False", b.heartbeat(tid, "lock-WRONG") is False)
            # reclaim_stale: Claim künstlich ablaufen lassen
            q.execute("UPDATE tasks SET claim_expires=1 WHERE id=%s", (tid,))
            reclaimed = b.reclaim_stale(now=100)
            check("reclaim_stale gibt abgelaufenen Claim zurück", tid in reclaimed, f"got {reclaimed}")
            check("reclaimte Karte wieder ready + claim_lock NULL",
                  q.execute("SELECT status, claim_lock FROM tasks WHERE id=%s", (tid,)).fetchone() == ("ready", None))

            print("[circuit-breaker]")
            b.create_task("f1", "f", status="ready", created_at=1003)
            r = None
            for _ in range(3):
                r = b.record_failure("f1", "boom", limit=3)
            check("record_failure trips -> blocked", r[1] == "blocked", f"got {r}")
            b.complete_task("f1")
            check("complete resetet consecutive_failures", q.execute("SELECT consecutive_failures FROM tasks WHERE id='f1'").fetchone()[0] == 0)

            print("[etappe4: notifier-cursor]")
            b.notify_subscribe("solo", "telegram", "chat1", user_id="u1")
            b.add_event("solo", "n1"); b.add_event("solo", "n2")
            evs = b.claim_unseen_events("solo", "telegram", "chat1")
            check("claim_unseen liefert neue Events", len(evs) == 2, f"got {len(evs)}")
            check("claim_unseen 2. Mal leer (Cursor vorgeschoben)", b.claim_unseen_events("solo", "telegram", "chat1") == [])
            b.add_event("solo", "n3")
            check("nach neuem Event wieder 1", len(b.claim_unseen_events("solo", "telegram", "chat1")) == 1)
            check("unsubscribe entfernt Sub", b.notify_unsubscribe("solo", "telegram", "chat1") is True)
            check("claim nach unsubscribe leer", b.claim_unseen_events("solo", "telegram", "chat1") == [])

            print("[etappe5: auto-heartbeat / tools]")
            b.create_task("hb1", "hb", status="running", created_at=1004)
            b.bump_heartbeat("hb1")
            check("bump_heartbeat setzt last_heartbeat_at", q.execute("SELECT last_heartbeat_at FROM tasks WHERE id='hb1'").fetchone()[0] is not None)
            b.block_task("hb1", kind="needs_input")
            check("block_task -> blocked + block_kind", q.execute("SELECT status, block_kind FROM tasks WHERE id='hb1'").fetchone() == ("blocked", "needs_input"))

            print("[etappe6: dashboard-reads]")
            t = b.get_task("R")
            check("get_task liefert dict", isinstance(t, dict) and t["id"] == "R")
            check("get_task None bei unbekannt", b.get_task("nope") is None)
            check("list_tasks(status=done) enthält f1", any(x["id"] == "f1" for x in b.list_tasks(status="done")))
            check("list_comments(R) >= 1", len(b.list_comments("R")) >= 1)
            check("list_events(R) >= 1", len(b.list_events("R")) >= 1)
            st = b.stats_by_status()
            check("stats_by_status summiert", sum(st.values()) == q.execute("SELECT count(*) FROM tasks").fetchone()[0])

            print("[health]")
            with e1.connect(autocommit=True) as hc:
                h = health.check_health(hc)
                check("health ok", h["ok"] is True, f"got {h}")
                mode = h["checks"]["integrity"]["mode"]
                check("health-mode", mode in ("amcheck", "fallback"))
                if mode == "amcheck":
                    check("amcheck: Indizes geprüft > 0", h["checks"]["integrity"]["indexes_checked"] > 0,
                          f"got {h['checks']['integrity']}")
                print(f"    (health-mode: {mode})")
        finally:
            conn.close(); q.close()
    finally:
        e1.teardown()

    print("[locks: 8 Claimer nebenläufig]")
    e2 = Env("locks")
    try:
        _apply_schema(e2, triggers=False)
        with e2.connect(autocommit=True) as c, c.cursor() as cur:
            with cur.copy("COPY tasks (id,title,status,assignee,created_at) FROM STDIN") as cp:
                for i in range(200):
                    cp.write_row((f"t{i:04d}", f"t{i}", "ready", "worker", 1000 + i))
        ctx = mp.get_context("fork")
        qq = ctx.Queue()
        procs = [ctx.Process(target=_worker, args=(e2.uri, t, qq)) for t in range(8)]
        for p in procs:
            p.start()
        total = sum(qq.get() for _ in procs)
        for p in procs:
            p.join()
        check("8 Claimer: Summe == 200 (kein Doppelclaim/Verlust)", total == 200, f"got {total}")
        with e2.connect(autocommit=True) as c:
            check("running == 200", c.execute("SELECT count(*) FROM tasks WHERE status='running'").fetchone()[0] == 200)
            check("distinct running-ids == 200", c.execute("SELECT count(DISTINCT id) FROM tasks WHERE status='running'").fetchone()[0] == 200)
        c1 = e2.connect(); c2 = e2.connect()
        try:
            a1 = try_board_lock(c1, "default"); a2 = try_board_lock(c2, "default")
            check("advisory: Ausschluss", a1 is True and a2 is False)
            c1.commit()
            check("advisory: Auto-Release", try_board_lock(c2, "default") is True)
            c2.commit()
        finally:
            c1.close(); c2.close()
    finally:
        e2.teardown()

    print(f"\nTESTS ({'PROD-PG' if PROD_DSN else 'pgserver'}): {'ALLE GRÜN' if not FAILS else f'{len(FAILS)} ROT: {FAILS}'}")
    return 0 if not FAILS else 2


if __name__ == "__main__":
    sys.exit(main())
