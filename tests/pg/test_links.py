"""B1.1 Welle-2 (links) — faithful-Port-Tests. Dual-mode (pgserver | echtes PG).

    HERMES_KANBAN_PG_DSN=... PYTHONPATH=<repo> python tests/pg/test_links.py

Muss-Fälle (Karte b1.1-port-links): link setzt Kante + demotet ready-Child mit
undone-Parent→todo; unlink erlaubt recompute_ready→ready wenn letzter Parent weg/done;
decompose legt Kinder+Links an; doppeltes link idempotent (ON CONFLICT).
"""
import os
import pathlib
import shutil
import sys
import tempfile

import psycopg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from kanban_pg import PgBackend  # noqa: E402

PKG = pathlib.Path(__file__).resolve().parents[2] / "kanban_pg"
PROD_DSN = os.environ.get("HERMES_KANBAN_PG_DSN", "").strip() or None
FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class Env:
    def __init__(self, name):
        self.name = name
        self._srv = self._dir = self._schema = None
        if PROD_DSN:
            self._schema = f"b1l_{name}_{os.getpid()}"
            with psycopg.connect(PROD_DSN, autocommit=True) as c:
                c.execute(f'DROP SCHEMA IF EXISTS "{self._schema}" CASCADE')
                c.execute(f'CREATE SCHEMA "{self._schema}"')
            sep = "&" if "?" in PROD_DSN else "?"
            self.uri = f"{PROD_DSN}{sep}options=-c%20search_path%3D{self._schema}%2Cpublic"
        else:
            xdg = pathlib.Path(tempfile.mkdtemp(prefix="xdg-"))
            os.environ.setdefault("XDG_RUNTIME_DIR", str(xdg))
            import pgserver
            self._dir = pathlib.Path(tempfile.mkdtemp(prefix="pgdata-"))
            self._srv = pgserver.get_server(self._dir)
            self.uri = self._srv.get_uri()

    def connect(self, autocommit=False):
        return psycopg.connect(self.uri, autocommit=autocommit)

    def teardown(self):
        if PROD_DSN:
            with psycopg.connect(PROD_DSN, autocommit=True) as c:
                c.execute(f'DROP SCHEMA IF EXISTS "{self._schema}" CASCADE')
        else:
            self._srv.cleanup()
            shutil.rmtree(self._dir, ignore_errors=True)


def _apply_schema(env):
    with env.connect(autocommit=True) as c, c.cursor() as cur:
        cur.execute((PKG / "schema.sql").read_text())
        cur.execute((PKG / "triggers.sql").read_text())


def _status(q, tid):
    r = q.execute("SELECT status FROM tasks WHERE id=%s", (tid,)).fetchone()
    return r[0] if r else None


def _events(q, tid):
    return [r[0] for r in q.execute(
        "SELECT kind FROM task_events WHERE task_id=%s ORDER BY id", (tid,)).fetchall()]


