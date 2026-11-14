"""B1.1 Nachzug — kanban_db-Routing-Guards, die beim Consumer-Port fehlten.

Regression für den PG-Start-Befund 2026-11-14: ``kanban show`` crashte den
Beobachter, weil ``kanban_db.parent_ids``/``child_ids`` rohes sqlite-``?`` an
psycopg schickten (``0 placeholders but 1 parameters``); ``board_stats`` und
``known_assignees`` lasen ``row["col"]`` auf psycopg-Tupeln (``tuple indices
must be integers``). Dieser Test fährt die Funktionen über eine echte
psycopg-Verbindung (connect() im PG-Modus) und schlägt vor dem Fix fehl.

Dual-mode: echtes PG via HERMES_KANBAN_PG_DSN (Wegwerf-Schema, public bleibt
unberührt) oder pgserver-Fallback.

    PYTHONPATH=<repo> python tests/pg/test_db_guards_w5.py
"""
import sys

from _w3env import Check, PgEnv, mode_banner

from kanban_pg import PgBackend  # noqa: E402


def main():
    print(mode_banner("db-guards-w5"))
    chk = Check()
    e = PgEnv("dbguards")
    try:
        e.apply_schema(triggers=True)
        e.activate_routing()

        from hermes_cli import kanban_db as kb

        # Seed: parent + child (verlinkt) + zwei assignee-tragende Karten.
        seed = PgBackend(e.connect(autocommit=False))
        seed.create_task("gp", "Parent", status="ready", created_at=100)
        seed.create_task("gc", "Child", status="ready", created_at=101)
        seed.link_tasks("gp", "gc")
        seed.create_task("ga", "A1", status="ready", assignee="alice", created_at=102)
        seed.create_task("gb", "A2", status="todo", assignee="alice", created_at=103)

        conn = kb.connect()  # PG-Modus → psycopg-Verbindung (die Guards greifen)
        chk("conn ist KEINE sqlite3.Connection (PG-Routing aktiv)",
            type(conn).__module__.startswith("psycopg"))

        # parent_ids / child_ids — der Beobachter-Crash-Pfad (kanban show).
        chk("parent_ids(child) == [parent]", kb.parent_ids(conn, "gc") == ["gp"])
        chk("child_ids(parent) == [child]", kb.child_ids(conn, "gp") == ["gc"])
        chk("parent_ids(unverlinkt) == []", kb.parent_ids(conn, "ga") == [])
        chk("child_ids(unverlinkt) == []", kb.child_ids(conn, "gc") == [])

        # board_stats — Row-Shape über psycopg-Tupel.
        stats = kb.board_stats(conn)
        chk("board_stats liefert dict", isinstance(stats, dict))
        # gp+ga ready; gc wird durch link_tasks (undone Parent gp) ready→todo
        # demotet, gb ist todo → ready==2, todo==2.
        chk("board_stats.by_status zählt ready (gp,ga)",
            stats["by_status"].get("ready") == 2, detail=str(stats.get("by_status")))
        chk("board_stats.by_status zählt todo (gc,gb)",
            stats["by_status"].get("todo") == 2, detail=str(stats.get("by_status")))
        chk("board_stats.by_assignee['alice'] hat ready+todo",
            stats["by_assignee"].get("alice", {}).get("ready") == 1
            and stats["by_assignee"].get("alice", {}).get("todo") == 1,
            detail=str(stats.get("by_assignee")))
        chk("board_stats.oldest_ready_age_seconds ist int",
            isinstance(stats["oldest_ready_age_seconds"], int))

        # known_assignees — Row-Shape über psycopg-Tupel.
        known = kb.known_assignees(conn)
        alice = next((x for x in known if x["name"] == "alice"), None)
        chk("known_assignees enthält 'alice'", alice is not None)
        chk("known_assignees['alice'].counts stimmt",
            alice is not None and alice["counts"].get("ready") == 1
            and alice["counts"].get("todo") == 1,
            detail=str(alice))

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
