"""B1.1 Lücke 1 — Profile erben das Kanban-Backend aus dem Instanz-Root (echtes PG).

Befund 2026-11-14: `backend: postgres` stand nur in der Root-config.yaml. Ein Worker
läuft mit `HERMES_HOME=<root>/profiles/<rolle>`; `_kanban_root_config()` las via
`load_config()` die Profil-Config (kein kanban.backend) → SQLite, während die Instanz
auf PG lief (Split-Brain). Fix: `_kanban_root_config()` liest die EINE Quelle im
Instanz-Root (`get_default_hermes_root()`), Profile erben; plus Fail-closed-Abbruch.

Dieser Test baut ein ECHTES Profil-Layout (Root-config.yaml + profiles/recon/config.yaml
OHNE kanban.backend), setzt HERMES_HOME aufs Profil (wie `_default_spawn`), und prüft gegen
ein ECHTES Postgres-Wegwerf-Schema, dass der „Worker" `backend=postgres` erbt. KEINE Env-
Overrides (HERMES_KANBAN_BACKEND/_PG_DSN bleiben leer) — die Config allein entscheidet.

    PYTHONPATH=<repo> HERMES_KANBAN_PG_DSN=<root-dsn> python tests/pg/test_profile_backend_inherit_w5.py
"""
import os
import pathlib
import sys
import tempfile

from _w3env import Check, PgEnv, mode_banner


def _write(p: pathlib.Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def main():
    print(mode_banner("profile-backend-inherit"))
    chk = Check()
    e = PgEnv("inherit")
    saved = {k: os.environ.get(k) for k in
             ("HERMES_HOME", "HERMES_KANBAN_BACKEND", "HERMES_KANBAN_PG_DSN")}
    try:
        e.apply_schema(triggers=True)

        # ECHTES Profil-Layout: Root-config sagt postgres (DSN = Wegwerf-Schema);
        # das Rollen-Profil hat KEINE kanban-Sektion (würde für sich sqlite ergeben).
        root = pathlib.Path(tempfile.mkdtemp(prefix="inst-root-"))
        _write(root / "config.yaml",
               f"kanban:\n  backend: postgres\n  pg_dsn: \"{e.uri}\"\n")
        _write(root / "profiles" / "recon" / "config.yaml",
               "agent:\n  name: recon\n")  # bewusst OHNE kanban.backend

        # Env säubern: die CONFIG soll allein entscheiden (kein Env-Override).
        os.environ.pop("HERMES_KANBAN_BACKEND", None)
        os.environ.pop("HERMES_KANBAN_PG_DSN", None)
        # Worker-Sicht: HERMES_HOME zeigt aufs Profil (wie _default_spawn es setzt).
        os.environ["HERMES_HOME"] = str(root / "profiles" / "recon")

        from hermes_cli import kanban_db as kb
        kb._PG_CFG_CACHE = kb._PG_CFG_UNSET  # Prozess-Cache leeren nach Env-Wechsel

        # Gegenprobe: die PROFIL-Config allein trägt KEIN postgres.
        import yaml
        prof = yaml.safe_load((root / "profiles" / "recon" / "config.yaml").read_text())
        chk("Profil-Config hat KEIN kanban.backend (ohne Fix wäre es sqlite)",
            "kanban" not in (prof or {}))

        # Kern: der Worker erbt das Backend aus dem Instanz-Root.
        cfg = kb._kanban_root_config()
        chk("_kanban_root_config liest INSTANZ-ROOT (postgres)",
            (cfg.get("kanban") or {}).get("backend") == "postgres", detail=str(cfg)[:120])
        chk("_kanban_is_postgres() == True unter Profil-HERMES_HOME",
            kb._kanban_is_postgres() is True)
        ident = kb.backend_identity()
        chk("backend_identity() == postgres (echte PG-Verbindung ins Wegwerf-Schema)",
            ident.startswith("backend=postgres"), detail=ident)

        # Fail-closed B: Instanz-Root=postgres, kein Env-Override, aber SQLite voraus → Abbruch.
        try:
            kb._guard_no_silent_sqlite_on_pg_instance()
            chk("Fail-closed wirft bei root=postgres + kein Env-Override", False)
        except RuntimeError as ex:
            chk("Fail-closed wirft RuntimeError mit klarer Meldung",
                "Split-Brain" in str(ex) and "postgres" in str(ex))

        # Explizite Env-Wahl wird geehrt (kein Abbruch) — Tests/Operator dürfen sqlite.
        os.environ["HERMES_KANBAN_BACKEND"] = "sqlite"
        try:
            kb._guard_no_silent_sqlite_on_pg_instance()
            chk("explizites HERMES_KANBAN_BACKEND=sqlite → KEIN Abbruch (geehrt)", True)
        except RuntimeError:
            chk("explizites HERMES_KANBAN_BACKEND=sqlite → KEIN Abbruch (geehrt)", False)
        os.environ.pop("HERMES_KANBAN_BACKEND", None)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        try:
            from hermes_cli import kanban_db as kb
            kb._PG_CFG_CACHE = kb._PG_CFG_UNSET
        except Exception:
            pass
        e.teardown()

    if chk.fails:
        print(f"\nFAIL: {len(chk.fails)} — {chk.fails}")
        return 1
    print("\nALLE GRÜN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
