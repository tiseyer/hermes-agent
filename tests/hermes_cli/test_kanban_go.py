"""Acceptance tests for the atomic, server-side Kanban GO contract."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from plugins.kanban.dashboard.plugin_api import router as kanban_router


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated board: these tests must never open the operator's live DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _create_task(conn, title: str, *, tenant: str = "acme", assignee: str = "hermes-coder") -> str:
    return kb.create_task(
        conn,
        title=title,
        tenant=tenant,
        assignee=assignee,
    )


def _hold_go_gate(
    conn,
    task_id: str,
    *,
    kind: str = "build",
    resume_assignee: str | None = "hermes-coder",
    include_resume_assignee: bool = True,
) -> None:
    """Reproduce the dispatcher's durable blocked@till GO gate."""
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'blocked', assignee = 'till', "
            "block_kind = 'needs_input' WHERE id = ?",
            (task_id,),
        )
        kb._append_event(
            conn,
            task_id,
            "blocked",
            {"kind": "needs_input", "reason": "go_gate: explicit human GO required"},
        )
        gate_payload: dict[str, object] = {
            "assignee": "till",
            "status": "blocked",
            "kind": kind,
        }
        if include_resume_assignee:
            gate_payload["resume_assignee"] = resume_assignee
        kb._append_event(
            conn,
            task_id,
            "go_gate_held",
            gate_payload,
        )


def _go_events(conn, task_id: str):
    return [event for event in kb.list_events(conn, task_id) if event.kind == "go_granted"]


def _get_task(conn, task_id: str):
    task = kb.get_task(conn, task_id)
    assert task is not None
    return task


def _snapshot(conn):
    return {
        "tasks": [tuple(row) for row in conn.execute(
            "SELECT id, status, assignee FROM tasks ORDER BY id"
        ).fetchall()],
        "events": conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
        "comments": conn.execute("SELECT COUNT(*) FROM task_comments").fetchone()[0],
    }


@pytest.mark.parametrize("kind", ["build", "merge"])
def test_grant_go_releases_gate_once_with_structured_audit(kanban_home, kind):
    with kb.connect() as conn:
        task_id = _create_task(conn, f"{kind} gated")
        _hold_go_gate(conn, task_id, kind=kind)

        granted = kb.grant_go(conn, task_id, kind=kind, actor="Till")

        assert granted.applied is True
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "ready"
        assert task.assignee == "hermes-coder"
        assert kb._go_gate_released(conn, task_id) is True

        events = _go_events(conn, task_id)
        assert len(events) == 1
        assert events[0].payload == {
            "task_id": task_id,
            "kind": kind,
            "actor": "Till",
            "timestamp": granted.timestamp,
            "action": None,
        }
        comments = kb.list_comments(conn, task_id)
        go_comments = [comment for comment in comments if comment.body.startswith("GO erteilt:")]
        assert len(go_comments) == 1
        assert go_comments[0].author == "Till"
        assert task_id in go_comments[0].body
        assert kind in go_comments[0].body
        assert "Till" in go_comments[0].body
        assert granted.timestamp_iso in go_comments[0].body

        before_retry = _snapshot(conn)
        retried = kb.grant_go(conn, task_id, kind=kind, actor="Till")
        assert retried.applied is False
        assert retried.timestamp == granted.timestamp
        assert _snapshot(conn) == before_retry


def test_grant_go_idempotency_is_scoped_to_the_current_gate_cycle(kanban_home):
    with kb.connect() as conn:
        task_id = _create_task(conn, "repeated build gate")
        _hold_go_gate(conn, task_id, kind="build", resume_assignee="alice")
        first = kb.grant_go(conn, task_id, kind="build", actor="Till")
        assert first.applied is True

        _hold_go_gate(conn, task_id, kind="build", resume_assignee="bob")
        second = kb.grant_go(conn, task_id, kind="build", actor="Till")

        assert second.applied is True
        task = _get_task(conn, task_id)
        assert task.status == "ready"
        assert task.assignee == "bob"
        assert len(_go_events(conn, task_id)) == 2
        assert len([
            comment for comment in kb.list_comments(conn, task_id)
            if comment.body.startswith("GO erteilt:")
        ]) == 2


