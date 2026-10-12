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
