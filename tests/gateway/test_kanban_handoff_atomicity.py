"""Atomic worker handoff and dispatcher anomaly regression coverage."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
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
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _running_task(conn, *, assignee: str = "coder") -> tuple[str, int]:
    task_id = kb.create_task(conn, title="atomic handoff", assignee=assignee)
    claimed = kb.claim_task(conn, task_id, claimer="test:worker")
    assert claimed is not None
    run = kb.latest_run(conn, task_id)
    assert run is not None and run.ended_at is None
    return task_id, run.id


def _event_payloads(conn, task_id: str, kind: str) -> list[dict]:
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
        (task_id, kind),
    ).fetchall()
    return [json.loads(row["payload"]) for row in rows]


def _inject_handoff_event(
    conn,
    task_id: str,
    *,
    expected_status: str = "review",
    age_seconds: int = 120,
) -> tuple[int, int]:
    now = int(time.time())
    comment = conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
        (task_id, "coder", "finished handoff evidence", now - age_seconds),
    )
    comment_id = int(comment.lastrowid)
    event = conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'handoff', ?, ?)",
        (
            task_id,
            json.dumps(
                {
                    "comment_id": comment_id,
                    "from": "coder",
                    "to": "reviewer",
                    "expected_status": expected_status,
                }
            ),
            now - age_seconds,
        ),
    )
    conn.commit()
    return int(event.lastrowid), comment_id


def test_worker_handoff_is_atomic_and_warden_alarms_partial_handoff(
    kanban_home, monkeypatch, caplog
):
    """One DB call owns the handoff, and the real tick alarms partial state once."""
    note = "Review handoff — geprüft ✅\nTests: 12 passed"
    with kb.connect() as conn:
        task_id, run_id = _running_task(conn)
        result = kb.handoff_task(
            conn,
            task_id,
            to="reviewer",
            note=note,
            author="coder",
            kind="review",
            expected_run_id=run_id,
        )
        assert result.status == "review"
        assert result.assignee == "reviewer"

    # Independent reconnect proves the whole transition committed together.
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "review"
        assert task.assignee == "reviewer"
        assert task.claim_lock is None
        assert task.worker_pid is None
        assert task.current_run_id is None
        comments = kb.list_comments(conn, task_id)
        assert [(c.author, c.body) for c in comments] == [("coder", note)]
        run = kb.latest_run(conn, task_id)
        assert run is not None and run.id == run_id and run.ended_at is not None
        handoffs = _event_payloads(conn, task_id, "handoff")
        assert handoffs == [
            {
                "comment_id": comments[0].id,
                "from": "coder",
                "to": "reviewer",
                "expected_status": "review",
            }
        ]

        # Reproduce the old split-write residue: structured review handoff exists,
        # but the card is still ready and no worker/run/claim remains.
        anomaly_id = kb.create_task(conn, title="partial", assignee="coder")
        handoff_event_id, handoff_comment_id = _inject_handoff_event(
            conn, anomaly_id
        )

    spawned: list[str] = []
    caplog.set_level(logging.ERROR, logger=kb.__name__)
    with kb.connect() as conn:
        first = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, *_args: spawned.append(task.id),
            max_spawn=10,
        )
        second = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, *_args: spawned.append(task.id),
            max_spawn=10,
        )
        assert first.handoff_anomalies == [anomaly_id]
        assert second.handoff_anomalies == []
        assert anomaly_id not in spawned
        alarms = _event_payloads(conn, anomaly_id, "handoff_anomaly")
        assert alarms == [
            {
                "handoff_event_id": handoff_event_id,
                "comment_id": handoff_comment_id,
                "expected_status": "review",
                "actual_status": "ready",
                "expected_assignee": "reviewer",
                "actual_assignee": "coder",
            }
        ]
    assert any(anomaly_id in record.getMessage() for record in caplog.records)


def test_handoff_to_next_role_lands_ready_and_closes_the_run(kanban_home):
    with kb.connect() as conn:
        task_id, run_id = _running_task(conn, assignee="specifier")

        result = kb.handoff_task(
            conn,
            task_id,
            to="hermes-coder",
            note="ABNAHMEKRITERIUM: focused command exits zero",
            author="specifier",
            expected_run_id=run_id,
        )

        task = kb.get_task(conn, task_id)
        assert task is not None
        assert (task.status, task.assignee, task.current_run_id) == (
            "ready",
            "hermes-coder",
            None,
        )
        assert (result.status, result.assignee, result.run_id) == (
            "ready",
            "hermes-coder",
            run_id,
        )
        run = conn.execute(
            "SELECT status, outcome, ended_at FROM task_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        assert run is not None
        assert run["status"] == "handoff"
        assert run["outcome"] == "handoff"
        assert run["ended_at"] is not None


def test_handoff_rolls_back_comment_run_status_and_event_on_transition_error(kanban_home):
    with kb.connect() as conn:
        task_id, run_id = _running_task(conn)
        conn.execute(
            f"""
            CREATE TEMP TRIGGER abort_handoff_update
            BEFORE UPDATE OF status ON tasks
            WHEN OLD.id = '{task_id}' AND NEW.status = 'review'
            BEGIN
                SELECT RAISE(ABORT, 'injected transition failure');
            END
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected transition failure"):
            kb.handoff_task(
                conn,
                task_id,
                to="reviewer",
                note="must disappear",
                author="coder",
                kind="review",
                expected_run_id=run_id,
            )

    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "running"
        assert task.assignee == "coder"
        assert task.current_run_id == run_id
        assert task.claim_lock == "test:worker"
        assert kb.list_comments(conn, task_id) == []
        assert _event_payloads(conn, task_id, "handoff") == []
        run = kb.latest_run(conn, task_id)
        assert run is not None and run.ended_at is None