@pytest.mark.parametrize(
    ("action", "expected_status"),
    [
        ({"name": "archive", "target_ids": "TARGET_IDS"}, "archived"),
        (
            {
                "name": "set_status",
                "target_ids": "TARGET_IDS",
                "status": "backlog",
            },
            "backlog",
        ),
    ],
)
def test_grant_go_applies_allowlisted_multi_target_action_atomically(
    kanban_home, action, expected_status
):
    with kb.connect() as conn:
        gate_id = _create_task(conn, "GO control")
        target_ids = [_create_task(conn, "target one"), _create_task(conn, "target two")]
        control_id = _create_task(conn, "untouched control")
        control_status_before = _get_task(conn, control_id).status
        _hold_go_gate(conn, gate_id)
        action = {**action, "target_ids": target_ids}

        result = kb.grant_go(
            conn,
            gate_id,
            kind="build",
            actor="Till",
            action=action,
        )

        assert result.applied is True
        assert [_get_task(conn, tid).status for tid in target_ids] == [
            expected_status,
            expected_status,
        ]
        assert _get_task(conn, control_id).status == control_status_before
        assert _go_events(conn, gate_id)[0].payload["action"] == action

        before_retry = _snapshot(conn)
        retried = kb.grant_go(
            conn,
            gate_id,
            kind="build",
            actor="Till",
            action=action,
        )
        assert retried.applied is False
        assert _snapshot(conn) == before_retry


def test_grant_go_rejects_invalid_or_foreign_requests_without_any_write(kanban_home):
    with kb.connect() as conn:
        gate_id = _create_task(conn, "GO control", tenant="acme")
        local_id = _create_task(conn, "local target", tenant="acme")
        foreign_id = _create_task(conn, "foreign target", tenant="other")
        _hold_go_gate(conn, gate_id)

        invalid_requests = [
            {"kind": "deploy", "action": None},
            {
                "kind": "build",
                "action": {"name": "delete", "target_ids": [local_id]},
            },
            {
                "kind": "build",
                "action": {"name": "archive", "target_ids": []},
            },
            {
                "kind": "build",
                "action": {"name": "archive", "target_ids": ["t_missing"]},
            },
            {
                "kind": "build",
                "action": {"name": "archive", "target_ids": [foreign_id]},
            },
            {
                "kind": "build",
                "action": {
                    "name": "set_status",
                    "target_ids": [local_id],
                    "status": "merge",
                },
            },
            {
                "kind": "build",
                "action": {
                    "name": "set_status",
                    "target_ids": [local_id],
                    "status": "blocked",
                },
            },
        ]

        for request in invalid_requests:
            before = _snapshot(conn)
            with pytest.raises(ValueError):
                kb.grant_go(
                    conn,
                    gate_id,
                    kind=request["kind"],
                    actor="Till",
                    action=request["action"],
                )
            assert _snapshot(conn) == before


