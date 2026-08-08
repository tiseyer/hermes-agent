"""Fokusmodus: Task-Hierarchie (parent_id/initiative_id) + Initiative-Rollups.

Deckt die Kernzusagen des Fokusmodus ab:

* Hierarchie-Datenmodell: ``create_task(parent_task_id=..., initiative_id=...)``
  inkl. Ableitung der Initiative aus dem Parent (auch über mehrere Ebenen),
  Validierung unbekannter Ids und Unabhängigkeit vom Dependency-Graphen.
* Auto-Decompose: Children erben ``parent_id``/``initiative_id`` vom Root.
* ``initiative_rollups``: Fortschritt/Zähler/aggregierter Status, GO-Gate →
  ``blocked`` (menschliches Signal schlägt running), fertige Initiativen →
  ``done``.
* Dependency-Abgrenzung: ``task_links``-Kanten (depends_on) verändern weder
  Mitgliedschaft noch Aggregation — Hierarchie und Scheduling sind orthogonal.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    c = kb.connect()
    yield c
    c.close()


def _mk_initiative(conn, n_children=3, tenant="voicera"):
    root = kb.create_task(conn, title="Hauptaufgabe", tenant=tenant, triage=True)
    kids = [
        kb.create_task(conn, title=f"Schritt {i}", parent_task_id=root)
        for i in range(n_children)
    ]
    return root, kids


def _hold_go(conn, task_id):
    """Simuliert die durable GO-Gate-Repräsentation des Dispatchers:
    assignee='till' + go_gate_held-Event (siehe _dispatch_once_locked)."""
    conn.execute(
        "UPDATE tasks SET status='ready', assignee='till' WHERE id=?", (task_id,)
    )
    conn.execute(
        "INSERT INTO task_events(task_id, kind, payload, created_at) "
        "VALUES (?, 'go_gate_held', '{}', ?)",
        (task_id, int(time.time())),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Hierarchie-Datenmodell
# ---------------------------------------------------------------------------


class TestHierarchyModel:
    def test_parent_sets_initiative_from_root(self, conn):
        root, kids = _mk_initiative(conn)
        for kid in kids:
            t = kb.get_task(conn, kid)
            assert t.parent_id == root
            assert t.initiative_id == root

    def test_grandchild_inherits_root_initiative(self, conn):
        root, kids = _mk_initiative(conn, 1)
        grandchild = kb.create_task(conn, title="Enkel", parent_task_id=kids[0])
        t = kb.get_task(conn, grandchild)
        assert t.parent_id == kids[0]
        assert t.initiative_id == root  # Wurzel, nicht der direkte Parent

    def test_initiative_without_parent_hangs_under_root(self, conn):
        root, _ = _mk_initiative(conn, 1)
        member = kb.create_task(conn, title="Direktmitglied", initiative_id=root)
        t = kb.get_task(conn, member)
        assert t.parent_id == root
        assert t.initiative_id == root

    def test_unknown_hierarchy_ids_rejected(self, conn):
        with pytest.raises(ValueError, match="hierarchy parent"):
            kb.create_task(conn, title="x", parent_task_id="t_nope")
        with pytest.raises(ValueError, match="initiative"):
            kb.create_task(conn, title="x", initiative_id="t_nope")

    def test_hierarchy_does_not_create_dependency_links(self, conn):
        """--parent-task strukturiert nur; die Karte bleibt sofort ready."""
        root, kids = _mk_initiative(conn, 1)
        t = kb.get_task(conn, kids[0])
        assert t.status == "ready"  # kein task_links-Eintrag => keine Wartekante
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM task_links WHERE child_id=?", (kids[0],)
        ).fetchone()
        assert rows["n"] == 0

    def test_dependency_parents_do_not_set_hierarchy(self, conn):
        """Bestehende parents=(...)-Semantik (Scheduling) bleibt hierarchiefrei."""
        a = kb.create_task(conn, title="A")
        b = kb.create_task(conn, title="B", parents=(a,))
        t = kb.get_task(conn, b)
        assert t.parent_id is None
        assert t.initiative_id is None
        assert t.status == "todo"  # Dependency-Gating unverändert


# ---------------------------------------------------------------------------
# Auto-Decompose-Vererbung
# ---------------------------------------------------------------------------


class TestDecomposeInheritance:
    def test_children_inherit_root_hierarchy(self, conn):
        root = kb.create_task(conn, title="Initiative", tenant="voicera", triage=True)
        kids = kb.decompose_triage_task(
            conn, root, root_assignee="orchestrator",
            children=[
                {"title": "Recon"},
                {"title": "Code", "parents": [0]},
                {"title": "Review", "parents": [1]},
            ],
            author="auto-decomposer",
        )
        assert kids and len(kids) == 3
        for kid in kids:
            t = kb.get_task(conn, kid)
            assert t.parent_id == root
            assert t.initiative_id == root

    def test_nested_decompose_keeps_root_initiative(self, conn):
        root = kb.create_task(conn, title="Initiative", triage=True)
        (mid,) = kb.decompose_triage_task(
            conn, root, root_assignee="orchestrator",
            children=[{"title": "Teilprojekt"}], author="d",
        )
        # Das Child selbst wird erneut zerlegt.
        conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (mid,))
        conn.commit()
        (leaf,) = kb.decompose_triage_task(
            conn, mid, root_assignee="orchestrator",
            children=[{"title": "Blatt"}], author="d",
        )
        t = kb.get_task(conn, leaf)
        assert t.parent_id == mid
        assert t.initiative_id == root


# ---------------------------------------------------------------------------
# Rollup-Aggregation
# ---------------------------------------------------------------------------


class TestInitiativeRollups:
    def test_counts_and_progress(self, conn):
        root, kids = _mk_initiative(conn, 4)
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (kids[0],))
        conn.execute(
            "UPDATE tasks SET status='running', assignee='voicera-coder' WHERE id=?",
            (kids[1],),
        )
        conn.commit()
        r = kb.initiative_rollups(conn)[root]
        assert r["total"] == 4 and r["done"] == 1
        assert r["running"] == 1 and r["waiting"] == 2
        assert r["active"][0]["title"] == "Schritt 1"
        assert r["active"][0]["assignee"] == "voicera-coder"
        assert r["agg_status"] == "running"

    def test_go_gate_sets_blocked_even_while_running(self, conn):
        """Verify-Bedingung: Child mit GO-Bedarf ⇒ Hauptaufgabe Blocked —
        auch wenn parallel ein anderes Child läuft (Mensch schlägt Maschine)."""
        root, kids = _mk_initiative(conn, 3)
        conn.execute(
            "UPDATE tasks SET status='running', assignee='voicera-coder' WHERE id=?",
            (kids[0],),
        )
        conn.commit()
        _hold_go(conn, kids[1])
        r = kb.initiative_rollups(conn)[root]
        assert r["needs_go"] == 1
        assert r["go_titles"] == ["Schritt 1"]
        assert r["agg_status"] == "blocked"

    def test_go_release_unblocks(self, conn):
        """Nach menschlichem GO (Reassign) zählt die Karte nicht mehr als gated."""
        root, kids = _mk_initiative(conn, 2)
        _hold_go(conn, kids[0])
        assert kb.initiative_rollups(conn)[root]["agg_status"] == "blocked"
        conn.execute(
            "UPDATE tasks SET assignee='voicera-coder' WHERE id=?", (kids[0],)
        )
        conn.commit()
        r = kb.initiative_rollups(conn)[root]
        assert r["needs_go"] == 0
        assert r["agg_status"] is None  # keine Signale => Root-Status zählt

    def test_all_done_aggregates_done(self, conn):
        root, kids = _mk_initiative(conn, 3)
        for kid in kids:
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (kid,))
        conn.commit()
        r = kb.initiative_rollups(conn)[root]
        assert r["done"] == r["total"] == 3
        assert r["agg_status"] == "done"

    def test_review_beats_waiting(self, conn):
        root, kids = _mk_initiative(conn, 2)
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (kids[0],))
        conn.commit()
        assert kb.initiative_rollups(conn)[root]["agg_status"] == "review"

    def test_tenant_filter(self, conn):
        root_v, _ = _mk_initiative(conn, 2, tenant="voicera")
        root_w, _ = _mk_initiative(conn, 2, tenant="wico")
        r = kb.initiative_rollups(conn, tenant="voicera")
        assert root_v in r and root_w not in r


# ---------------------------------------------------------------------------
# Dependency-Abgrenzung (depends_on ≠ Hierarchie)
# ---------------------------------------------------------------------------


class TestDependencySeparation:
    def test_links_between_members_do_not_change_rollup(self, conn):
        root, kids = _mk_initiative(conn, 3)
        before = kb.initiative_rollups(conn)[root]
        # Kette von depends_on-Kanten zwischen den Geschwistern.
        kb.link_tasks(conn, kids[0], kids[1])
        kb.link_tasks(conn, kids[1], kids[2])
        after = kb.initiative_rollups(conn)[root]
        # Nur die vom Gating erwartete Statusverschiebung (ready→todo) darf
        # sich im waiting-Topf spiegeln — Mitgliedschaft/total unverändert,
        # und kein Link macht aus einem Fremden ein Mitglied.
        assert after["total"] == before["total"] == 3
        assert set(m.id for m in kb.initiative_members(conn, root)) == set(kids)

    def test_links_to_outside_tasks_do_not_add_members(self, conn):
        root, kids = _mk_initiative(conn, 2)
        outsider = kb.create_task(conn, title="Fremde Karte")
        kb.link_tasks(conn, outsider, kids[0])   # kids[0] wartet auf outsider
        kb.link_tasks(conn, kids[1], outsider)   # outsider wartet auf kids[1]
        members = {m.id for m in kb.initiative_members(conn, root)}
        assert members == set(kids)
        assert kb.get_task(conn, outsider).initiative_id is None
        rollup = kb.initiative_rollups(conn)[root]
        assert rollup["total"] == 2

    def test_decomposed_root_reverse_links_do_not_leak(self, conn):
        """decompose verlinkt den Root als task_links-CHILD jedes Children —
        das darf weder Mitgliedschaft noch Rollup doppeln."""
        root = kb.create_task(conn, title="Initiative", triage=True)
        kids = kb.decompose_triage_task(
            conn, root, root_assignee="orchestrator",
            children=[{"title": "a"}, {"title": "b"}], author="d",
        )
        rollup = kb.initiative_rollups(conn)[root]
        assert rollup["total"] == 2
        assert {m.id for m in kb.initiative_members(conn, root)} == set(kids)
