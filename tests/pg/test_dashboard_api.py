"""B1.1 Welle-4c — Dashboard-REST (plugins/kanban/dashboard/plugin_api.py) gegen PG.

Deckt die 12 Endpunkte/Helfer ab, deren Inline-`?`-SQL auf das kanban_pg.DashboardMixin
umgestellt wurde: _compute_task_diagnostics, _links_for, get_board-Aggregate, get_task-
Deps, update_task/_set_status_direct/bulk_update (Writer), _parents_blocking_ready,
list_diagnostics, list_active_workers, _board_counts, _fetch_new (events_after).

    HERMES_KANBAN_PG_DSN=... PYTHONPATH=<repo>:<repo>/tests/pg python tests/pg/test_dashboard_api.py
"""
import os
import sys
import tempfile

from _w3env import Check, PgEnv, mode_banner

from kanban_pg import PgBackend  # noqa: E402


def main():
    print(mode_banner("dashboard-api"))
    check = Check()
    e = PgEnv("dashapi")
    # Board-Pfad test-lokal (plugin_api löst teils kanban_db_path auf; PG routet eh).
    os.environ["HERMES_KANBAN_DB"] = os.path.join(tempfile.mkdtemp(prefix="w4c-"), "kanban.db")
    try:
        e.apply_schema(triggers=True)
        e.activate_routing()

        from hermes_cli import kanban_db as kb
        import plugins.kanban.dashboard.plugin_api as api

        seed = PgBackend(e.connect(autocommit=False))
        # Initiative + Mitglieder
        seed.create_task("ini", "Initiative", status="running", created_at=700)
        for mid, st in [("m1", "done"), ("m2", "running"), ("m3", "todo")]:
            seed.create_task(mid, f"M {mid}", status=st, assignee="worker", created_at=701)
            seed.conn.execute("UPDATE tasks SET initiative_id='ini' WHERE id=%s", (mid,))
        seed.conn.execute("UPDATE tasks SET initiative_id='ini' WHERE id='ini'")
        # done-Parent + Kind (für Parent-Gate + progress + links)
        seed.create_task("p", "Parent", status="done", created_at=702)
        seed.create_task("c", "Kind", status="todo", assignee="worker", created_at=703)
        seed.link_tasks("p", "c")
        seed.link_tasks("ini", "m1")
        # laufender Worker (active_workers) + Tenant
        seed.create_task("w", "Worker", status="running", assignee="voicera-coder", created_at=704)
        seed.conn.execute("UPDATE tasks SET tenant='voicera' WHERE id='w'")
        seed.conn.commit()
        with e.connect(autocommit=False) as wc, wc.cursor() as cur:
            cur.execute("INSERT INTO task_runs (task_id,profile,status,started_at,worker_pid) "
                        "VALUES (%s,%s,%s,%s,%s)", ("w", "voicera-coder", "running", 705, 111))
            cur.execute("INSERT INTO task_comments (task_id,author,body,created_at) "
                        "VALUES (%s,%s,%s,%s)", ("c", "dash", "hi", 706))
            cur.execute("INSERT INTO task_attachments (task_id,filename,stored_path,size,created_at) "
                        "VALUES (%s,%s,%s,%s,%s)", ("c", "a.txt", "/tmp/a", 3, 707))
            wc.commit()

        be = PgBackend(e.connect(autocommit=False))
        conn = kb.connect()
        try:
            print("[DashboardMixin: Aggregate]")
            check("all_links enthält (p,c) und (ini,m1)",
                  set(be.all_links()) >= {("p", "c"), ("ini", "m1")})
            check("comment_counts c==1", be.comment_counts().get("c") == 1)
            csp = dict(be.child_status_by_parent())  # parent -> Kind-Status
            check("child_status_by_parent: p->c(todo), ini->m1(done)",
                  csp.get("p") == "todo" and csp.get("ini") == "done", f"got {csp}")
            check("max_event_id > 0", be.max_event_id() > 0)
            check("distinct_tenants enthält voicera", "voicera" in be.distinct_tenants())
            check("distinct_assignees_unarchived enthält worker",
                  "worker" in be.distinct_assignees_unarchived())
            aw = be.active_workers()
            check("active_workers: w mit pid 111", any(r["task_id"] == "w" and r["worker_pid"] == 111 for r in aw))
            check("events_after(0) liefert Events", len(be.events_after(0)) >= 1)
            deps = be.deps_for_members(["m1"])
            check("deps_for_members(m1): Parent ini", any(d["pid"] == "ini" for d in deps))
            check("parents_not_done(c) leer (Parent done)", be.parents_not_done("c") == [])
            check("child_ids(p)==[c]", be.child_ids("c") == [] and be.child_ids("p") == ["c"])
            check("tasks_by_ids/unarchived/events_by/runs_by nicht leer",
                  len(be.tasks_by_ids(["c"])) == 1 and len(be.tasks_unarchived()) >= 6
                  and len(be.runs_by_task_ids(["w"])) == 1)

            print("[DashboardMixin: Writer]")
            check("set_status_direct c todo->ready (Parent done)", be.set_status_direct("c", "ready") is True)
            check("c jetzt ready", _scalar(e, "SELECT status FROM tasks WHERE id='c'") == "ready")
            check("set_status_direct m3->ready REFUSED (kein done-Parent? m3 hat keinen Parent -> erlaubt)",
                  be.set_status_direct("m3", "ready") is True)  # m3 parent-los -> erlaubt
            be.update_priority("c", 7)
            check("update_priority c==7", _scalar(e, "SELECT priority FROM tasks WHERE id='c'") == 7)
            be.update_title_body("c", title="Kind neu", body="text")
            check("update_title_body", _scalar(e, "SELECT title FROM tasks WHERE id='c'") == "Kind neu")
            # Parent-Gate negativ: Kind mit undone-Parent
            seed.create_task("pu", "undone", status="todo", created_at=708)
            seed.create_task("cu", "cu", status="todo", created_at=709)
            seed.link_tasks("pu", "cu")
            seed.conn.commit()
            check("set_status_direct cu->ready REFUSED (Parent nicht done)",
                  be.set_status_direct("cu", "ready") is False)

            print("[plugin_api-Helfer (conn-routed)]")
            links = api._links_for(conn, "p")
            check("_links_for(p): children=[c]", links["children"] == ["c"] and links["parents"] == [])
            check("_parents_blocking_ready(cu): pu", any(r["id"] == "pu" for r in api._parents_blocking_ready(conn, "cu")))
            check("_set_status_direct(conn,m3,'todo') routes PG",
                  api._set_status_direct(conn, "m3", "todo") is True
                  and _scalar(e, "SELECT status FROM tasks WHERE id='m3'") == "todo")
            diags = api._compute_task_diagnostics(conn, task_ids=None)
            check("_compute_task_diagnostics -> dict (kein Crash gg. PG)", isinstance(diags, dict))
            counts = api._board_counts("default")
            check("_board_counts liefert Statuszähler (nicht leer im PG)",
                  isinstance(counts, dict) and sum(counts.values()) >= 6, f"got {counts}")

            print("[Route-Handler mit expliziten Args]")
            board = api.get_board(tenant=None, include_archived=False, board=None,
                                  family_view=False, workflow_template_id=None,
                                  current_step_key=None, view="all", initiative=None)
            all_ids = {t["id"] for col in board["columns"] for t in col["tasks"]}
            check("get_board liefert Spalten + enthält Karten",
                  "columns" in board and {"ini", "w", "c"} <= all_ids, f"ids={sorted(all_ids)}")
            check("get_board tenants/assignees befüllt",
                  "voicera" in board["tenants"] and "worker" in board["assignees"])
            workers = api.list_active_workers(board=None)
            check("list_active_workers: w gelistet",
                  any(x["task_id"] == "w" for x in workers["workers"]))
            ld = api.list_diagnostics(board=None, severity=None)
            check("list_diagnostics liefert Struktur", "diagnostics" in ld and "count" in ld)
            # Writer-Route: update_task priority via Pydantic-Payload
            upd = api.update_task("c", api.UpdateTaskBody(priority=3), board=None)
            check("update_task(priority) -> task", upd.get("task") is not None
                  and _scalar(e, "SELECT priority FROM tasks WHERE id='c'") == 3)
            bu = api.bulk_update(api.BulkTaskBody(ids=["c", "m2"], priority=5), board=None)
            check("bulk_update -> results je id",
                  len(bu["results"]) == 2
                  and _scalar(e, "SELECT priority FROM tasks WHERE id='m2'") == 5)
        finally:
            conn.close()
            be.conn.close()
            seed.conn.close()
    finally:
        os.environ.pop("HERMES_KANBAN_DB", None)
        e.teardown()

    print(f"\nTESTS dashboard-api: {'ALLE GRÜN' if not check.fails else f'{len(check.fails)} ROT: {check.fails}'}")
    return 0 if not check.fails else 2


def _scalar(env, sql):
    with env.connect(autocommit=True) as c:
        return c.execute(sql).fetchone()[0]


if __name__ == "__main__":
    sys.exit(main())
