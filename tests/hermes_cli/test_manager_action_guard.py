"""Integration coverage for the per-card Manager action loop brake."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_third_identical_action_alerts_once_halts_only_that_card_and_resets_on_change(
    kanban_home, monkeypatch
):
    """The persistent CLI seam brakes one stable card without stalling others."""
    alerts: list[tuple[str, str, int]] = []
    monkeypatch.setattr(
        kc,
        "_send_manager_loop_alert",
        lambda task, action_kind, repeat_count: alerts.append(
            (task.id, action_kind, repeat_count)
        ),
    )

    with kb.connect() as conn:
        card_a = kb.create_task(conn, title="stable contradiction")
        card_b = kb.create_task(conn, title="healthy card")

    for cycle in range(5):
        kc.run_slash(
            f"--manager-action comment {card_a} 'identical diagnosis'"
        )
        kc.run_slash(
            f"--manager-action comment {card_b} 'Manager: normal progress {cycle}'"
        )

    with kb.connect() as conn:
        comments_a = kb.list_comments(conn, card_a)
        comments_b = kb.list_comments(conn, card_b)
        halted_events = [
            event
            for event in kb.list_events(conn, card_a)
            if event.kind == "manager_loop_halted"
        ]

    assert [comment.body for comment in comments_a] == [
        "identical diagnosis"
    ] * 3
    assert [comment.body for comment in comments_b] == [
        f"Manager: normal progress {cycle}" for cycle in range(5)
    ]
    assert alerts == [(card_a, "comment", 3)]
    assert len(halted_events) == 1

    # A relevant external change starts a fresh episode for A. B keeps running,
    # proving reset and suppression are both scoped to the individual card.
    with kb.connect() as conn:
        kb.add_comment(conn, card_a, "user", "Manager: external evidence")

    kc.run_slash(
        f"--manager-action comment {card_a} 'identical diagnosis'"
    )
    kc.run_slash(
        f"--manager-action comment {card_b} 'Manager: normal progress 5'"
    )

    with kb.connect() as conn:
        manager_a = [
            comment
            for comment in kb.list_comments(conn, card_a)
            if comment.body == "identical diagnosis"
        ]
        manager_b = [
            comment
            for comment in kb.list_comments(conn, card_b)
            if comment.body.startswith("Manager:")
        ]

    assert len(manager_a) == 4
    assert len(manager_b) == 6
    assert alerts == [(card_a, "comment", 3)]


def test_effective_comment_content_is_canonical_and_failed_mutation_does_not_count(
    kanban_home, monkeypatch
):
    alerts = []
    monkeypatch.setattr(kc, "_send_manager_loop_alert", lambda *args: alerts.append(args))
    with kb.connect() as conn:
        card = kb.create_task(conn, title="canonical", assignee="manager")

    for command in (
        f'--manager-action comment {card} "same text"',
        f"--manager-action comment {card} same text",
        f'--manager-action comment {card} "same text"',
        f"--manager-action comment {card} same text",
    ):
        kc.run_slash(command)

    with kb.connect() as conn:
        assert [c.body for c in kb.list_comments(conn, card)] == ["same text"] * 3
        row = conn.execute(
            "SELECT repeat_count, halted FROM manager_action_guard WHERE task_id = ?",
            (card,),
        ).fetchone()
        assert (row["repeat_count"], row["halted"]) == (3, 1)
    assert len(alerts) == 1

    with kb.connect() as conn:
        invalid = kb.create_task(conn, title="invalid", assignee="manager")
    for _ in range(3):
        kc.run_slash(f"--manager-action relabel {invalid}")
    with kb.connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM manager_action_guard WHERE task_id = ?", (invalid,)
        ).fetchone() is None


def test_failed_telegram_delivery_remains_in_outbox_and_retries_next_tick(
    kanban_home, monkeypatch
):
    attempts = []

    def flaky_send(*args):
        attempts.append(args)
        if len(attempts) == 1:
            raise RuntimeError("telegram unavailable")

    monkeypatch.setattr(kc, "_send_manager_loop_alert", flaky_send)
    with kb.connect() as conn:
        card = kb.create_task(conn, title="retry alert", assignee="manager")
        other_card = kb.create_task(conn, title="healthy card", assignee="manager")

    for _ in range(3):
        kc.run_slash(f'--manager-action comment {card} "same"')
    # The Manager may correctly skip the halted card on later ticks. Any next
    # guarded action must therefore drain the board-wide durable outbox rather
    # than requiring a fourth attempt against the already-halted card.
    kc.run_slash(f'--manager-action comment {other_card} "keeps moving"')

    with kb.connect() as conn:
        row = conn.execute(
            "SELECT attempts, delivered_at FROM manager_alert_outbox WHERE task_id = ?",
            (card,),
        ).fetchone()
        assert row["attempts"] == 2
        assert row["delivered_at"] is not None
        assert len(kb.list_comments(conn, card)) == 3
    assert len(attempts) == 2
