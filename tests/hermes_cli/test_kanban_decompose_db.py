"""Tests for kb.decompose_triage_task — the DB-layer atomic fan-out
from the triage column. LLM-free by design.
"""

from __future__ import annotations

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


def _create_triage(conn, title="rough idea", body=None, assignee=None, tenant=None):
    return kb.create_task(
        conn,
        title=title,
        body=body,
        assignee=assignee,
        tenant=tenant,
        triage=True,
    )


def test_decompose_creates_children_and_promotes_root(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn, title="ship a feature")
        assert kb.get_task(conn, tid).status == "triage"

    children = [
        {"title": "research", "body": "look at prior art", "assignee": "researcher", "parents": []},
        {"title": "build it", "body": "write code", "assignee": "engineer", "parents": [0]},
    ]
    with kb.connect() as conn:
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orchestrator",
            children=children,
            author="decomposer",
        )
    assert child_ids is not None
    assert len(child_ids) == 2

    with kb.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, child_ids[0])
        c1 = kb.get_task(conn, child_ids[1])

    # Root mirrors its family's active work lane, not a dispatchable root job.
    assert root.status == "ready"
    assert root.assignee == "orchestrator"
    assert (root.family_root_id, root.family_order) == (tid, 0)
    # First child has no internal parents → ready on recompute_ready.
    assert c0.status == "ready"
    assert c0.assignee == "researcher"
    assert (c0.family_root_id, c0.family_order, c0.child_role) == (tid, 1, "work")
    # Second child has parents=[0] → stays in todo until c0 completes.
    assert c1.status == "todo"
    assert c1.assignee == "engineer"
    assert (c1.family_root_id, c1.family_order, c1.child_role) == (tid, 2, "work")


def _promoted_events(conn, task_id):
    return [e for e in kb.list_events(conn, task_id) if e.kind == "promoted"]


def test_decompose_leaf_chain_promotes_and_progresses(kanban_home):
    """P06 promoted-Fix (Paket 2): decompose promotes parent-free leaves
    to ``ready`` directly (promoted>0), and the dependency chain then
    advances A done -> B ready -> ... -> root done.

    This is the regression guard for the fork-local deadlock where
    ``recompute_ready``'s backlog rule (parent-less ``todo`` = backlog)
    swallowed decompose leaves forever, so ``promoted`` stayed 0 and no
    child ever dispatched. The negative probe (restoring the old
    ``if auto_promote: recompute_ready(conn)`` body) turns this test red.
    """
    with kb.connect() as conn:
        tid = _create_triage(conn, title="ship a feature")

    # Pure chain: A is a parent-free leaf, B waits on A, C waits on B.
    children = [
        {"title": "A", "assignee": "engineer", "parents": []},
        {"title": "B", "assignee": "engineer", "parents": [0]},
        {"title": "C", "assignee": "engineer", "parents": [1]},
    ]
    with kb.connect() as conn:
        child_ids = kb.decompose_triage_task(
            conn, tid, root_assignee="orchestrator",
            children=children, author="decomposer",
        )
    assert child_ids is not None and len(child_ids) == 3
    a, b, c = child_ids

    # --- promoted>0: the leaf is promoted to ready by decompose itself ---
    with kb.connect() as conn:
        assert kb.get_task(conn, a).status == "ready"     # leaf: startable now
        assert kb.get_task(conn, b).status == "todo"      # waits on A
        assert kb.get_task(conn, c).status == "todo"      # waits on B
        # The decompose fan-out emitted at least one 'promoted' event
        # (the P06 fix). Old code left every leaf in 'todo' -> zero.
        promoted_total = sum(len(_promoted_events(conn, x)) for x in child_ids)
        assert promoted_total >= 1
        assert len(_promoted_events(conn, a)) == 1

    # --- chain progression: A done -> B ready ---
    with kb.connect() as conn:
        assert kb.complete_task(conn, a, result="A done")
    with kb.connect() as conn:
        assert kb.get_task(conn, a).status == "done"
        assert kb.get_task(conn, b).status == "ready"     # promoted by chain
        assert kb.get_task(conn, c).status == "todo"
        assert len(_promoted_events(conn, b)) == 1

    # --- B done -> C ready ---
    with kb.connect() as conn:
        assert kb.complete_task(conn, b, result="B done")
    with kb.connect() as conn:
        assert kb.get_task(conn, b).status == "done"
        assert kb.get_task(conn, c).status == "ready"

    # --- C done -> root (Mutter) done ---
    with kb.connect() as conn:
        assert kb.complete_task(conn, c, result="C done")
    with kb.connect() as conn:
        assert kb.get_task(conn, c).status == "done"
        # Root waits on the whole graph via task_links; once every child
        # is terminal the family projection settles to done.
        assert kb.get_task(conn, tid).status == "done"


