"""B1.1 Nachzug — PG-Tauglichkeit der 12 ungeguardeten Schreiber (+ ihrer
roh-``?``-Helfer-Closure) in kanban_db.py.

Regression für den PG-Start-Befund 2026-11-14: handoff_task / grant_go /
plan_human_step / delete_task / … schickten rohes sqlite-``?`` an psycopg
(``0 placeholders but N parameters``) bzw. nutzten ``write_txn`` mit
``BEGIN IMMEDIATE`` (sqlite-only). Dieser Test fährt einen vollen
Fabrik-Durchlauf (create → coder → review-Rückläufer → Merger-GO → Abschluss)
sowie je einen Smoke pro Schreiber über eine echte psycopg-Verbindung
(``kb.connect()`` im PG-Modus). KEIN Schritt darf ``0 placeholders`` /
``tuple indices`` / lastrowid werfen.

Dual-mode: echtes PG via HERMES_KANBAN_PG_DSN (Wegwerf-Schema, public bleibt
unberührt) oder pgserver. Ausführen:

    DSN=... HERMES_KANBAN_PG_DSN=$DSN PYTHONPATH=<repo> \
        python tests/pg/test_pg_writer_roundtrip_w5.py
"""
import sys

from _w3env import Check, PgEnv, mode_banner

from kanban_pg import PgBackend  # noqa: E402


def main():
    print(mode_banner("pg-writer-roundtrip-w5"))
    chk = Check()
    e = PgEnv("wrt")
    try:
        e.apply_schema(triggers=True)
        e.activate_routing()

        from hermes_cli import kanban_db as kb

        seed = PgBackend(e.connect(autocommit=False))
        conn = kb.connect()  # PG-Modus → psycopg (die konvertierten Pfade greifen)
        chk("conn ist psycopg (PG-Routing aktiv)",
            type(conn).__module__.startswith("psycopg"))

        # ---------------- TEIL A: voller Pipeline-Durchlauf ----------------
        seed.create_task("rt", "Roundtrip", status="running",
                         assignee="orchestrator", created_at=500)
        # orchestrator → coder (handoff_task + _end_run/_synthesize_ended_run/_append_event)
        chk("handoff → coder",
            kb.handoff_task(conn, "rt", "coder",
                            note="ABNAHMEKRITERIUM: x", author="specifier") is True)
        t = kb.get_task(conn, "rt")
        chk("rt ready@coder", t.status == "ready" and t.assignee == "coder")
        # coder → reviewer (Review-Vorlage)
        chk("handoff → reviewer",
            kb.handoff_task(conn, "rt", "reviewer", note="bitte review") is True)
        # Review-Rückläufer reviewer → coder
        chk("review-return → coder",
            kb.handoff_task(conn, "rt", "coder", note="nachbessern: y") is True)
        # Hand-back an orchestrator (_scope_recut_count < ceiling → normaler Pfad)
        chk("handoff → orchestrator",
            kb.handoff_task(conn, "rt", "orchestrator", note="scope?") is True)

        # Merger-GO-Gate aufsetzen (wie der Dispatcher) + grant_go
        seed.create_task("gg", "Merge gated", status="running",
                         assignee="merger", created_at=501)
        with kb.write_txn(conn):
            kb._q(conn,
                  "UPDATE tasks SET status='blocked', assignee='till', "
                  "block_kind='needs_input' WHERE id=?", ("gg",))
            kb._append_event(conn, "gg", "blocked",
                             {"kind": "needs_input", "reason": "go_gate"})
            kb._append_event(conn, "gg", "go_gate_held",
                             {"assignee": "till", "status": "blocked",
                              "kind": "merge", "resume_assignee": "merger"})
        granted = kb.grant_go(conn, "gg", kind="merge", actor="till")
        chk("grant_go applied", granted.applied is True)
        gt = kb.get_task(conn, "gg")
        chk("gg nach GO ready/todo@merger",
            gt.status in ("ready", "todo") and gt.assignee == "merger",
            detail=f"{gt.status}@{gt.assignee}")
        # Abschluss (complete_task ist geguardet → PgBackend; muss weiter laufen)
        chk("complete_task", kb.complete_task(conn, "gg", summary="merged") is True)
        chk("gg done", kb.get_task(conn, "gg").status == "done")

        # get_run über eine durch handoff synthetisierte run-id von rt
        lr = kb.latest_run(conn, "rt")
        if lr is not None:
            gr = kb.get_run(conn, lr.id)
            chk("get_run(id)", gr is not None and gr.id == lr.id)
        else:
            chk("get_run (keine run-id — übersprungen)", True)

        # ---------------- TEIL B: Smoke je Schreiber ----------------
        # add_attachment (id-INSERT via _q_insert) + delete_attachment (DELETE)
        seed.create_task("at", "Attach", status="ready", created_at=502)
        aid = kb.add_attachment(conn, "at", filename="log.txt",
                                stored_path="/tmp/does-not-exist-xyz", size=3)
        chk("add_attachment id>0", isinstance(aid, int) and aid > 0)
        chk("delete_attachment", kb.delete_attachment(conn, aid) is not None)

        # plan_human_step (INSERT task_links + UPDATE)
        seed.create_task("pp", "Pred", status="running", created_at=503)
        seed.create_task("ph", "Planned", status="ready", created_at=504)
        chk("plan_human_step planned",
            kb.plan_human_step(conn, "ph", after=["pp"]) == "planned")

        # gc_events (DELETE … IN subquery)
        chk("gc_events int", isinstance(kb.gc_events(conn, older_than_seconds=0), int))

        # cleanup_worktree_artifacts (2 SELECTs; done-worktree, Pfad existiert nicht)
        seed.create_task("cw", "done", status="done", created_at=505)
        kb._q(conn,
              "UPDATE tasks SET workspace_kind='worktree', "
              "workspace_path='/tmp/no-such-wt-xyz' WHERE id=?", ("cw",))
        conn.commit()
        chk("cleanup_worktree_artifacts list",
            isinstance(kb.cleanup_worktree_artifacts(conn, "cw"), list))

        # delete_task + delete_archived_task
        seed.create_task("dt", "to delete", status="ready", created_at=506)
        chk("delete_task", kb.delete_task(conn, "dt") is True)
        seed.create_task("da", "archived", status="archived", created_at=507)
        chk("delete_archived_task", kb.delete_archived_task(conn, "da") is True)

        conn.close()
    finally:
        e.teardown()

    if chk.fails:
        print(f"\nFAIL: {len(chk.fails)} — {chk.fails}")
        return 1
    print("\nALLE GRÜN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
