"""Infra-Crash-Bremse im PG-Verteiler (2026-11-13, Charlie) — stellt die Lage
vom 11.11. nach: ungültige Codex-Anmeldung → Worker crashen reihenweise → der
Verteiler MUSS nach wenigen Ticks anhalten (Halt-Flag + Alert), statt tausende
Worker zu starten. Aufgaben-Inhaltsfehler (gave_up/blocked) lösen NICHT aus.

Lauf:  HERMES_KANBAN_PG_DSN=... PYTHONPATH=<fork-wt> python tests/pg/test_dispatch_breaker.py
"""
import os, pathlib, sys, tempfile, time
import psycopg

WT = pathlib.Path(os.environ.get("HERMES_FORK_WT", "/home/till/fabrik-loop-wt")).resolve()
sys.path.insert(0, str(WT))
import hermes_cli.kanban_db as kdb            # noqa: E402
from gateway import kanban_watchers as kw     # noqa: E402

PKG = WT / "kanban_pg"
PROD = os.environ.get("HERMES_KANBAN_PG_DSN", "").strip() or None
FAILS = []
def check(n, c): print(f"  [{'OK' if c else 'FAIL'}] {n}"); (FAILS.append(n) if not c else None)


def _schema_env(name):
    assert PROD, "nur gegen echtes PG (Wegwerf-Schema)"
    sch = f"brk_{name}_{os.getpid()}"
    with psycopg.connect(PROD, autocommit=True) as c:
        c.execute(f'DROP SCHEMA IF EXISTS "{sch}" CASCADE'); c.execute(f'CREATE SCHEMA "{sch}"')
    sep = "&" if "?" in PROD else "?"
    return sch, f"{PROD}{sep}options=-c%20search_path%3D{sch}%2Cpublic"


def main():
    sch, uri = _schema_env("x")
    try:
        with psycopg.connect(uri, autocommit=True) as c, c.cursor() as cur:
            cur.execute((PKG / "schema.sql").read_text())
        now = 2_000_000_000
        halt = kw._halt_flag_for(kdb, "default")
        tmp = pathlib.Path(tempfile.mkdtemp()) / "kanban.db"   # Halt-Flag ins tmp lenken
        # kanban_db_path auf ein Wegwerf-Pfad biegen, damit wir die echte .halt nicht anfassen
        orig = kdb.kanban_db_path
        kdb.kanban_db_path = lambda board=None: tmp
        halt = kw._halt_flag_for(kdb, "default")
        kw._INFRA_CRASH_STREAK.clear()
        alerts = []
        # --- NEGATIV: Aufgaben-Inhaltsfehler (gave_up) lösen NICHT aus ---
        with psycopg.connect(uri, autocommit=True) as c:
            for i in range(10):
                c.execute("INSERT INTO task_events (task_id,kind,created_at) VALUES (%s,'gave_up',%s)", (f"t_gv{i}", now))
        with psycopg.connect(uri) as c:
            for _ in range(5):
                kw._infra_crash_guard(kdb, c, "default", now=now, alert_fn=alerts.append)
        check("gave_up (Aufgaben-Inhalt) löst NICHT aus", not halt.exists() and not alerts)
        # --- POSITIV: reihenweise Crashes, 0 fertig → Halt nach streak_limit ---
        with psycopg.connect(uri, autocommit=True) as c:
            for i in range(10):
                c.execute("INSERT INTO task_events (task_id,kind,created_at) VALUES (%s,'crashed',%s)", (f"t_cr{i}", now))
        tripped = []
        with psycopg.connect(uri) as c:
            for _ in range(3):   # streak_limit=3
                tripped.append(kw._infra_crash_guard(kdb, c, "default", now=now, alert_fn=alerts.append))
        check("Halt nach 3 Crash-Ticks gesetzt", halt.exists())
        check("genau beim 3. Tick ausgelöst", tripped == [False, False, True])
        check("Telegram-Alert mit Grund gesendet", len(alerts) == 1 and "ANGEHALTEN" in alerts[0])
        # --- Nach Halt skippt der Tick (kein neuer Spawn) ---
        res = kw._pg_dispatch_tick(kdb, psycopg.connect(uri), "default",
                                   spawn_fn=lambda td: ("x", "x", "x"), _now=now)
        check("Tick skippt bei gesetztem Halt-Flag", getattr(res, "skipped_locked", False))
        kdb.kanban_db_path = orig
    finally:
        with psycopg.connect(PROD, autocommit=True) as c:
            c.execute(f'DROP SCHEMA IF EXISTS "{sch}" CASCADE')
    print("ALLE GRÜN" if not FAILS else "FAILS: " + ", ".join(FAILS)); sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