def main():
    print(f"=== links — Modus: {'ECHTES PG (Wegwerf-Schema)' if PROD_DSN else 'pgserver'} ===")
    e = Env("core")
    try:
        _apply_schema(e)
        conn = e.connect(autocommit=False)
        q = e.connect(autocommit=True)
        try:
            b = PgBackend(conn)

            print("[link setzt Kante + demotet ready-Child bei undone-Parent]")
            p = b.create_task_full(title="parent")  # ready (keine Parents)
            c = b.create_task_full(title="child")    # ready
            b.link_tasks(p, c)
            edge = q.execute("SELECT 1 FROM task_links WHERE parent_id=%s AND child_id=%s", (p, c)).fetchone()
            check("Kante angelegt", edge is not None)
            check("ready-Child mit undone-Parent → todo", _status(q, c) == "todo")
            check("linked-Event auf Child", "linked" in _events(q, c))

            print("[doppeltes link idempotent (ON CONFLICT)]")
            b.link_tasks(p, c)
            n = q.execute("SELECT count(*) FROM task_links WHERE parent_id=%s AND child_id=%s", (p, c)).fetchone()[0]
            check("keine Duplikat-Kante", n == 1)

            print("[self-link + cycle verboten]")
            try:
                b.link_tasks(c, c); check("self-link raises", False)
            except ValueError:
                conn.rollback(); check("self-link raises", True)
            try:
                b.link_tasks(c, p); check("cycle raises", False)  # p→c existiert, c→p = Zyklus
            except ValueError:
                conn.rollback(); check("cycle raises", True)

            print("[unlink → recompute → ready (letzter Parent weg)]")
            ok = b.unlink_tasks(p, c)
            check("unlink True + unlinked-Event", ok and "unlinked" in _events(q, c))
            check("Child nach unlink via recompute → ready (keine Parents mehr)", _status(q, c) == "ready")

            print("[unlink: Parent done → Child ready]")
            pd = b.create_task_full(title="pdone")
            cd = b.create_task_full(title="cd")
            b.link_tasks(pd, cd)  # pd ready(undone) → cd todo
            check("cd todo nach link", _status(q, cd) == "todo")
            b.complete_task_full(pd)  # pd done → recompute cd→ready
            check("cd ready nach parent done", _status(q, cd) == "ready")

            print("[family_root-Propagation parent→child]")
            root = b.create_task_full(title="root")
            q.execute("UPDATE tasks SET family_root_id=%s, family_order=0 WHERE id=%s", (root, root))
            m1 = b.create_task_full(title="member")
            b.link_tasks(root, m1)
            fr = q.execute("SELECT family_root_id, family_order FROM tasks WHERE id=%s", (m1,)).fetchone()
            check("child erbt family_root + order>=1", fr[0] == root and fr[1] >= 1)

            print("[specify_triage_task: triage→todo]")
            tr = b.create_task_full(title="spec me", triage=True)
            check("specify True", b.specify_triage_task(tr, title="spec done", body="B", author="spec") is True)
            check("triage→ready (parent-los, scoped promote)", _status(q, tr) == "ready")
            check("specified-Event", "specified" in _events(q, tr))

            print("[decompose_triage_task: Kinder + Links + auto_promote]")
            dr = b.create_task_full(title="decompose me", triage=True)
            kids = b.decompose_triage_task(
                dr, root_assignee="orchestrator",
                children=[
                    {"title": "k0"},
                    {"title": "k1", "parents": [0]},
                ], author="decomposer")
            check("2 Kinder angelegt", isinstance(kids, list) and len(kids) == 2, f"got {kids}")
            check("k1 unter k0 verlinkt", q.execute(
                "SELECT 1 FROM task_links WHERE parent_id=%s AND child_id=%s", (kids[0], kids[1])).fetchone() is not None)
            check("root unter jedem Kind (wartet)", q.execute(
                "SELECT count(*) FROM task_links WHERE child_id=%s", (dr,)).fetchone()[0] == 2)
            check("root → family-Projektion (family_root=self, order 0)", q.execute(
                "SELECT family_root_id, family_order FROM tasks WHERE id=%s", (dr,)).fetchone() == (dr, 0))
            check("k0 (parent-frei) auto_promote → ready", _status(q, kids[0]) == "ready")
            check("k1 (hat parent k0) bleibt todo", _status(q, kids[1]) == "todo")
            check("decomposed-Event auf root", "decomposed" in _events(q, dr))
            check("root assignee gesetzt", q.execute("SELECT assignee FROM tasks WHERE id=%s", (dr,)).fetchone()[0] == "orchestrator")

            print("[decompose Zyklus-Erkennung]")
            dz = b.create_task_full(title="cyclic", triage=True)
            try:
                b.decompose_triage_task(dz, root_assignee=None,
                    children=[{"title": "a", "parents": [1]}, {"title": "b", "parents": [0]}])
                check("decompose cycle raises", False)
            except ValueError:
                conn.rollback(); check("decompose cycle raises", True)
        finally:
            conn.close(); q.close()
    finally:
        e.teardown()

    print(f"\nTESTS links ({'PROD-PG' if PROD_DSN else 'pgserver'}): "
          f"{'ALLE GRÜN' if not FAILS else f'{len(FAILS)} ROT: {FAILS}'}")
    return 0 if not FAILS else 2


if __name__ == "__main__":
    sys.exit(main())
