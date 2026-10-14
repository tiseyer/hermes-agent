"""S1 — geplanter Till-Schritt ("spaeter") und Kategorie-Nachtrag.

Inbox-Konzept (Till 2026-10): "Human-in-the-Loop" != "braucht Till JETZT".
Ein geplanter Schritt wartet in todo (child_role human_check) auf seine
Vorgaenger und wird erst dann human — nie ready (kein Agent darf ihn ziehen).
Bereits wartende Karten bekommen per --category ihre Kategorie nachgetragen.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB (wie test_kanban_db.py)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _done(conn, tid):
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    conn.commit()


def test_kategorie_nachtragen_auf_wartender_karte(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="default")
        kb.block_task(conn, tid, kind="needs_input")
        assert kb.move_to_human(conn, tid) == "moved"
        assert kb.get_task(conn, tid).category is None
        assert kb.move_to_human(conn, tid, category="frage") == "categorized"
        assert kb.get_task(conn, tid).category == "frage"
        # gleiche Kategorie / keine Kategorie = No-op, kein weiteres Event
        assert kb.move_to_human(conn, tid, category="frage") == "noop"
        assert kb.move_to_human(conn, tid) == "noop"
        n = conn.execute(
            "SELECT COUNT(*) c FROM task_events WHERE task_id=? AND kind='categorized'",
            (tid,),
        ).fetchone()["c"]
        assert n == 1
        assert kb.get_task(conn, tid).status == "human"


def test_planen_mit_offenem_vorgaenger_wird_spaeter(kanban_home):
    with kb.connect() as conn:
        vor = kb.create_task(conn, title="Implementierung", assignee="default")
        till = kb.create_task(conn, title="Merge-GO", assignee="default")
        kb.block_task(conn, till, kind="needs_input")
        kb.move_to_human(conn, till)
        out = kb.plan_human_step(conn, till, after=[vor], category="go_noetig")
        assert out == "planned"
        t = kb.get_task(conn, till)
        assert t.status == "todo"
        assert t.child_role == "human_check"
        assert t.category == "go_noetig"
        link = conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?", (vor, till)
        ).fetchone()
        assert link is not None


def test_recompute_ready_macht_faelligen_schritt_human_nie_ready(kanban_home):
    with kb.connect() as conn:
        vor = kb.create_task(conn, title="Implementierung", assignee="default")
        till = kb.create_task(conn, title="Abnahme", assignee="default")
        assert kb.plan_human_step(conn, till, after=[vor], category="abnahme") == "planned"
        # Vorgaenger offen: bleibt geplant, wird NICHT ready
        kb.recompute_ready(conn)
        assert kb.get_task(conn, till).status == "todo"
        _done(conn, vor)
        kb.recompute_ready(conn)
        t = kb.get_task(conn, till)
        assert t.status == "human"
        assert t.category == "abnahme"
        ev = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='human' "
            "ORDER BY id DESC LIMIT 1", (till,),
        ).fetchone()["payload"]
        assert "geplanter Till-Schritt" in ev


def test_planen_ohne_offenen_vorgaenger_eskaliert_sofort(kanban_home):
    with kb.connect() as conn:
        vor = kb.create_task(conn, title="fertig", assignee="default")
        _done(conn, vor)
        till = kb.create_task(conn, title="Frage", assignee="default")
        assert kb.plan_human_step(conn, till, after=[vor], category="frage") == "moved"
        t = kb.get_task(conn, till)
        assert t.status == "human" and t.category == "frage"


def test_planen_verweigert_laufende_unbekannte_und_terminale(kanban_home):
    with kb.connect() as conn:
        vor = kb.create_task(conn, title="v", assignee="default")
        tid = kb.create_task(conn, title="x", assignee="default")
        assert kb.plan_human_step(conn, tid, after=["t_gibtsnicht"]) == "unknown_parent"
        assert kb.get_task(conn, tid).child_role != "human_check"
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
        conn.commit()
        assert kb.plan_human_step(conn, tid, after=[vor]) == "busy"
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        conn.commit()
        assert kb.plan_human_step(conn, tid, after=[vor]) == "terminal"
        assert kb.plan_human_step(conn, "t_nope", after=[vor]) == "notfound"


def test_normales_todo_wird_weiter_ready(kanban_home):
    # Gegenprobe: die Sonderregel greift NUR fuer human_check.
    with kb.connect() as conn:
        vor = kb.create_task(conn, title="v", assignee="default")
        _done(conn, vor)
        arbeit = kb.create_task(conn, title="Arbeit", assignee="default", parents=[vor])
        kb.recompute_ready(conn)
        assert kb.get_task(conn, arbeit).status == "ready"


def test_blocked_zielfrage_bleibt_blocked_statt_human(kanban_home):
    # F38: eine Karte, die als needs_input in 'blocked' liegt UND eine echte
    # Geschaefts-Zielfrage (Kommentar mit '?', kein Technik-Begriff) traegt, ist
    # bereits der Eimer, den der Beobachter fuer blocked_zielfrage liest. Der
    # Manager darf sie NICHT nach 'human' ziehen (sonst rot-stuck
    # "Zielfrage-Karte haengt in human"). Sie bleibt blocked.
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Tier-System aufloesen", assignee="specifier")
        kb.block_task(conn, tid, kind="needs_input",
                      reason="brauche eine Entscheidung")
        kb.add_comment(
            conn, tid, "specifier",
            "Brauche Klaerung: Tier-System ganz entfernen oder auf zwei "
            "Stufen reduzieren? Mit Folgen fuer das Datenmodell und "
            "bestehende Mandanten.",
        )
        # Manager-Eskalation wird verweigert — Karte bleibt blocked.
        assert kb.move_to_human(conn, tid, category="entscheidung") == "stays_blocked"
        t = kb.get_task(conn, tid)
        assert t.status == "blocked"
        # Kategorie wird in place nachgetragen, Status bleibt blocked.
        assert t.category == "entscheidung"
        # Kein 'human'-Event.
        n_human = conn.execute(
            "SELECT COUNT(*) c FROM task_events WHERE task_id=? AND kind='human'",
            (tid,),
        ).fetchone()["c"]
        assert n_human == 0
        # Wiederholter Aufruf bleibt no-op (keine weiteren categorized-Events).
        assert kb.move_to_human(conn, tid, category="entscheidung") == "stays_blocked"
        n_cat = conn.execute(
            "SELECT COUNT(*) c FROM task_events WHERE task_id=? AND kind='categorized'",
            (tid,),
        ).fetchone()["c"]
        assert n_cat == 1


def test_blocked_technische_rueckfrage_eskaliert_weiter_human(kanban_home):
    # Gegenprobe: eine needs_input-Karte mit einer rein TECHNISCHEN Rueckfrage
    # (Technik-Begriff im Kommentar) ist KEINE legitime Zielfrage; der
    # Beobachter wuerde sie ohnehin nicht als blocked_zielfrage gruenen, also
    # darf der Manager sie weiter nach human eskalieren.
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="specifier")
        kb.block_task(conn, tid, kind="needs_input", reason="frage")
        kb.add_comment(conn, tid, "specifier",
                       "Welche library-version soll ich nehmen?")
        assert kb.move_to_human(conn, tid, category="frage") == "moved"
        assert kb.get_task(conn, tid).status == "human"


def test_blocked_capability_wall_eskaliert_weiter_human(kanban_home):
    # Gegenprobe: ein harter capability-Block (Zugang/Creds fehlen) ist eine
    # menschliche AKTION, keine Zielfrage — auch mit '?'-Kommentar wird er
    # weiter nach human eskaliert.
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="specifier")
        kb.block_task(conn, tid, kind="capability", reason="kein Zugang")
        kb.add_comment(conn, tid, "specifier",
                       "Soll der Shop oeffentlich sichtbar sein?")
        assert kb.move_to_human(conn, tid, category="blocker") == "moved"
        assert kb.get_task(conn, tid).status == "human"


def _parser():
    import argparse
    from hermes_cli import kanban as kc
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest="cmd")
    kc.build_parser(sub)
    return root, kc


def test_cli_human_after_plant_und_kategorie_nachtragen(kanban_home, capsys):
    root, kc = _parser()
    with kb.connect() as conn:
        vor = kb.create_task(conn, title="Implementierung", assignee="default")
        till = kb.create_task(conn, title="Merge-GO", assignee="default")
        warte = kb.create_task(conn, title="Frage", assignee="default")
        kb.block_task(conn, warte, kind="needs_input")
        kb.move_to_human(conn, warte)
    rc = kc.kanban_command(root.parse_args(
        ["kanban", "human", till, "--after", vor, "--category", "go_noetig"]))
    assert rc == 0
    assert "Planned" in capsys.readouterr().out
    rc = kc.kanban_command(root.parse_args(
        ["kanban", "human", warte, "--category", "frage"]))
    assert rc == 0
    assert "category set to frage" in capsys.readouterr().out
    with kb.connect() as conn:
        assert kb.get_task(conn, till).status == "todo"
        assert kb.get_task(conn, till).child_role == "human_check"
        assert kb.get_task(conn, warte).category == "frage"


# --------------------------------------------------------------------------
# F45 — an ACTIVE work card escalated for an owner DECISION must land in
# blocked@till (blocked_zielfrage, rule b), NOT in the dead ``human`` bucket
# (which the Beobachter flags rot-stuck after the frist). Symmetric companion
# to F38 (which keeps an ALREADY-blocked Zielfrage in blocked). Live Karte 18
# t_1fb31d56: a scope/abnahme decision got human-escalated and rotted > 1h.
# --------------------------------------------------------------------------
def _ready(conn, tid):
    conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    conn.commit()


def test_active_decision_reroutes_to_blocked_zielfrage(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="Ziel für vollständige deutsche UI klären",
            body="Bitte festlegen: neues vollständiges Lokalisierungsvorhaben "
                 "oder den bisherigen breiten Stand abschließen.",
            assignee="orchestrator",
        )
        _ready(conn, tid)
        out = kb.move_to_human(conn, tid, category="entscheidung")
        assert out == "rerouted_blocked"
        t = kb.get_task(conn, tid)
        # Landed in the observer's green owner-decision bucket, not human.
        assert t.status == "blocked"
        assert t.assignee == "till"
        assert t.block_kind == "needs_input"
        assert t.category == "entscheidung"
        # A non-technical ?-Zielfrage is present for the observer's rule b.
        frage = conn.execute(
            "SELECT body FROM task_comments WHERE task_id=? AND body LIKE '%?%' "
            "ORDER BY id DESC LIMIT 1", (tid,),
        ).fetchone()
        assert frage is not None and "ZIELENTSCHEID" in frage["body"]
        # No ``human`` event — the card never touched the dead bucket.
        n_human = conn.execute(
            "SELECT COUNT(*) c FROM task_events WHERE task_id=? AND kind='human'",
            (tid,),
        ).fetchone()["c"]
        assert n_human == 0
        # Sticky blocked event → recompute_ready must not auto-promote it.
        kb.recompute_ready(conn)
        assert kb.get_task(conn, tid).status == "blocked"
        # Re-invocation is now caught by the F38 guard — safe no-op.
        assert kb.move_to_human(conn, tid, category="entscheidung") == "stays_blocked"


def test_active_decision_reuses_prepared_zielfrage(kanban_home):
    # A card that already carries a prepared non-technical Zielfrage keeps it —
    # no synthesised comment is appended on reroute.
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Abnahme-GO", assignee="orchestrator")
        _ready(conn, tid)
        kb.add_comment(
            conn, tid, "orchestrator",
            "VORBEREITETER ZIELENTSCHEID: Soll der erreichte Stand abgenommen "
            "und freigegeben werden, oder ein neues Vorhaben aufgesetzt werden?",
        )
        n_before = conn.execute(
            "SELECT COUNT(*) c FROM task_comments WHERE task_id=?", (tid,),
        ).fetchone()["c"]
        assert kb.move_to_human(conn, tid, category="abnahme") == "rerouted_blocked"
        n_after = conn.execute(
            "SELECT COUNT(*) c FROM task_comments WHERE task_id=?", (tid,),
        ).fetchone()["c"]
        # No synthesised Zielfrage — the prepared one is reused.
        assert n_after == n_before
        assert kb.get_task(conn, tid).status == "blocked"


def test_active_blocker_still_escalates_to_human(kanban_home):
    # Gegenprobe: ``blocker`` is a pure impediment (not a question for Till) —
    # it is NOT a decision category, so it keeps the human path.
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Zugang fehlt", assignee="orchestrator")
        _ready(conn, tid)
        assert kb.move_to_human(conn, tid, category="blocker") == "moved"
        assert kb.get_task(conn, tid).status == "human"


def test_planned_human_check_step_still_uses_human(kanban_home):
    # Gegenprobe: a planned Till step (child_role human_check) is Till's inbox by
    # design and must still reach ``human`` even with a decision category.
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="Geplante Abnahme", assignee="default",
            child_role="human_check",
        )
        _ready(conn, tid)
        assert kb.move_to_human(conn, tid, category="abnahme") == "moved"
        assert kb.get_task(conn, tid).status == "human"


def test_cli_human_on_active_decision_routes_to_blocked(kanban_home, capsys):
    root, kc = _parser()
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="Ziel für vollständige deutsche UI klären",
            body="neues vollständiges Lokalisierungsvorhaben oder den "
                 "bisherigen breiten Stand abschließen.",
            assignee="orchestrator",
        )
        _ready(conn, tid)
    rc = kc.kanban_command(root.parse_args(
        ["kanban", "human", tid, "--category", "entscheidung",
         "--reason", "Abnahmeziel widerspricht der begründeten Sperre."]))
    assert rc == 0
    assert "blocked@till" in capsys.readouterr().out
    with kb.connect() as conn:
        t = kb.get_task(conn, tid)
        assert t.status == "blocked" and t.block_kind == "needs_input"
        # The rationale comment is carried onto the card for context.
        assert conn.execute(
            "SELECT 1 FROM task_comments WHERE task_id=? AND body LIKE 'HUMAN:%'",
            (tid,),
        ).fetchone() is not None
