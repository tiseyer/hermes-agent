"""B1.1 Welle-2 (notify) — faithful-Port-Tests. Dual-mode (pgserver | echtes PG).

    HERMES_KANBAN_PG_DSN=... PYTHONPATH=<repo> python tests/pg/test_notify.py

Muss-Fälle (Karte b1.1-port-notify): subscribe(upsert)+list; claim_unseen liefert neue
Events + schiebt Cursor; 2. Claim leer; neues Event → wieder 1; nebenläufiger Claim
derselben Sub (2 Prozesse) kein Doppel-Event (FOR UPDATE); kinds-Filter (faithfuler
Selektivitäts-Mechanismus — kanban_db hat KEINE delivery_mode-Ping-Logik, nur
last_event_id + kinds); unsubscribe entfernt.
"""
import os
import pathlib
import shutil
import sys
import tempfile
import multiprocessing as mp

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
        self._srv = None
        self._dir = None
        self._schema = None
        if PROD_DSN:
            self._schema = f"b1n_{name}_{os.getpid()}"
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


def _apply_schema(env, triggers=True):
    with env.connect(autocommit=True) as c, c.cursor() as cur:
        cur.execute((PKG / "schema.sql").read_text())
        if triggers:
            cur.execute((PKG / "triggers.sql").read_text())


def _claim_worker(uri, out_q):
    conn = psycopg.connect(uri, autocommit=False)
    b = PgBackend(conn)
    try:
        _old, _new, evs = b.claim_unseen_events_for_sub(
            task_id="T", platform="telegram", chat_id="c1")
        out_q.put(len(evs))
    except Exception:
        conn.rollback()
        out_q.put(-1)
    finally:
        conn.close()


