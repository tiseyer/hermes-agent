"""B1.1 — Live-Pfad-Stationen echt fahren, die bisher Attrappen/ungetestet waren.

Aus ~/recon/karten/live-pfad-attrappen-2026-11-14.md. Deckt ab:
- Station 6:  _default_spawn baut argv+env aus einer ECHTEN PG-geclaimten Task
              (nur subprocess.Popen gemockt → kein OS-Prozess; Env/argv echt).
- Station 10: resolve_task_repo_profiles liest eine Karte über eine ECHTE psycopg-conn.
- Station 14: block_task_full(kind=review) mit dem ECHTEN config-gestützten
              reviewer_resolver (resolve_task_repo_profiles), nicht dem Test-Lambda.

Stationen 4/5 (claim_task-Form × Task.from_row) → test_pg_spawn_fn_fullrow_w5.
Stationen 7/8 (Profil-HERMES_HOME → backend=postgres) → test_profile_backend_inherit_w5
und die Generalprobe. Station 1 (async _kanban_dispatcher_watcher-Loop) → begründet in
der Landkarte stehen gelassen: reine asyncio-Gateway-Schleife, der synchrone Kern
_pg_dispatch_tick wird echt gefahren.

    PYTHONPATH=<repo> HERMES_KANBAN_PG_DSN=<root-dsn> python tests/pg/test_live_path_stations_w5.py
"""
import os
import pathlib
import sys
import tempfile

from _w3env import Check, PgEnv, mode_banner

from kanban_pg import PgBackend  # noqa: E402


def main():
    print(mode_banner("live-path-stations"))
    chk = Check()
    e = PgEnv("stations")
    tmpdb = pathlib.Path(tempfile.mkdtemp(prefix="stations-")) / "kanban.db"
    os.environ["HERMES_KANBAN_DB"] = str(tmpdb)
    try:
        e.apply_schema(triggers=True)
        e.activate_routing()

        from hermes_cli import kanban_db as kb

        seed = PgBackend(e.connect(autocommit=False))
        seed.create_task("st_spawn", "Spawn-Env-Karte", status="ready",
                         assignee="recon", created_at=600)
        seed.create_task("st_repo", "Repo-Routing-Karte", status="ready",
                         assignee="hermes-coder", created_at=601)
        q = e.connect(autocommit=True)
        q.execute("UPDATE tasks SET repository=%s WHERE id IN ('st_spawn','st_repo')",
                  ("hermes",))

        conn = kb.connect()  # PG

        # --- Station 6: _default_spawn baut aus ECHTER PG-Task, Popen gemockt ---
        task = kb.get_task(conn, "st_spawn")  # volle Task aus PG (wie der Fix)
        rec = {}
        import subprocess
        orig_popen = subprocess.Popen

        class FakePopen:
            def __init__(self, cmd, **kw):
                rec["cmd"] = cmd
                rec["env"] = kw.get("env", {})
                rec["cwd"] = kw.get("cwd")
                self.pid = 424242

        subprocess.Popen = FakePopen
        try:
            pid = kb._default_spawn(task, task.workspace_path or "", board="default")
        finally:
            subprocess.Popen = orig_popen
        chk("Station6: _default_spawn gibt PID (argv/env gebaut, kein KeyError)", pid == 424242)
        cmd = rec.get("cmd") or []
        chk("Station6: argv ist `hermes -p recon … chat`",
            "-p" in cmd and "recon" in cmd and "chat" in cmd, detail=str(cmd)[:120])
        env = rec.get("env") or {}
        chk("Station6: Env HERMES_KANBAN_TASK == st_spawn", env.get("HERMES_KANBAN_TASK") == "st_spawn")
        chk("Station6: Env HERMES_PROFILE == recon", env.get("HERMES_PROFILE") == "recon")

        # --- Station 10: resolve_task_repo_profiles über ECHTE psycopg-conn ---
        try:
            prof = kb.resolve_task_repo_profiles(conn, "st_repo")
            chk("Station10: resolve_task_repo_profiles läuft auf PG (Tupel oder None)",
                prof is None or (isinstance(prof, tuple) and len(prof) == 3), detail=str(prof))
        except Exception as ex:  # noqa: BLE001
            chk("Station10: resolve_task_repo_profiles ohne Fehler auf PG", False, detail=repr(ex))

        # --- Station 14: block_task_full(kind=review) mit ECHTEM reviewer_resolver ---
        be = PgBackend(conn)
        calls = {}

        def real_resolver(task_id, tenant):
            # Der ECHTE Pfad: config-gestützte Rollen-Auflösung (resolve_task_repo_profiles),
            # NICHT ein Konstant-Lambda. Fällt auf 'reviewer' zurück, wenn nichts auflöst.
            calls["args"] = (task_id, tenant)
            r = kb.resolve_task_repo_profiles(conn, task_id)
            return (r[1] if r and r[1] else "reviewer")

        # Karte erst running (claim) → dann review-block (Rückläufer-Grenze).
        be.claim_task("st_repo")
        ok = be.block_task_full("st_repo", kind="review", reviewer_resolver=real_resolver)
        chk("Station14: block_task_full(review) läuft mit echtem Resolver", bool(ok))
        chk("Station14: echter reviewer_resolver wurde gerufen (task_id,tenant)",
            calls.get("args", (None,))[0] == "st_repo")
        strow = q.execute("SELECT status, assignee FROM tasks WHERE id='st_repo'").fetchone()
        chk("Station14: Karte in review-Spalte geroutet", strow[0] == "review", detail=str(strow))

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