def test_handoff_anomaly_negative_controls(kanban_home, monkeypatch):
    with kb.connect() as conn:
        ordinary = kb.create_task(conn, title="ordinary", assignee="coder")
        kb.add_comment(conn, ordinary, "coder", "normal progress comment")

        retry = kb.create_task(conn, title="retry/backoff", assignee="coder")
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'retry_scheduled', '{}', ?)",
            (retry, int(time.time()) - 120),
        )
        conn.commit()

        role_ready, role_run = _running_task(conn, assignee="specifier")
        kb.handoff_task(
            conn,
            role_ready,
            to="hermes-coder",
            note="ready for implementation",
            author="specifier",
            expected_run_id=role_run,
        )

        live_claim = kb.create_task(conn, title="live claim", assignee="coder")
        _inject_handoff_event(conn, live_claim)
        conn.execute(
            "UPDATE tasks SET claim_lock = 'test:live-claim' WHERE id = ?",
            (live_claim,),
        )
        conn.commit()

        live_run = kb.create_task(conn, title="live run", assignee="coder")
        _inject_handoff_event(conn, live_run)
        run = conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at) "
            "VALUES (?, 'coder', 'running', ?)",
            (live_run, int(time.time())),
        )
        assert run.lastrowid is not None
        conn.execute(
            "UPDATE tasks SET current_run_id = ? WHERE id = ?",
            (run.lastrowid, live_run),
        )
        conn.commit()

        live_pid = kb.create_task(conn, title="live pid", assignee="coder")
        _inject_handoff_event(conn, live_pid)
        conn.execute(
            "UPDATE tasks SET worker_pid = ? WHERE id = ?",
            (os.getpid(), live_pid),
        )
        conn.commit()

        live_heartbeat = kb.create_task(
            conn, title="live heartbeat", assignee="coder"
        )
        _inject_handoff_event(conn, live_heartbeat)
        conn.execute(
            "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?",
            (int(time.time()), live_heartbeat),
        )
        conn.commit()

        grace = kb.create_task(conn, title="grace", assignee="coder")
        _inject_handoff_event(conn, grace, age_seconds=0)

        merger = kb.create_task(conn, title="merge lane", assignee="merger")
        _inject_handoff_event(conn, merger)

        verified_return = kb.create_task(conn, title="verification return", assignee="coder")
        _inject_handoff_event(conn, verified_return)
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'done_verification_failed', '{}', ?)",
            (verified_return, int(time.time())),
        )
        conn.commit()

    monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid == os.getpid())
    with kb.connect() as conn:
        result = kb.dispatch_once(conn, spawn_fn=lambda *_args: None, max_spawn=0)
        assert result.handoff_anomalies == []
        for task_id in (
            ordinary,
            retry,
            role_ready,
            live_claim,
            live_run,
            live_pid,
            live_heartbeat,
            grace,
            merger,
            verified_return,
        ):
            assert _event_payloads(conn, task_id, "handoff_anomaly") == []


def test_handoff_anomaly_uses_live_state_not_stale_liveness_residue(kanban_home):
    """Expired claims and ended-run pointers do not hide a wrong-role handoff."""
    with kb.connect() as conn:
        task_id, run_id = _running_task(conn, assignee="specifier")
        result = kb.handoff_task(
            conn,
            task_id,
            to="hermes-coder",
            note="ready for implementation",
            author="specifier",
            expected_run_id=run_id,
        )
        assert result.status == "ready"
        event = conn.execute(
            "SELECT id FROM task_events "
            "WHERE task_id = ? AND kind = 'handoff' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        assert event is not None
        conn.execute(
            "UPDATE task_events SET created_at = ? WHERE id = ?",
            (int(time.time()) - 120, event["id"]),
        )
        conn.execute(
            "UPDATE tasks SET assignee = 'wrong-role', "
            "claim_lock = 'expired', claim_expires = ?, current_run_id = ? "
            "WHERE id = ?",
            (int(time.time()) - 60, run_id, task_id),
        )
        conn.commit()

        tick = kb.dispatch_once(conn, spawn_fn=lambda *_args: None, max_spawn=0)
        assert tick.handoff_anomalies == [task_id]
        alarms = _event_payloads(conn, task_id, "handoff_anomaly")
        assert len(alarms) == 1
        assert alarms[0]["expected_assignee"] == "hermes-coder"
        assert alarms[0]["actual_assignee"] == "wrong-role"
