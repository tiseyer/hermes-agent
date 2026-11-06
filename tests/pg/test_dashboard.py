"""B1.1 Welle-3 (dashboard) — Consumer-Routing gegen PG.

Karte b1.1-port-dashboard. Muss-Fälle: Board-Liste/Task-Detail/Comments/Events/
Stats liefern korrekt gegen PG; UI-Kommentar via add_comment (+ commented-Event);
UI-Assignee-Änderung via assign_task; Status-Trigger (status_changed_at); Reader
parallel zu offenem Writer blockiert nicht (MVCC); backend=sqlite unverändert.

    HERMES_KANBAN_PG_DSN=... PYTHONPATH=<repo> python tests/pg/test_dashboard.py
"""
import sys

from _w3env import Check, PgEnv, mode_banner

from kanban_pg import PgBackend  # noqa: E402


def main():
    print(mode_banner("dashboard"))
    check = Check()
    e = PgEnv("dash")
    try:
        e.apply_schema(triggers=True)
        e.activate_routing()

        from hermes_cli import kanban_db as kb

        # Seed über das Backend (strukturell, committet), dann über die
        # Routing-Schicht lesen/schreiben — exakt der Dashboard-Pfad.
        seed = PgBackend(e.connect(autocommit=False))
        seed.create_task("t1", "Erste Karte", status="todo", assignee="worker", created_at=100)
        seed.create_task("t2", "Zweite Karte", status="done", assignee="worker", created_at=101)
        seed.create_task("t3", "Archiviert", status="archived", assignee="worker", created_at=102)

        conn = kb.connect()
        try:
            print("[connect-Routing]")
            check("connect() liefert psycopg (nicht sqlite3)",
                  conn.__class__.__module__.startswith("psycopg"))

            print("[write_serialize_lock = No-op im PG-Modus]")
            with kb.write_serialize_lock(kb.kanban_db_path(board="default")) as held:
                check("lock-CM yieldet (kein Datei-flock)", held is True)

            print("[list_boards = EIN Store]")
            boards = kb.list_boards(include_archived=False)
            check("genau 1 Board 'default'",
                  len(boards) == 1 and boards[0].get("slug") == "default",
                  f"got {boards}")

            print("[Task-Detail: get_task liefert Task-Objekt]")
            t = kb.get_task(conn, "t1")
            check("get_task -> Task mit id/title",
                  isinstance(t, kb.Task) and t.id == "t1" and t.title == "Erste Karte")
            check("get_task unbekannt -> None", kb.get_task(conn, "nope") is None)

            print("[list_tasks: Filter + Archiv-Ausschluss + Task-Typ]")
            todo = kb.list_tasks(conn, status="todo")
            check("list_tasks(status=todo) -> [Task] enthält t1",
                  todo and all(isinstance(x, kb.Task) for x in todo)
                  and any(x.id == "t1" for x in todo))
            allt = kb.list_tasks(conn)
            ids = {x.id for x in allt}
            check("default schließt archived aus (t3 fehlt)", "t3" not in ids and "t1" in ids)
            witharch = {x.id for x in kb.list_tasks(conn, include_archived=True)}
            check("include_archived=True zeigt t3", "t3" in witharch)
            check("ungültiger status -> ValueError", _raises(lambda: kb.list_tasks(conn, status="bogus")))

            print("[UI-Kommentar via add_comment -> Insert + commented-Event]")
            cid = kb.add_comment(conn, "t1", author="dash-user", body="  hallo  ")
            check("add_comment liefert id", isinstance(cid, int) and cid > 0)
            comments = kb.list_comments(conn, "t1")
            check("list_comments -> [Comment] mit gestripptem body",
                  len(comments) == 1 and comments[0].body == "hallo"
                  and comments[0].task_id == "t1" and comments[0].author == "dash-user")
            evkinds = [ev.kind for ev in kb.list_events(conn, "t1")]
            check("commented-Event angehängt", "commented" in evkinds)
            check("add_comment unbekannte Karte -> ValueError",
                  _raises(lambda: kb.add_comment(conn, "nope", author="x", body="y")))
            check("add_comment leerer body -> ValueError",
                  _raises(lambda: kb.add_comment(conn, "t1", author="x", body="  ")))

            print("[UI-Assignee-Änderung via assign_task]")
            ok = kb.assign_task(conn, "t1", "reviewer")
            check("assign_task True", ok is True)
            check("Assignee in get_task aktualisiert", kb.get_task(conn, "t1").assignee == "reviewer")

            print("[Status-Trigger: status_changed_at wird gestempelt]")
            # Seed-Backend-Statuswechsel (thin set_status) -> Trigger feuert.
            before = _scalar(e, "SELECT status_changed_at FROM tasks WHERE id='t1'")
            seed.set_status("t1", "ready")
            after = _scalar(e, "SELECT status_changed_at FROM tasks WHERE id='t1'")
            check("status_changed_at gesetzt/erhöht nach Statuswechsel",
                  after is not None and (before is None or after >= before),
                  f"before={before} after={after}")

            print("[Stats: stats_by_status summiert über den Store]")
            st = seed.stats_by_status()
            total = _scalar(e, "SELECT count(*) FROM tasks")
            check("stats summieren == Gesamtzahl", sum(st.values()) == total, f"{st} vs {total}")

            print("[MVCC: Reader blockiert nicht an offenem Writer]")
            writer = e.connect(autocommit=False)
            try:
                writer.execute("UPDATE tasks SET title='GEÄNDERT' WHERE id='t2'")  # uncommitted
                # Reader auf eigener Verbindung + kurzem statement_timeout:
                reader = kb.connect()
                try:
                    reader.execute("SET statement_timeout = 3000")
                    rt = kb.get_task(reader, "t2")
                    check("Reader liefert committeten Snapshot ohne Block",
                          rt is not None and rt.title == "Zweite Karte",
                          f"got title={rt.title if rt else None}")
                finally:
                    reader.close()
                writer.rollback()
            finally:
                writer.close()
        finally:
            conn.close()
            seed.conn.close()
    finally:
        e.teardown()

    print(f"\nTESTS dashboard: {'ALLE GRÜN' if not check.fails else f'{len(check.fails)} ROT: {check.fails}'}")
    return 0 if not check.fails else 2


def _raises(fn):
    try:
        fn()
        return False
    except Exception:
        return True


def _scalar(env, sql):
    with env.connect(autocommit=True) as c:
        return c.execute(sql).fetchone()[0]


if __name__ == "__main__":
    sys.exit(main())
