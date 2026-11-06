"""B1.1 Welle-3 — gemeinsames Test-Harness (Consumer-Ebene).

Anders als die PgBackend-Direkttests (test_dispatch/tasks/links/notify, die
``kanban_pg`` isoliert fahren) prüft Welle 3 die kanban_db-ROUTING-Schicht:
``connect()`` liefert im PG-Modus eine psycopg-Verbindung, die conn-Typ-Guards
routen automatisch. Deshalb setzen diese Tests ``HERMES_KANBAN_BACKEND=postgres``
+ ``HERMES_KANBAN_PG_DSN=<Wegwerf-Schema-URI>`` und rufen danach die
kanban_db-/tools-/watchers-/dashboard-Funktionen an.

Dual-mode: echtes PG via ``HERMES_KANBAN_PG_DSN`` (Wegwerf-Schema, public bleibt
leer) oder pgserver-Fallback.
"""
import os
import pathlib
import shutil
import sys
import tempfile

import psycopg

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
PKG = REPO / "kanban_pg"
PROD_DSN = os.environ.get("HERMES_KANBAN_PG_DSN", "").strip() or None


class Check:
    def __init__(self):
        self.fails = []

    def __call__(self, name, cond, detail=""):
        print(f"  [{'OK' if cond else 'FAIL'}] {name}"
              + (f" — {detail}" if detail and not cond else ""))
        if not cond:
            self.fails.append(name)


class PgEnv:
    """Wegwerf-Schema (oder pgserver). Liefert eine schema-gescopte URI, die
    via search_path isoliert — ``public`` bleibt unberührt."""

    def __init__(self, name):
        self.name = name
        self._srv = None
        self._dir = None
        self._schema = None
        if PROD_DSN:
            self._schema = f"b1w3_{name}_{os.getpid()}"
            with psycopg.connect(PROD_DSN, autocommit=True) as c:
                c.execute(f'DROP SCHEMA IF EXISTS "{self._schema}" CASCADE')
                c.execute(f'CREATE SCHEMA "{self._schema}"')
            sep = "&" if "?" in PROD_DSN else "?"
            self.uri = (f"{PROD_DSN}{sep}options=-c%20search_path%3D"
                        f"{self._schema}%2Cpublic")
        else:
            xdg = pathlib.Path(tempfile.mkdtemp(prefix="xdg-"))
            os.environ.setdefault("XDG_RUNTIME_DIR", str(xdg))
            import pgserver
            self._dir = pathlib.Path(tempfile.mkdtemp(prefix="pgdata-"))
            self._srv = pgserver.get_server(self._dir)
            self.uri = self._srv.get_uri()

    def apply_schema(self, triggers=True):
        with psycopg.connect(self.uri, autocommit=True) as c, c.cursor() as cur:
            cur.execute((PKG / "schema.sql").read_text())
            if triggers:
                cur.execute((PKG / "triggers.sql").read_text())

    def connect(self, autocommit=False):
        return psycopg.connect(self.uri, autocommit=autocommit)

    def activate_routing(self):
        """Schaltet kanban_db.connect() auf diesen Store (Env-first, validiert)."""
        os.environ["HERMES_KANBAN_BACKEND"] = "postgres"
        os.environ["HERMES_KANBAN_PG_DSN"] = self.uri

    def teardown(self):
        os.environ.pop("HERMES_KANBAN_BACKEND", None)
        os.environ.pop("HERMES_KANBAN_PG_DSN", None)
        if PROD_DSN:
            with psycopg.connect(PROD_DSN, autocommit=True) as c:
                c.execute(f'DROP SCHEMA IF EXISTS "{self._schema}" CASCADE')
        else:
            self._srv.cleanup()
            shutil.rmtree(self._dir, ignore_errors=True)


def mode_banner(domain):
    return (f"=== {domain} — Modus: "
            f"{'ECHTES PG (Wegwerf-Schema)' if PROD_DSN else 'pgserver'} ===")
