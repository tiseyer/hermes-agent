"""B1.1 Welle-3 (watchers) — Dispatcher-Tick + Notifier gegen PG.

Karte b1.1-port-watchers. Muss-Fälle: ein Dispatch-Tick promotet/claimt gegen PG
OHNE Datei-flock; Notifier schiebt den Cursor über das Backend; kein Doppel-
Delivery bei zwei Ticks/Claims; backend=sqlite unverändert. Keine echten Worker:
ein Stub-spawn_fn wird in den (modulweit extrahierten) PG-Tick injiziert.

    HERMES_KANBAN_PG_DSN=... PYTHONPATH=<repo> python tests/pg/test_watchers.py
"""
import os
import sys
import tempfile

from _w3env import Check, PgEnv, mode_banner

from kanban_pg import PgBackend  # noqa: E402


def _scalar(env, sql):
    with env.connect(autocommit=True) as c:
        return c.execute(sql).fetchone()[0]


def main():
    print(mode_banner("watchers"))
    check = Check()
    e = PgEnv("watch")
    # Lock-Pfad test-lokal pinnen: im PG-Modus routet connect() ohnehin nach PG
    # (backend=postgres gewinnt); HERMES_KANBAN_DB lenkt nur kanban_db_path, damit
    # die flock-Abwesenheit gegen einen frischen Pfad prüfbar ist (nicht gegen die
    # bereits existierende Prod-<db>.dispatch.lock).
    _tmpdb = os.path.join(tempfile.mkdtemp(prefix="w3watch-"), "kanban.db")
    os.environ["HERMES_KANBAN_DB"] = _tmpdb
    try:
        e.apply_schema(triggers=True)
        e.activate_routing()

        from hermes_cli import kanban_db as kb
        from gateway.kanban_watchers import _pg_dispatch_tick

        seed = PgBackend(e.connect(autocommit=False))
        # Promote-Kette: done-Parent + assignee-loses Kind (wird ready, nicht gespawnt)
        seed.create_task("wp", "Parent done", status="done", created_at=300)
        seed.create_task("wc", "Kind", status="todo", created_at=301)
        seed.link_tasks("wp", "wc")
        # spawnbare Karte: ready + assignee
        seed.create_task("r1", "Spawnbar", status="ready", assignee="worker", created_at=302)

        conn = kb.connect()
        try:
            print("[list_boards = EIN Store (Tick enumeriert Boards)]")
            boards = kb.list_boards(include_archived=False)
            check("genau 1 Board 'default'", len(boards) == 1 and boards[0]["slug"] == "default")

            print("[Dispatch-Tick: promotet + claimt + spawnt (Stub) ohne Datei-flock]")
            spawned_calls = []

            def stub_spawn(task_dict):
                spawned_calls.append(task_dict["id"])
                return 999  # fake pid; kein echter Worker

            res = _pg_dispatch_tick(kb, conn, "default", max_spawn=None,
                                    failure_limit=3, spawn_fn=stub_spawn)

            check("DispatchResult-Form (spawned list / reclaimed int)",
                  isinstance(res.spawned, list) and isinstance(res.reclaimed, int))
            check("promoted >= 1 (wc done-Parent -> ready)", res.promoted >= 1, f"promoted={res.promoted}")
            check("wc ist jetzt ready", _scalar(e, "SELECT status FROM tasks WHERE id='wc'") == "ready")
            check("r1 gespawnt (im spawned-Set)", any(t[0] == "r1" for t in res.spawned)
                  and "r1" in spawned_calls, f"spawned={res.spawned}")
            check("r1 running + worker_pid gestempelt",
                  _scalar(e, "SELECT status FROM tasks WHERE id='r1'") == "running"
                  and _scalar(e, "SELECT worker_pid FROM tasks WHERE id='r1'") == 999)

            print("[kein Datei-flock im PG-Modus]")
            lock_path = kb.kanban_db_path(board="default").with_name("kanban.db.dispatch.lock")
            check("keine <db>.dispatch.lock angelegt (test-lokaler Pfad)",
                  not lock_path.exists(), f"{lock_path}")

            print("[zweiter Tick: kein Doppel-Spawn der laufenden Karte]")
            spawned_calls.clear()
            res2 = _pg_dispatch_tick(kb, conn, "default", max_spawn=None,
                                     failure_limit=3, spawn_fn=stub_spawn)
            check("r1 nicht erneut gespawnt (läuft bereits)",
                  "r1" not in spawned_calls and not any(t[0] == "r1" for t in res2.spawned))

            print("[Notifier: Cursor-Vorschub über Backend, kein Doppel-Delivery]")
            seed.create_task("nt", "Notify-Karte", status="ready", assignee="worker", created_at=303)
            with seed._txn():
                seed._append_event("nt", "progress", {"n": 1})
                seed._append_event("nt", "progress", {"n": 2})
            kb.add_notify_sub(conn, task_id="nt", platform="telegram",
                              chat_id="c1", thread_id="")
            old1, new1, evs1 = kb.claim_unseen_events_for_sub(
                conn, task_id="nt", platform="telegram", chat_id="c1", thread_id="")
            check("erster Claim liefert Events + schiebt Cursor",
                  len(evs1) >= 2 and new1 > old1, f"old={old1} new={new1} n={len(evs1)}")
            old2, new2, evs2 = kb.claim_unseen_events_for_sub(
                conn, task_id="nt", platform="telegram", chat_id="c1", thread_id="")
            check("zweiter Claim: keine Doppel-Delivery (0 Events, Cursor stabil)",
                  len(evs2) == 0 and new2 == new1, f"old={old2} new={new2} n={len(evs2)}")
        finally:
            conn.close()
            seed.conn.close()
    finally:
        os.environ.pop("HERMES_KANBAN_DB", None)
        e.teardown()

    # ---- W4-B PG-Tick-Policy: max_in_progress / per_profile / stale-timeout ----
    e2 = PgEnv("watch2")
    os.environ["HERMES_KANBAN_DB"] = os.path.join(tempfile.mkdtemp(prefix="w3watch2-"), "kanban.db")
    try:
        e2.apply_schema(triggers=True)
        e2.activate_routing()
        from hermes_cli import kanban_db as kb  # noqa: F811
        from gateway.kanban_watchers import _pg_dispatch_tick  # noqa: F811
        sb = PgBackend(e2.connect(autocommit=False))
        conn2 = kb.connect()

        def _count_running():
            with e2.connect(autocommit=True) as c:
                return c.execute("SELECT count(*) FROM tasks WHERE status='running'").fetchone()[0]

        def noop_spawn(task_dict):
            return 123

        try:
            print("[W4-B max_in_progress: globaler Deckel]")
            for i in range(3):
                sb.create_task(f"mi{i}", f"mi{i}", status="ready", assignee="worker", created_at=600 + i)
            r = _pg_dispatch_tick(kb, conn2, "default", max_in_progress=1, spawn_fn=noop_spawn)
            check("max_in_progress=1 -> genau 1 gespawnt", len(r.spawned) == 1 and _count_running() == 1,
                  f"spawned={len(r.spawned)} running={_count_running()}")

            print("[W4-B max_in_progress_per_profile: pro assignee]")
            e3 = PgEnv("watch3")
            try:
                e3.apply_schema(triggers=True)
                os.environ["HERMES_KANBAN_PG_DSN"] = e3.uri  # Routing auf e3 umhängen
                sb3 = PgBackend(e3.connect(autocommit=False))
                conn3 = kb.connect()
                sb3.create_task("pa", "pa", status="ready", assignee="alpha", created_at=610)
                sb3.create_task("pb", "pb", status="ready", assignee="alpha", created_at=611)
                sb3.create_task("pc", "pc", status="ready", assignee="beta", created_at=612)
                r3 = _pg_dispatch_tick(kb, conn3, "default",
                                       max_in_progress_per_profile=1, spawn_fn=noop_spawn)
                spawned_ids = {t[0] for t in r3.spawned}
                check("per_profile=1: alpha nur 1x, beta 1x (2 gesamt)",
                      len(spawned_ids) == 2 and ("pc" in spawned_ids)
                      and len(spawned_ids & {"pa", "pb"}) == 1,
                      f"spawned={spawned_ids}")
                conn3.close()
            finally:
                os.environ["HERMES_KANBAN_PG_DSN"] = e2.uri  # zurück auf e2
                e3.teardown()

            print("[W4-B stale-timeout: Heartbeat-Stale -> reclaim]")
            sb.create_task("st1", "st1", status="ready", assignee="worker", created_at=620)
            sb.claim_task("st1", claimer="hermes:x")
            with e2.connect(autocommit=True) as c:
                c.execute("UPDATE tasks SET last_heartbeat_at=%s WHERE id='st1'", (1,))  # uralt
            r4 = _pg_dispatch_tick(kb, conn2, "default", stale_timeout_seconds=60, spawn_fn=noop_spawn)
            with e2.connect(autocommit=True) as c:
                st1_status = c.execute("SELECT status FROM tasks WHERE id='st1'").fetchone()[0]
            check("stale-timeout reclaimt st1 (running -> ready/respawnt)",
                  r4.reclaimed >= 1, f"reclaimed={r4.reclaimed}")
            check("reclaimed-Event (heartbeat_stale) auf st1",
                  "reclaimed" in [x[0] for x in e2.connect(autocommit=True).execute(
                      "SELECT kind FROM task_events WHERE task_id='st1' ORDER BY id").fetchall()])
            _ = st1_status
        finally:
            conn2.close()
            sb.conn.close()
    finally:
        os.environ.pop("HERMES_KANBAN_DB", None)
        e2.teardown()

    print(f"\nTESTS watchers: {'ALLE GRÜN' if not check.fails else f'{len(check.fails)} ROT: {check.fails}'}")
    return 0 if not check.fails else 2


if __name__ == "__main__":
    sys.exit(main())
