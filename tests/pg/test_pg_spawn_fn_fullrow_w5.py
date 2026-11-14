"""B1.1 — der ECHTE Dispatcher-spawn_fn muss die volle Task bekommen (PG).

Befund 2026-11-14: `PgBackend.claim_task` liefert ein schmales dict (`get_task`,
10 Spalten, ohne `body`); der LIVE-Default-spawn_fn in `_pg_dispatch_tick`
deserialisierte es mit `Task.from_row` → `KeyError: 'body'` → jede Karte fiel in
`spawn_failed`. Die alten Spawn-Tests nutzten einen Stub-spawn_fn (`task["id"]`)
und umgingen das. Dieser Test fährt den ECHTEN Default-spawn_fn über
`_pg_dispatch_tick` und stubbt nur `_default_spawn` (kein echter OS-Worker).

Fix: der spawn_fn holt die volle Task via geguardetem `get_task` (→ get_task_full).

Dual-mode: echtes PG via HERMES_KANBAN_PG_DSN (Wegwerf-Schema) oder pgserver.

    PYTHONPATH=<repo> python tests/pg/test_pg_spawn_fn_fullrow_w5.py
"""
import os
import pathlib
import sys
import tempfile

from _w3env import Check, PgEnv, mode_banner

from kanban_pg import PgBackend  # noqa: E402


def main():
    print(mode_banner("spawn_fn-fullrow"))
    chk = Check()
    e = PgEnv("spawnfn")
    # HERMES_KANBAN_DB test-lokal pinnen, damit der Halt-Flag-Check von
    # _pg_dispatch_tick auf eine temp (nicht gesetzte) Datei zeigt, nicht aufs
    # Live-Board. Im PG-Modus routet connect() ohnehin nach PG.
    tmpdb = pathlib.Path(tempfile.mkdtemp(prefix="spawnfn-")) / "kanban.db"
    os.environ["HERMES_KANBAN_DB"] = str(tmpdb)
    try:
        e.apply_schema(triggers=True)
        e.activate_routing()

        from hermes_cli import kanban_db as kb
        from gateway.kanban_watchers import _pg_dispatch_tick

        seed = PgBackend(e.connect(autocommit=False))
        seed.create_task("sp1", "Spawn-Vollzeile", status="ready",
                         assignee="recon", created_at=500)
        # Body setzen, damit wir beweisen können, dass die volle Zeile ankommt.
        qc = e.connect(autocommit=True)
        qc.execute("UPDATE tasks SET body=%s WHERE id='sp1'", ("VOLLER BODY",))

        conn = kb.connect()  # PG-Modus → psycopg

        captured = {}
        orig_spawn = kb._default_spawn

        def stub_spawn(task, workspace, *, board=None):
            # Der echte spawn_fn ruft dies mit einer kb.Task — hier beweisen wir,
            # dass es eine VOLLE Task ist (body lesbar), ohne echten Prozess.
            captured["task"] = task
            captured["workspace"] = workspace
            captured["board"] = board
            return os.getpid()  # ein lebender PID → dispatch wertet als spawned

        kb._default_spawn = stub_spawn
        try:
            res = _pg_dispatch_tick(kb, conn, "default", spawn_fn=None)
        finally:
            kb._default_spawn = orig_spawn

        chk("spawn_fn wurde aufgerufen (kein KeyError verschluckt)",
            "task" in captured)
        t = captured.get("task")
        chk("spawn_fn bekam eine kb.Task", isinstance(t, kb.Task))
        chk("Task ist VOLL — body ohne KeyError lesbar",
            t is not None and t.body == "VOLLER BODY", detail=repr(getattr(t, "body", "<none>")))
        chk("Task.id == sp1", t is not None and t.id == "sp1")
        chk("board durchgereicht", captured.get("board") == "default")
        chk("Karte ist running (Spawn erfolgreich, kein spawn_failed)",
            qc.execute("SELECT status FROM tasks WHERE id='sp1'").fetchone()[0] == "running")
        chk("kein spawn_failed-Run",
            qc.execute("SELECT count(*) FROM task_runs WHERE task_id='sp1' "
                       "AND outcome='spawn_failed'").fetchone()[0] == 0)
        chk("Tick meldet sp1 als spawned",
            any(s[0] == "sp1" for s in (res.spawned or [])))

        conn.close()
    finally:
        os.environ.pop("HERMES_KANBAN_DB", None)
        e.teardown()

    if chk.fails:
        print(f"\nFAIL: {len(chk.fails)} — {chk.fails}")
        return 1
    print("\nALLE GRÜN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
