"""Watchdog wiring for the PRODUCTION dispatcher path: the gateway-embedded
loop ``GatewayKanbanWatchersMixin._kanban_dispatcher_watcher``.

The 2026-09-23 08:52 freeze happened in this loop (no separate ``kanban
daemon`` process runs in production). The wiring mirrors ``run_daemon()``:
tick counter bumped AFTER each iteration, heartbeat every iteration (same
``state/dispatcher.heartbeat`` file, ``source: gateway_dispatcher``), same
kill switch, same stall budget. A tick that hangs inside
``asyncio.to_thread`` freezes the counter — the OS-thread watchdog then
hard-exits 75 (proven here in a subprocess), which systemd's
``RestartForceExitStatus=75`` turns into a service restart because the
embedded loop runs inside the gateway process itself (the unit's MainPID).

All subprocess children run with an explicitly sandboxed ``HERMES_HOME``.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

import gateway.kanban_watchers as kw
from hermes_cli import dispatcher_watchdog as wd
from hermes_cli import kanban_db as kb

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def wd_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


class _DummyConn:
    def close(self):
        pass


def _stub_watcher_environment(monkeypatch, dispatch_fn):
    """Stub everything the watcher touches outside its own loop mechanics."""
    monkeypatch.setattr(kw, "_DISPATCHER_INITIAL_DELAY_S", 0.01)
    monkeypatch.setattr(
        kw, "_resolve_auto_decompose_settings", lambda _load_config: (False, 0)
    )
    import hermes_cli.config as cfg

    monkeypatch.setattr(
        cfg, "load_config", lambda: {"kanban": {"dispatch_interval_seconds": 1}}
    )
    monkeypatch.setattr(kb, "connect", lambda **_kw: _DummyConn())
    monkeypatch.setattr(kb, "dispatch_once", dispatch_fn)
    monkeypatch.setattr(kb, "list_boards", lambda **_kw: [{"slug": "default"}])
    monkeypatch.setattr(kb, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(kb, "has_spawnable_ready", lambda _conn: False)
    monkeypatch.setattr(kb, "has_spawnable_review", lambda _conn: False)


class _Host(kw.GatewayKanbanWatchersMixin):
    def __init__(self, on_stall=None, probe=0.1, strikes=3):
        self._running = True
        self._kanban_watchdog_probe_interval = probe
        self._kanban_watchdog_max_strikes = strikes
        self._kanban_watchdog_on_stall = on_stall


async def _drain(task: asyncio.Task, host: _Host, timeout: float = 15.0) -> None:
    host._running = False
    try:
        await asyncio.wait_for(task, timeout)
    except asyncio.TimeoutError:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


def test_watcher_frozen_tick_fires_stall(wd_home, monkeypatch):
    """The production freeze in miniature: dispatch_once hangs inside
    asyncio.to_thread, the tick counter never advances, the watchdog
    fires after 3 missed probes."""
    release = threading.Event()

    def _frozen(*_a, **_kw):
        release.wait(60)

    _stub_watcher_environment(monkeypatch, _frozen)

    fired = threading.Event()
    info: dict = {}

    def on_stall(payload):
        info.update(payload)
        fired.set()

    async def scenario():
        host = _Host(on_stall=on_stall)
        task = asyncio.create_task(host._kanban_dispatcher_watcher())
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(None, fired.wait, 10)
        assert ok, "watchdog did not fire on frozen watcher tick"
        release.set()
        await _drain(task, host)

    asyncio.run(scenario())
    assert info["strikes"] == 3
    assert info["tick_count"] == 0, "hung first tick must freeze the counter"
    assert info["exit_code"] == 75
    assert wd.get_watchdog_dump_path(wd_home).exists()


def test_watcher_normal_ticks_no_stall_and_heartbeat(wd_home, monkeypatch):
    """Healthy watcher over several stall budgets: no fire, heartbeat file
    advances and is attributed to the gateway loop."""
    _stub_watcher_environment(
        monkeypatch, lambda *_a, **_kw: kb.DispatchResult()
    )
    fired = threading.Event()

    async def scenario():
        # Ticks are ~1s apart (interval floor); probe 1.5s x 2 strikes =
        # 3s stall budget, several times the tick spacing.
        host = _Host(on_stall=lambda _info: fired.set(), probe=1.5, strikes=2)
        task = asyncio.create_task(host._kanban_dispatcher_watcher())
        hb_path = wd.get_heartbeat_path(wd_home)
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if hb_path.exists():
                if json.loads(hb_path.read_text())["tick_count"] >= 3:
                    break
            await asyncio.sleep(0.1)
        assert hb_path.exists(), "heartbeat never appeared"
        await _drain(task, host)

    asyncio.run(scenario())
    assert not fired.is_set(), "watchdog fired despite healthy watcher ticks"
    hb = json.loads(wd.get_heartbeat_path(wd_home).read_text())
    assert hb["tick_count"] >= 3
    assert hb["last_tick_ok"] is True
    assert hb["source"] == "gateway_dispatcher"


def test_watcher_kill_switch_starts_no_watchdog(wd_home, monkeypatch):
    """HERMES_DISPATCHER_WATCHDOG=0 on the watcher path: no watchdog
    thread, frozen tick does not trigger a stall."""
    monkeypatch.setenv(wd.WATCHDOG_ENV_VAR, "0")
    release = threading.Event()
    started = threading.Event()

    def _frozen(*_a, **_kw):
        started.set()
        release.wait(60)

    _stub_watcher_environment(monkeypatch, _frozen)
    fired = threading.Event()

    async def scenario():
        host = _Host(on_stall=lambda _info: fired.set(), probe=0.05, strikes=2)
        task = asyncio.create_task(host._kanban_dispatcher_watcher())
        loop = asyncio.get_running_loop()
        assert await loop.run_in_executor(None, started.wait, 10)
        await asyncio.sleep(0.5)  # ~10 strike windows had it been armed
        names = [t.name for t in threading.enumerate()]
        assert "dispatcher-stall-watchdog" not in names
        assert not fired.is_set()
        release.set()
        await _drain(task, host)

    asyncio.run(scenario())


def test_watcher_frozen_tick_exits_75_subprocess(tmp_path):
    """Gateway-context hard-exit proof: the embedded watcher loop with a
    frozen tick takes the whole process down with exit code 75 — exactly
    what systemd's RestartForceExitStatus=75 converts into a restart."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    script = textwrap.dedent(
        """
        import asyncio, sys, time
        sys.path.insert(0, {root!r})
        import gateway.kanban_watchers as kw
        import hermes_cli.config as cfg
        from hermes_cli import kanban_db as kb

        kw._DISPATCHER_INITIAL_DELAY_S = 0.01
        kw._resolve_auto_decompose_settings = lambda _lc: (False, 0)
        cfg.load_config = lambda: {{"kanban": {{"dispatch_interval_seconds": 1}}}}

        class _Conn:
            def close(self):
                pass

        kb.connect = lambda **_kw: _Conn()
        kb.dispatch_once = lambda *_a, **_kw: time.sleep(600)
        kb.list_boards = lambda **_kw: [{{"slug": "default"}}]
        kb.reap_worker_zombies = lambda: []

        class Host(kw.GatewayKanbanWatchersMixin):
            _running = True
            _kanban_watchdog_probe_interval = 0.15
            _kanban_watchdog_max_strikes = 3

        asyncio.run(Host()._kanban_dispatcher_watcher())
        """.format(root=str(PROJECT_ROOT))
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env={
            "PATH": __import__("os").environ.get("PATH", ""),
            "HOME": __import__("os").environ.get("HOME", ""),
            "HERMES_HOME": str(home),
            "TZ": "UTC",
            "LANG": "C.UTF-8",
            "PYTHONHASHSEED": "0",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 75, (
        f"expected exit 75, got {proc.returncode}; stderr:\n{proc.stderr}"
    )
    marker = json.loads(
        (home / "state" / "dispatcher.last-stall.json").read_text()
    )
    assert marker["exit_code"] == 75
    assert marker["strikes"] == 3
    assert (home / "logs" / "dispatcher-watchdog.log").exists()
