"""B1.1 Welle-3 (tools) — Worker-Tool-Routing gegen PG.

Karte b1.1-port-tools. Muss-Fälle: Worker-complete über Tool -> done (+ recompute_
ready-Folge); Worker-block über Tool -> blocked; Auto-Heartbeat (#31752 B-Pfad)
bumpt last_heartbeat_at der laufenden Karte; backend=sqlite unverändert
(Regression test_kanban_db.py). Die Tools laufen über _write_locked (connect +
write_serialize_lock, beide PG-fähig) + die conn-geguardeten kanban_db-Writer.

    HERMES_KANBAN_PG_DSN=... PYTHONPATH=<repo> python tests/pg/test_tools.py
"""
import os
import sys

from _w3env import Check, PgEnv, mode_banner

from kanban_pg import PgBackend  # noqa: E402


def _scalar(env, sql):
    with env.connect(autocommit=True) as c:
        return c.execute(sql).fetchone()[0]


def main():
    print(mode_banner("tools"))
    check = Check()
    e = PgEnv("tools")
    # Orchestrator-Kontext: KEIN HERMES_KANBAN_TASK -> Ownership-Gate inert.
    os.environ.pop("HERMES_KANBAN_TASK", None)
    os.environ["HERMES_PROFILE"] = "orchestrator"
    try:
        e.apply_schema(triggers=True)
        e.activate_routing()

        from hermes_cli import kanban_db as kb
        import tools.kanban_tools as kt

        seed = PgBackend(e.connect(autocommit=False))
        # complete-Kette: Parent 'wc' + abhängiges Kind 'wc_child'. wc trägt
        # einen branch_name = normale Build-Karte: der Zielentscheidungs-Gate
        # im Handler (nur für branch-lose Entscheidungskarten) greift NICHT,
        # also läuft complete durch die geguardete Kern-Op. (Der Inline-SQL-
        # Scan dieses Gates ist noch sqlite-only — s. OFFEN.md-Befund.)
        seed.create_task("wc", "Arbeit", status="ready", assignee="worker", created_at=200)
        seed.conn.execute("UPDATE tasks SET branch_name=%s WHERE id=%s", ("wt/wc", "wc"))
        seed.conn.commit()
        seed.create_task("wc_child", "Folgekind", status="todo", created_at=201)
        seed.link_tasks("wc", "wc_child")
        # block-Karte
        seed.create_task("wb", "Blockkandidat", status="ready", assignee="worker", created_at=202)
        # heartbeat-Karte
        seed.create_task("hb", "Läufer", status="ready", assignee="worker", created_at=203)

        print("[Worker-complete über Tool -> done + recompute-Folge]")
        complete = kt._write_locked(kt._handle_complete)
        out = complete({"task_id": "wc", "summary": "fertig"})
        check("kanban_complete kein tool_error", "error" not in str(out).lower() or '"ok"' in str(out).lower(),
              f"out={out}")
        check("wc -> done", _scalar(e, "SELECT status FROM tasks WHERE id='wc'") == "done",
              f"status={_scalar(e, 'SELECT status FROM tasks WHERE id=' + chr(39) + 'wc' + chr(39))}")
        check("recompute-Folge: wc_child todo->ready (Parent done)",
              _scalar(e, "SELECT status FROM tasks WHERE id='wc_child'") == "ready",
              f"child={_scalar(e, 'SELECT status FROM tasks WHERE id=' + chr(39) + 'wc_child' + chr(39))}")

        print("[Worker-block über Tool -> blocked]")
        block = kt._write_locked(kt._handle_block)
        out = block({"task_id": "wb", "reason": "brauche Eingabe", "kind": "needs_input"})
        check("kanban_block kein harter Fehler", out is not None)
        check("wb -> blocked", _scalar(e, "SELECT status FROM tasks WHERE id='wb'") == "blocked",
              f"status={_scalar(e, 'SELECT status FROM tasks WHERE id=' + chr(39) + 'wb' + chr(39))}")
        check("block_kind=needs_input gespeichert",
              _scalar(e, "SELECT block_kind FROM tasks WHERE id='wb'") == "needs_input")

        print("[Auto-Heartbeat (#31752 B-Pfad) -> last_heartbeat_at gebumpt]")
        claimed = seed.claim_task("hb", claimer="host:hbtest")
        check("hb geclaimt -> running", claimed is not None
              and _scalar(e, "SELECT status FROM tasks WHERE id='hb'") == "running")
        before = _scalar(e, "SELECT last_heartbeat_at FROM tasks WHERE id='hb'")
        os.environ["HERMES_KANBAN_TASK"] = "hb"
        os.environ["HERMES_KANBAN_CLAIM_LOCK"] = "host:hbtest"
        kt._auto_heartbeat_last_attempt = 0.0  # Rate-Limit zurücksetzen
        fired = kt.heartbeat_current_worker_from_env()
        after = _scalar(e, "SELECT last_heartbeat_at FROM tasks WHERE id='hb'")
        check("heartbeat_current_worker_from_env lief (write attempted)", fired is True)
        check("last_heartbeat_at gesetzt/gebumpt",
              after is not None and (before is None or after >= before),
              f"before={before} after={after}")
        os.environ.pop("HERMES_KANBAN_TASK", None)
        os.environ.pop("HERMES_KANBAN_CLAIM_LOCK", None)

        seed.conn.close()
    finally:
        os.environ.pop("HERMES_PROFILE", None)
        e.teardown()

    print(f"\nTESTS tools: {'ALLE GRÜN' if not check.fails else f'{len(check.fails)} ROT: {check.fails}'}")
    return 0 if not check.fails else 2


if __name__ == "__main__":
    sys.exit(main())
