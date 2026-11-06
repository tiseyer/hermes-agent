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

    print(f"\nTESTS watchers: {'ALLE GRÜN' if not check.fails else f'{len(check.fails)} ROT: {check.fails}'}")
    return 0 if not check.fails else 2


if __name__ == "__main__":
    sys.exit(main())