def test_grant_go_rejects_archiving_a_running_target_atomically(kanban_home):
    with kb.connect() as conn:
        gate_id = _create_task(conn, "GO control")
        running_id = _create_task(conn, "active worker")
        claimed = kb.claim_task(conn, running_id, claimer="test:worker")
        assert claimed is not None
        assert claimed.status == "running"
        run_id = claimed.current_run_id
        assert run_id is not None
        _hold_go_gate(conn, gate_id)
        before = _snapshot(conn)

        with pytest.raises(ValueError, match="cannot move running targets"):
            kb.grant_go(
                conn,
                gate_id,
                kind="build",
                actor="Till",
                action={"name": "archive", "target_ids": [running_id]},
            )

        assert _snapshot(conn) == before
        running = _get_task(conn, running_id)
        assert running.status == "running"
        assert running.current_run_id == run_id
        open_run = conn.execute(
            "SELECT ended_at FROM task_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert open_run is not None
        assert open_run["ended_at"] is None


def test_go_api_is_a_direct_transport_to_the_canonical_db_service(kanban_home):
    with kb.connect() as conn:
        gate_id = _create_task(conn, "GO via API")
        target_ids = [_create_task(conn, "archive one"), _create_task(conn, "archive two")]
        _hold_go_gate(conn, gate_id, kind="merge")

    app = FastAPI()

    @app.middleware("http")
    async def attach_verified_principal(request, call_next):
        request.state.token_principal = SimpleNamespace(
            principal="voicera:till",
            scopes=("kanban:go",),
        )
        request.state.token_authenticated = True
        return await call_next(request)

    app.include_router(kanban_router, prefix="/api/plugins/kanban")
    client = TestClient(app)

    response = client.post(
        "/api/plugins/kanban/go",
        json={
            "task_id": gate_id,
            "kind": "merge",
            "action": {"name": "archive", "target_ids": target_ids},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["task_id"] == gate_id
    assert body["kind"] == "merge"
    assert body["actor"] == "voicera:till"
    assert body["applied"] is True
    with kb.connect() as conn:
        assert _get_task(conn, gate_id).status == "ready"
        assert [_get_task(conn, tid).status for tid in target_ids] == [
            "archived",
            "archived",
        ]
        event_payload = _go_events(conn, gate_id)[0].payload
        assert json.loads(json.dumps(event_payload))["actor"] == "voicera:till"


def test_go_api_rejects_unknown_action_fields(kanban_home):
    with kb.connect() as conn:
        gate_id = _create_task(conn, "GO via API")
        target_id = _create_task(conn, "target")
        _hold_go_gate(conn, gate_id)
        before = _snapshot(conn)

    app = FastAPI()

    @app.middleware("http")
    async def attach_verified_principal(request, call_next):
        request.state.token_principal = SimpleNamespace(
            principal="voicera:till",
            scopes=("kanban:go",),
        )
        return await call_next(request)

    app.include_router(kanban_router, prefix="/api/plugins/kanban")
    response = TestClient(app).post(
        "/api/plugins/kanban/go",
        json={
            "task_id": gate_id,
            "kind": "build",
            "action": {
                "name": "archive",
                "target_ids": [target_id],
                "arbitrary_tool": "delete_everything",
            },
        },
    )

    assert response.status_code == 422
    with kb.connect() as conn:
        assert _snapshot(conn) == before


def test_go_api_uses_only_a_scoped_verified_actor(kanban_home):
    from hermes_cli.dashboard_auth.token_auth import is_token_route
    from plugins.kanban.dashboard.plugin_api import GO_ROUTE_PATH

    with kb.connect() as conn:
        gate_id = _create_task(conn, "GO via unscoped token")
        _hold_go_gate(conn, gate_id)
        before = _snapshot(conn)

    assert is_token_route(GO_ROUTE_PATH)
    app = FastAPI()

    @app.middleware("http")
    async def attach_unscoped_principal(request, call_next):
        request.state.token_principal = SimpleNamespace(
            principal="forged-body-cannot-win",
            scopes=(),
        )
        return await call_next(request)

    app.include_router(kanban_router, prefix="/api/plugins/kanban")
    response = TestClient(app).post(
        "/api/plugins/kanban/go",
        json={
            "task_id": gate_id,
            "kind": "build",
            "actor": "Till",
        },
    )

    # Caller-supplied actor is forbidden at schema level and cannot forge the
    # audit identity. Even without that field this principal lacks kanban:go.
    assert response.status_code == 422
    with kb.connect() as conn:
        assert _snapshot(conn) == before

    response = TestClient(app).post(
        "/api/plugins/kanban/go",
        json={"task_id": gate_id, "kind": "build"},
    )
    assert response.status_code == 403
    with kb.connect() as conn:
        assert _snapshot(conn) == before


def test_grant_go_requires_the_active_gate_kind_and_not_a_stale_gate(kanban_home):
    with kb.connect() as conn:
        task_id = _create_task(conn, "kind-bound gate")
        _hold_go_gate(conn, task_id, kind="build")

        before = _snapshot(conn)
        with pytest.raises(ValueError):
            kb.grant_go(conn, task_id, kind="merge", actor="Till")
        assert _snapshot(conn) == before

        assert kb.unblock_task(conn, task_id)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (task_id,))
            kb._append_event(
                conn,
                task_id,
                "blocked",
                {"kind": "needs_input", "reason": "unrelated later block"},
            )
        before = _snapshot(conn)
        with pytest.raises(ValueError):
            kb.grant_go(conn, task_id, kind="build", actor="Till")
        assert _snapshot(conn) == before


def test_legacy_gate_without_resume_assignee_has_deterministic_fallback(kanban_home):
    with kb.connect() as conn:
        task_id = _create_task(conn, "legacy build gate", assignee="till")
        _hold_go_gate(
            conn,
            task_id,
            kind="build",
            include_resume_assignee=False,
        )

        kb.grant_go(conn, task_id, kind="build", actor="Till")

        task = _get_task(conn, task_id)
        assert task.status == "ready"
        assert task.assignee == "default"


def test_dispatcher_gate_round_trip_resumes_original_profile(
    kanban_home, all_assignees_spawnable
):
    """Exercise the real gate, release predicate, and next dispatcher tick."""
    with kb.connect() as conn:
        task_id = _create_task(
            conn,
            "Smoke-Tests gegen prod fahren",
            assignee="alice",
        )
        spawned: list[str] = []

        kb.dispatch_once(conn, spawn_fn=lambda task, workspace, **kw: spawned.append(task.id))

        assert spawned == []
        held = [event for event in kb.list_events(conn, task_id) if event.kind == "go_gate_held"]
        assert held[-1].payload is not None
        assert held[-1].payload["resume_assignee"] == "alice"
        kb.grant_go(conn, task_id, kind="build", actor="Till")
        released = kb.get_task(conn, task_id)
        assert released is not None
        assert released.status == "ready"
        assert released.assignee == "alice"

        kb.dispatch_once(conn, spawn_fn=lambda task, workspace, **kw: spawned.append(task.id))

        assert spawned == [task_id]