def test_decompose_returns_none_when_task_missing(kanban_home):
    with kb.connect() as conn:
        result = kb.decompose_triage_task(
            conn,
            "nonexistent",
            root_assignee="orch",
            children=[{"title": "x"}],
            author="me",
        )
    assert result is None


def test_decompose_returns_none_when_task_not_in_triage(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="already a real task")  # not triage
        result = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orch",
            children=[{"title": "x"}],
            author="me",
        )
    assert result is None


def test_decompose_empty_children_returns_none(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        result = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orch",
            children=[],
            author="me",
        )
    assert result is None


def test_decompose_rejects_self_parent(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        with pytest.raises(ValueError, match="cannot list itself"):
            kb.decompose_triage_task(
                conn,
                tid,
                root_assignee="orch",
                children=[{"title": "x", "parents": [0]}],
                author="me",
            )


def test_decompose_rejects_out_of_range_parent(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        with pytest.raises(ValueError, match="not a valid index"):
            kb.decompose_triage_task(
                conn,
                tid,
                root_assignee="orch",
                children=[{"title": "x", "parents": [5]}],
                author="me",
            )


def test_decompose_rejects_cyclic_parents(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        with pytest.raises(ValueError, match="cyclic dependency"):
            kb.decompose_triage_task(
                conn,
                tid,
                root_assignee="orch",
                children=[
                    {"title": "A", "parents": [1]},
                    {"title": "B", "parents": [0]},
                ],
                author="me",
            )


def test_decompose_records_audit_comment_and_event(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orch",
            children=[{"title": "task A", "assignee": "researcher"}],
            author="alice",
        )
    assert child_ids is not None

    with kb.connect() as conn:
        comments = kb.list_comments(conn, tid)
        events = kb.list_events(conn, tid)

    assert any("Decomposed into" in (c.body or "") for c in comments)
    assert any(ev.kind == "decomposed" for ev in events)


def test_decompose_children_inherit_dir_workspace(kanban_home):
    """Fan-out children inherit the root's dir workspace, not scratch."""
    proj = "/home/teknium/myproject"
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="codegen root", assignee="worker",
            workspace_kind="dir", workspace_path=proj, triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn, tid, root_assignee="orchestrator",
            children=[{"title": "part A"}, {"title": "part B", "parents": [0]}],
            author="decomposer",
        )
    assert child_ids and len(child_ids) == 2
    with kb.connect() as conn:
        for cid in child_ids:
            t = kb.get_task(conn, cid)
            assert t.workspace_kind == "dir"
            assert t.workspace_path == proj


def test_decompose_children_stay_scratch_when_root_scratch(kanban_home):
    """No regression: a scratch root still fans out into scratch children."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="scratch root", assignee="worker",
            workspace_kind="scratch", triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn, tid, root_assignee="orchestrator",
            children=[{"title": "s1"}], author="decomposer",
        )
    with kb.connect() as conn:
        t = kb.get_task(conn, child_ids[0])
    assert t.workspace_kind == "scratch"
    assert t.workspace_path is None


def test_decompose_per_child_workspace_override(kanban_home):
    """An explicit per-child workspace beats inheritance."""
    proj = "/home/teknium/myproject"
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="root", assignee="worker",
            workspace_kind="dir", workspace_path=proj, triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn, tid, root_assignee="orchestrator",
            children=[
                {"title": "override", "workspace_kind": "dir",
                 "workspace_path": "/other/repo"},
                {"title": "inherit"},
            ],
            author="decomposer",
        )
    with kb.connect() as conn:
        over = kb.get_task(conn, child_ids[0])
        inh = kb.get_task(conn, child_ids[1])
    assert over.workspace_path == "/other/repo"
    assert inh.workspace_path == proj


def test_decompose_rewrites_wrong_tenant_role_profiles(kanban_home, monkeypatch):
    """A voicera root whose child is assigned hermes-coder gets the
    tenant-correct voicera-coder (live-repro t_05d6b16b crash loop)."""
    from hermes_cli import profiles as profiles_mod
    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(
        profiles_mod, "profile_exists",
        lambda name: name in ("voicera-coder", "voicera-reviewer",
                              "hermes-coder", "orchestrator"),
    )
    with kb.connect() as conn:
        root = kb.create_task(
            conn, title="root", triage=True, tenant="voicera",
        )
        child_ids = kb.decompose_triage_task(
            conn, root, root_assignee="orchestrator",
            children=[
                {"title": "code it", "assignee": "hermes-coder", "parents": []},
                {"title": "review it", "assignee": "hermes-reviewer",
                 "parents": [0]},
            ],
        )
        assert child_ids
        assert kb.get_task(conn, child_ids[0]).assignee == "voicera-coder"
        assert kb.get_task(conn, child_ids[1]).assignee == "voicera-reviewer"


# --- Leitstern: goal visibility (Ziel lesbar machen, (d) + (e)) ----------

def test_decompose_prepends_goal_pointer_to_children(kanban_home):
    """(d) Every decomposed child body opens with a one-line pointer back
    to the family root's goal — a POINTER, NOT a full copy of the root body
    (single source of truth = the root card)."""
    root_title = "Cockpit-Chat urteilsfähig machen"
    root_body = "GEHEIMER_ZIELTEXT: der große Zweck der ganzen Familie."
    with kb.connect() as conn:
        tid = _create_triage(conn, title=root_title, body=root_body)
        child_ids = kb.decompose_triage_task(
            conn, tid, root_assignee="orchestrator",
            children=[
                {"title": "teil A", "body": "mach A", "parents": []},
                {"title": "teil B", "body": "", "parents": [0]},
            ],
            author="decomposer",
        )
    assert child_ids and len(child_ids) == 2
    with kb.connect() as conn:
        c0 = kb.get_task(conn, child_ids[0])
        c1 = kb.get_task(conn, child_ids[1])
    ptr = f"## Übergeordnetes Ziel\nTeil von: {root_title} (Ziel siehe Wurzelkarte)"
    # child with a body: pointer prepended, original body preserved after it
    assert c0.body.startswith(ptr)
    assert "mach A" in c0.body
    # child with empty body: just the pointer
    assert c1.body == ptr
    # POINTER, not full copy: the root body text is never embedded in children
    assert "GEHEIMER_ZIELTEXT" not in (c0.body or "")
    assert "GEHEIMER_ZIELTEXT" not in (c1.body or "")


def test_decompose_goal_pointer_idempotent(kanban_home):
    """A child body that already opens with the Ziel marker is not
    double-prepended (guards re-decompose / model-authored markers)."""
    pre = "## Übergeordnetes Ziel\nTeil von: etwas anderes (Ziel siehe Wurzelkarte)"
    with kb.connect() as conn:
        tid = _create_triage(conn, title="root", body="root goal")
        child_ids = kb.decompose_triage_task(
            conn, tid, root_assignee="orchestrator",
            children=[{"title": "x", "body": pre + "\n\neigentlicher body"}],
            author="decomposer",
        )
    with kb.connect() as conn:
        c = kb.get_task(conn, child_ids[0])
    assert c.body.count("## Übergeordnetes Ziel") == 1  # no stacking
    assert c.body.startswith(pre)


def test_build_worker_context_surfaces_root_goal(kanban_home):
    """(e) A leaf's worker context surfaces the family root's goal
    (title + full body) under a Leitstern header; the root's own context
    does not (it IS the goal)."""
    root_title = "Cockpit-Chat urteilsfähig machen"
    root_body = "ZIELTEXT_WURZEL: warum die ganze Familie existiert."
    with kb.connect() as conn:
        tid = _create_triage(conn, title=root_title, body=root_body)
        child_ids = kb.decompose_triage_task(
            conn, tid, root_assignee="orchestrator",
            children=[{"title": "leaf", "body": "local fix"}],
            author="decomposer",
        )
    with kb.connect() as conn:
        child_ctx = kb.build_worker_context(conn, child_ids[0])
        root_ctx = kb.build_worker_context(conn, tid)
    # child sees the root goal surfaced at read-time (full body, not just pointer)
    assert f"## Übergeordnetes Ziel (Familien-Wurzel {tid})" in child_ctx
    assert "ZIELTEXT_WURZEL" in child_ctx
    assert root_title in child_ctx
    # the root card itself gets no goal-surfacing block (root_ref == self)
    assert "Übergeordnetes Ziel (Familien-Wurzel" not in root_ctx
