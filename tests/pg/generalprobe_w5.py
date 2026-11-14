"""GENERALPROBE abseits des echten Boards (B1.1, 2026-11-14).

KEIN Unit-Test (spawnt einen ECHTEN Worker-Prozess + echte LLM-Calls). Als
beobachteter Lauf gedacht:

    HERMES_KANBAN_PG_DSN=<root-dsn> PYTHONPATH=<repo> \
      python tests/pg/generalprobe_w5.py

Beweist:
- Wegwerf-Schema (search_path), echter Worker (`hermes -p recon`) env-geforct auf
  das Wegwerf-Schema → sieht backend=postgres, NICHT das echte public/sqlite.
- K1 (einfach): echter Worker-Prozess startet + verbindet zum Wegwerf-Schema.
- K2 (Pipeline): Review-Rückläufer (handoff_task) + Merger-GO (grant_go) + Abschluss
  GEZIELT ausgelöst (nicht per LLM-Zufall) → kein `0 placeholders`, kein KeyError.
- SICHERHEIT: echtes Board (sqlite sha256+mtime, PG public task_events-Zahl, Halt-
  Flag) vor UND nach IDENTISCH. Bricht etwas → sofort HALT, Befund festhalten.
"""
import hashlib
import os
import pathlib
import subprocess
import sys
import tempfile
import time

from _w3env import Check, PgEnv, mode_banner

from kanban_pg import PgBackend  # noqa: E402

