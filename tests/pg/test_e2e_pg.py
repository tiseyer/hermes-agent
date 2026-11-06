"""B1.1 Welle-3 VOLLSUITE-Kriterium (Till, HART): die D4/D5-injizierten Grenzen
laufen gegen backend=postgres ECHT mit — NICHT gemockt.

Ein End-to-End-PG-Test, der fährt:
  * echten spawn_fn: startet einen ECHTEN OS-Prozess (subprocess sleep) und
    stempelt dessen realen PID — über die rewired Watchers-Call-Site
    (_pg_dispatch_tick → PgBackend.dispatch_once).
  * echten reclaim: release_stale_claims mit dem DEFAULT pid_alive (os.kill(pid,0))
    und default terminate — ein toter PID wird real erkannt und reclaimt; ein
    LEBENDER PID mit frischem Heartbeat wird real verlängert (nicht reclaimt).
  * echten reviewer_resolver: block_task_full(kind="review", reviewer_resolver=…)
    mit einem echten Callable — die Review-Handoff-Grenze routet real.

KEIN Hermes-Worker, keine Fabrik: der „Worker" ist ein harmloses `sleep`, damit
Spawn/Kill/pid_alive/terminate ECHT laufen, ohne die Fabrik anzuschalten.

    HERMES_KANBAN_PG_DSN=... PYTHONPATH=<repo> python tests/pg/test_e2e_pg.py
"""
import os
import signal
import subprocess
import sys
import time

from _w3env import Check, PgEnv, mode_banner

from kanban_pg import PgBackend  # noqa: E402
from kanban_pg.ops_dispatch import claimer_id  # noqa: E402


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def main():
    print(mode_banner("e2e (echte Grenzen)"))
    check = Check()
    e = PgEnv("e2e")
    kids = []  # echte Subprozesse zum Aufräumen
    procs = {}  # pid -> Popen (zum Reapen, sonst bleibt ein Zombie "alive")

    def real_sleep_pid():
        p = subprocess.Popen(["sleep", "120"])
        kids.append(p)
        procs[p.pid] = p
        return p.pid

    try:
        e.apply_schema(triggers=True)
        e.activate_routing()

        from hermes_cli import kanban_db as kb
        from gateway.kanban_watchers import _pg_dispatch_tick

        q = e.connect(autocommit=True)
        conn = kb.connect()
        b = PgBackend(conn)
        seed = PgBackend(e.connect(autocommit=False))
        try:
            print("[1) ECHTER spawn_fn über die rewired Watchers-Call-Site]")
            seed.create_task("s1", "Spawn-Karte", status="ready", assignee="worker", created_at=400)
            spawned_pid = {}

            def real_spawn(task_dict):
                pid = real_sleep_pid()
                spawned_pid[task_dict["id"]] = pid
                return pid  # echter OS-PID

            res = _pg_dispatch_tick(kb, conn, "default", spawn_fn=real_spawn)
            s1_pid = spawned_pid.get("s1")
            check("Tick spawnt s1 mit echtem OS-Prozess",
                  any(t[0] == "s1" for t in res.spawned) and s1_pid is not None)
            check("s1 running + realer worker_pid gestempelt",
                  q.execute("SELECT status, worker_pid FROM tasks WHERE id='s1'").fetchone()
                  == ("running", s1_pid))
            check("der gestempelte PID lebt WIRKLICH (os.kill 0)", _pid_alive(s1_pid))

            print("[2) ECHTER reclaim: toter PID -> release_stale_claims (default pid_alive/terminate)]")
            # Worker wirklich töten UND reapen (sonst Zombie -> os.kill(pid,0) True).
            procs[s1_pid].terminate()
            procs[s1_pid].wait(timeout=5)
            check("s1-Worker-Prozess ist tot (reaped)", not _pid_alive(s1_pid))
            q.execute("UPDATE tasks SET claim_expires=1 WHERE id='s1'")
            reclaimed = b.release_stale_claims()  # DEFAULT = echtes os.kill(pid,0) + terminate
            check("release_stale_claims reclaimt >=1 (realer toter PID)", reclaimed >= 1, f"got {reclaimed}")
            row = q.execute("SELECT status, claim_lock, worker_pid FROM tasks WHERE id='s1'").fetchone()
            check("s1 -> ready, lock/pid geräumt", row == ("ready", None, None), f"got {row}")
            check("reclaimed-Event angehängt",
                  "reclaimed" in [r[0] for r in q.execute(
                      "SELECT kind FROM task_events WHERE task_id='s1' ORDER BY id").fetchall()])

            print("[3) ECHTER pid_alive: LEBENDER PID + frischer Heartbeat -> verlängert, NICHT reclaimt]")
            seed.create_task("s2", "Lebt-Karte", status="ready", assignee="worker", created_at=401)
            live_pid = real_sleep_pid()
            # host-lokaler Claim (lock beginnt mit claimer_id()-Host-Präfix).
            b.claim_task("s2", claimer=claimer_id())
            now = int(time.time())
            q.execute(
                "UPDATE tasks SET worker_pid=%s, claim_expires=1, last_heartbeat_at=%s WHERE id='s2'",
                (live_pid, now),
            )
            before_exp = q.execute("SELECT claim_expires FROM tasks WHERE id='s2'").fetchone()[0]
            rc2 = b.release_stale_claims()  # echtes pid_alive(live_pid) == True
            row2 = q.execute("SELECT status, claim_expires FROM tasks WHERE id='s2'").fetchone()
            kinds2 = [r[0] for r in q.execute(
                "SELECT kind FROM task_events WHERE task_id='s2' ORDER BY id").fetchall()]
            check("lebender Worker NICHT reclaimt (bleibt running)", row2[0] == "running", f"got {row2}")
            check("Claim wurde real verlängert (claim_expires vorgerückt)",
                  row2[1] is not None and row2[1] > before_exp, f"before={before_exp} after={row2[1]}")
            check("claim_extended-Event (Grund pid_alive)", "claim_extended" in kinds2)

            print("[4) ECHTER reviewer_resolver: Review-Handoff-Grenze (injiziertes Callable)]")
            seed.create_task("s3", "Review-Karte", status="running", assignee="voicera-coder",
                             created_at=402)
            seed.conn.execute("UPDATE tasks SET tenant=%s WHERE id=%s", ("voicera", "s3"))
            seed.conn.commit()
            resolver_calls = []

            def real_reviewer_resolver(task_id, tenant):
                resolver_calls.append((task_id, tenant))
                return "voicera-reviewer"  # echte Routing-Entscheidung

            ok = b.block_task_full("s3", reason="bitte review", kind="review",
                                   reviewer_resolver=real_reviewer_resolver)
            row3 = q.execute("SELECT status, assignee FROM tasks WHERE id='s3'").fetchone()
            check("reviewer_resolver wurde mit (task_id, tenant) gerufen",
                  resolver_calls == [("s3", "voicera")], f"got {resolver_calls}")
            check("s3 in Review-Spalte an den aufgelösten Reviewer geroutet",
                  ok is True and row3 == ("review", "voicera-reviewer"), f"got {row3}")
        finally:
            conn.close()
            seed.conn.close()
            q.close()
    finally:
        for p in kids:
            try:
                p.kill()
                p.wait(timeout=5)
            except Exception:
                pass
        e.teardown()

    print(f"\nTESTS e2e: {'ALLE GRÜN' if not check.fails else f'{len(check.fails)} ROT: {check.fails}'}")
    return 0 if not check.fails else 2


if __name__ == "__main__":
    sys.exit(main())