def main():
    print(f"=== notify — Modus: {'ECHTES PG (Wegwerf-Schema)' if PROD_DSN else 'pgserver'} ===")
    e = Env("core")
    try:
        _apply_schema(e, triggers=True)
        conn = e.connect(autocommit=False)
        q = e.connect(autocommit=True)
        try:
            b = PgBackend(conn)
            b.create_task("T", "task", status="todo", created_at=100)

            print("[subscribe (upsert) + list + notifier_profile-backfill]")
            b.add_notify_sub(task_id="T", platform="telegram", chat_id="c1", user_id="u1")
            b.add_notify_sub(task_id="T", platform="telegram", chat_id="c1", user_id="u1")  # idempotent
            subs = b.list_notify_subs("T")
            check("list liefert genau 1 Sub (upsert idempotent)", len(subs) == 1, f"got {len(subs)}")
            # Backfill notifier_profile auf bestehende Zeile (nur wenn leer)
            b.add_notify_sub(task_id="T", platform="telegram", chat_id="c1",
                             notifier_profile="notifier")
            subs = b.list_notify_subs("T")
            check("notifier_profile-Backfill", subs[0]["notifier_profile"] == "notifier")

            print("[claim_unseen: neue Events + Cursor-Vorlauf]")
            b.add_event("T", "n1"); b.add_event("T", "n2")
            old, new, evs = b.claim_unseen_events_for_sub(task_id="T", platform="telegram", chat_id="c1")
            check("claim liefert 2 Events", len(evs) == 2, f"got {len(evs)}")
            check("Cursor vorgeschoben (new>old)", new > old)
            check("Events sind dicts mit kind", evs[0]["kind"] == "n1")
            o2, n2, evs2 = b.claim_unseen_events_for_sub(task_id="T", platform="telegram", chat_id="c1")
            check("2. Claim leer", evs2 == [] and o2 == n2)
            b.add_event("T", "n3")
            _o, _n, evs3 = b.claim_unseen_events_for_sub(task_id="T", platform="telegram", chat_id="c1")
            check("nach neuem Event wieder 1", len(evs3) == 1 and evs3[0]["kind"] == "n3")

            print("[unseen_events_for_sub: read-only (kein Vorlauf)]")
            b.add_event("T", "n4")
            mx, evsr = b.unseen_events_for_sub(task_id="T", platform="telegram", chat_id="c1")
            check("unseen liefert n4", len(evsr) == 1 and evsr[0]["kind"] == "n4")
            mx2, evsr2 = b.unseen_events_for_sub(task_id="T", platform="telegram", chat_id="c1")
            check("unseen schiebt NICHT vor (2. Lesen gleich)", len(evsr2) == 1)

            print("[kinds-Filter (faithfuler Selektivitäts-Mechanismus)]")
            b.add_notify_sub(task_id="T", platform="telegram", chat_id="cK")
            b.add_event("T", "heartbeat"); b.add_event("T", "done_marker")
            _ok, _nk, kev = b.claim_unseen_events_for_sub(
                task_id="T", platform="telegram", chat_id="cK", kinds=["done_marker"])
            check("kinds-Filter liefert nur gefilterte", [x["kind"] for x in kev] == ["done_marker"], f"got {kev}")

            print("[advance / rewind CAS]")
            b.add_notify_sub(task_id="T", platform="telegram", chat_id="cR")
            b.add_event("T", "r1")
            o, n, ev = b.claim_unseen_events_for_sub(task_id="T", platform="telegram", chat_id="cR")
            check("rewind CAS (claimed->old) True", b.rewind_notify_cursor(
                task_id="T", platform="telegram", chat_id="cR", claimed_cursor=n, old_cursor=o))
            _o2, _n2, ev2 = b.claim_unseen_events_for_sub(task_id="T", platform="telegram", chat_id="cR")
            check("nach rewind wieder claimbar", len(ev2) == len(ev) and len(ev2) >= 1)

            print("[unsubscribe entfernt]")
            check("remove True", b.remove_notify_sub(task_id="T", platform="telegram", chat_id="c1") is True)
            check("claim nach remove -> leer", b.claim_unseen_events_for_sub(
                task_id="T", platform="telegram", chat_id="c1") == (0, 0, []))
        finally:
            conn.close(); q.close()
    finally:
        e.teardown()

    print("[nebenläufig: 2 Prozesse claimen dieselbe Sub — kein Doppel-Event]")
    e2 = Env("conc")
    try:
        _apply_schema(e2, triggers=False)
        with e2.connect(autocommit=True) as c:
            c.execute("INSERT INTO tasks (id,title,status,created_at) VALUES ('T','t','todo',1)")
            c.execute("INSERT INTO kanban_notify_subs (task_id,platform,chat_id,thread_id,created_at) "
                      "VALUES ('T','telegram','c1','',1)")
            with c.cursor() as cur:
                with cur.copy("COPY task_events (task_id,kind,created_at) FROM STDIN") as cp:
                    for i in range(50):
                        cp.write_row(("T", f"e{i}", 1000 + i))
        ctx = mp.get_context("fork")
        qq = ctx.Queue()
        procs = [ctx.Process(target=_claim_worker, args=(e2.uri, qq)) for _ in range(2)]
        for p in procs:
            p.start()
        counts = sorted(qq.get() for _ in procs)
        for p in procs:
            p.join()
        check("kein Fehler in Workern", -1 not in counts, f"got {counts}")
        check("Summe == 50 (kein Doppel/Verlust)", sum(counts) == 50, f"got {counts}")
        check("genau EIN Claimer sah alle 50, der andere 0 (FOR UPDATE)",
              counts == [0, 50], f"got {counts}")
    finally:
        e2.teardown()

    print(f"\nTESTS notify ({'PROD-PG' if PROD_DSN else 'pgserver'}): "
          f"{'ALLE GRÜN' if not FAILS else f'{len(FAILS)} ROT: {FAILS}'}")
    return 0 if not FAILS else 2


if __name__ == "__main__":
    sys.exit(main())