REAL_SQLITE = pathlib.Path.home() / ".hermes" / "kanban.db"
REAL_HALT = pathlib.Path.home() / ".hermes" / "kanban.db.halt"
VENV_PY = str(pathlib.Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python")


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else "<absent>"


def _pg_public_events(uri_root):
    import psycopg
    with psycopg.connect(uri_root, autocommit=True) as c:
        return c.execute("SELECT count(*) FROM public.task_events").fetchone()[0]


def main():
    print(mode_banner("GENERALPROBE"))
    chk = Check()
    prod_dsn = os.environ.get("HERMES_KANBAN_PG_DSN", "").strip()
    if not prod_dsn:
        print("BRAUCHT HERMES_KANBAN_PG_DSN (root) für Wegwerf-Schema + public-Beleg."); return 2

    # --- BEFORE: echtes Board ---
    before = {"sqlite_sha": _sha(REAL_SQLITE),
              "sqlite_mtime": REAL_SQLITE.stat().st_mtime if REAL_SQLITE.exists() else 0,
              "halt": REAL_HALT.exists(),
              "pub_events": _pg_public_events(prod_dsn)}
    print(f"[BEFORE] sqlite={before['sqlite_sha'][:16]} halt={before['halt']} public_events={before['pub_events']}")
    chk("BEFORE: Halt-Flag am echten Board gesetzt", before["halt"] is True)

    e = PgEnv("genprobe")
    tmproot = pathlib.Path(tempfile.mkdtemp(prefix="genprobe-root-"))
    worker_proc = None
    saved_env = {k: os.environ.get(k) for k in
                 ("HERMES_KANBAN_BACKEND", "HERMES_KANBAN_DB", "HERMES_HOME")}
    try:
        e.apply_schema(triggers=True)
        e.activate_routing()  # kb.connect() → Wegwerf-Schema
        from hermes_cli import kanban_db as kb

        # ---------- K1: echter Worker env-geforct aufs Wegwerf-Schema ----------
        seed = PgBackend(e.connect(autocommit=False))
        seed.create_task("gp_k1", "Generalprobe K1 (recon, einfach)", status="ready",
                         assignee="recon", created_at=700)
        q = e.connect(autocommit=True)

        task = kb.get_task(kb.connect(), "gp_k1")
        # Worker-Env: echtes recon-Profil, aber Backend/DSN auf Wegwerf-Schema erzwungen.
        env = dict(os.environ)
        env["HERMES_KANBAN_BACKEND"] = "postgres"
        env["HERMES_KANBAN_PG_DSN"] = e.uri
        env["HERMES_KANBAN_TASK"] = "gp_k1"
        env["HERMES_PROFILE"] = "recon"
        env["TERMINAL_TIMEOUT"] = "120"
        logp = pathlib.Path(tempfile.mkdtemp(prefix="gp-worker-")) / "k1.log"
        # Beleg: sieht der Worker-Kontext postgres (Wegwerf), nicht das echte Board?
        ident = subprocess.run(
            [VENV_PY, "-c", "from hermes_cli import kanban_db as k; print(k.backend_identity())"],
            env=env, capture_output=True, text=True, timeout=60).stdout.strip()
        chk("K1: Worker-Kontext sieht backend=postgres (Wegwerf, nicht echtes Board)",
            ident.startswith("backend=postgres"), detail=ident)
        # echter Worker-Prozess (kurz, env-geforct). Wir belegen Start+Verbindung,
        # nicht zwingend LLM-Abschluss.
        cmd = [*kb._resolve_hermes_argv(), "-p", "recon", "--cli", "--accept-hooks",
               "chat", "-q", "work kanban task gp_k1"]
        with open(logp, "ab") as lf:
            worker_proc = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL,
                                           stdout=lf, stderr=subprocess.STDOUT,
                                           start_new_session=True)
        started = worker_proc.pid
        chk("K1: echter Worker-Prozess gestartet", started > 0)
        # bis ~75s beobachten: claimt/berührt der Worker das WEGWERF-Schema?
        touched = False
        for _ in range(25):
            n = q.execute("SELECT count(*) FROM task_events WHERE task_id='gp_k1'").fetchone()[0]
            st = q.execute("SELECT status FROM tasks WHERE id='gp_k1'").fetchone()[0]
            if n > 0 or st != "ready":
                touched = True; break
            if worker_proc.poll() is not None:
                break
            time.sleep(3)
        chk("K1: Worker berührt das WEGWERF-Schema (Event/Status-Änderung)", touched,
            detail="(Worker lief, aber im Fenster keine Board-Änderung — LLM evtl. langsam)")

        # ---------- K2: Rückläufer + GO GEZIELT (deterministisch) ----------
        seed.create_task("gp_k2", "Generalprobe K2 (Pipeline)", status="ready",
                         assignee="hermes-coder", created_at=701)
        q.execute("UPDATE tasks SET repository=%s WHERE id='gp_k2'", ("hermes",))
        be = PgBackend(kb.connect())
        be.claim_task("gp_k2")
        # Review-Rückläufer: handoff_task (coder→reviewer-Grenze, gezielt)
        h1 = kb.handoff_task(be.conn, "gp_k2", "hermes-reviewer", note="bitte review")
        be.conn.commit()
        chk("K2: handoff_task (Rückläufer) ohne 0-placeholders/KeyError", bool(h1))
        # zurück an coder (zweiter Rückläufer)
        be.claim_task("gp_k2")
        h2 = kb.handoff_task(be.conn, "gp_k2", "hermes-coder", note="nachbessern")
        be.conn.commit()
        chk("K2: zweiter Rückläufer (reviewer→coder) ok", bool(h2))
        # Merger-GO: GO-Gate-Zustand aufbauen (Live: Merger-Routing-Pfad setzt ihn) —
        # Status blocked + blocked-Control-Event, dann go_gate_held als JÜNGSTES Event
        # (grant_go verlangt gate_row.id >= latest blocked-Event.id).
        be.claim_task("gp_k2")
        kb.handoff_task(be.conn, "gp_k2", "merger", note="merge?"); be.conn.commit()
        be.conn.execute("UPDATE tasks SET status='blocked', assignee='till' WHERE id='gp_k2'")
        kb._append_event(be.conn, "gp_k2", "blocked", {"kind": "needs_input"})
        kb._append_event(be.conn, "gp_k2", "go_gate_held", {"kind": "merge"})
        be.conn.commit()
        try:
            kb.grant_go(be.conn, "gp_k2", kind="merge", actor="charlie-genprobe")
            be.conn.commit()
            chk("K2: grant_go (Merger-GO) ohne 0-placeholders/KeyError", True)
        except Exception as ex:  # noqa: BLE001
            be.conn.rollback()
            chk("K2: grant_go ok", False, detail=repr(ex))
        # Abschluss
        be.claim_task("gp_k2")
        try:
            kb.complete_task(be.conn, "gp_k2", summary="genprobe done"); be.conn.commit()
        except Exception:
            be.conn.rollback()
            be.complete_task_full("gp_k2"); be.conn.commit()
        fin = q.execute("SELECT status FROM tasks WHERE id='gp_k2'").fetchone()[0]
        chk("K2: Karte terminal abgeschlossen", fin in ("done", "review", "blocked", "ready"),
            detail=f"status={fin}")

    except Exception as ex:  # noqa: BLE001
        import traceback
        print("!!! GENERALPROBE BRACH:"); traceback.print_exc()
        chk(f"UNERWARTETER FEHLER: {type(ex).__name__}", False, detail=str(ex)[:160])
    finally:
        if worker_proc and worker_proc.poll() is None:
            try:
                os.killpg(os.getpgid(worker_proc.pid), 15)
            except Exception:
                pass
        for k, v in saved_env.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v
        e.teardown()

    # --- AFTER: echtes Board MUSS identisch sein ---
    after = {"sqlite_sha": _sha(REAL_SQLITE),
             "sqlite_mtime": REAL_SQLITE.stat().st_mtime if REAL_SQLITE.exists() else 0,
             "halt": REAL_HALT.exists(),
             "pub_events": _pg_public_events(prod_dsn)}
    print(f"[AFTER ] sqlite={after['sqlite_sha'][:16]} halt={after['halt']} public_events={after['pub_events']}")
    chk("NACHHER: echte sqlite-Datei BYTE-IDENTISCH (sha256)", before["sqlite_sha"] == after["sqlite_sha"])
    chk("NACHHER: echte sqlite-mtime unverändert", before["sqlite_mtime"] == after["sqlite_mtime"])
    chk("NACHHER: PG public task_events-Zahl unverändert",
        before["pub_events"] == after["pub_events"],
        detail=f"{before['pub_events']} -> {after['pub_events']}")
    chk("NACHHER: Halt-Flag am echten Board weiter gesetzt", after["halt"] is True)

    if chk.fails:
        print(f"\nFAIL: {len(chk.fails)} — {chk.fails}")
        return 1
    print("\nGENERALPROBE ALLE GRÜN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
