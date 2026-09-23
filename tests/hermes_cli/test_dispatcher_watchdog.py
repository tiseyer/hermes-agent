"""Tests for the dispatcher liveness watchdog (Paket 1, Loop-Freeze-Härtung).

Covers the production failure mode from 2026-09-23 08:52: a dispatch tick
that *hangs* (DB lock wait) instead of raising froze ``run_daemon`` forever
with no error. The watchdog must:

* fire (exit 75 / injected ``on_stall``) after ``max_strikes`` probes
  without tick progress — reproduced here with a frozen ``dispatch_once``;
* stay silent while ticks progress, even failing ones (progress != health);
* write the heartbeat file every iteration, success and failure alike;
* be fully disabled by the ``HERMES_DISPATCHER_WATCHDOG=0`` kill switch.

All subprocess children run with an explicitly sandboxed ``HERMES_HOME``
(tmp_path) — never against the real ``~/.hermes``.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import dispatcher_watchdog as wd
from hermes_cli import kanban_db as kb

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def wd_home(tmp_path, monkeypatch):
    """Explicitly sandboxed HERMES_HOME for heartbeat/dump/marker paths."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _wait_for(predicate, timeout: float = 10.0, step: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


# ---------------------------------------------------------------------------
# Kill switch + helpers
# ---------------------------------------------------------------------------

def test_watchdog_enabled_default_and_kill_switch(monkeypatch):
    monkeypatch.delenv(wd.WATCHDOG_ENV_VAR, raising=False)
    assert wd.watchdog_enabled() is True
    for off in ("0", "false", "no", "off", " FALSE ", "Off"):
        monkeypatch.setenv(wd.WATCHDOG_ENV_VAR, off)
        assert wd.watchdog_enabled() is False, off
    for on in ("", "1", "true", "yes", "anything"):
        monkeypatch.setenv(wd.WATCHDOG_ENV_VAR, on)
        assert wd.watchdog_enabled() is True, on


def test_backoff_wait_seconds_formula():
    assert wd.backoff_wait_seconds(60.0, 0) == 60.0
    assert wd.backoff_wait_seconds(60.0, 1) == 60.0
    assert wd.backoff_wait_seconds(60.0, 2) == 120.0
    assert wd.backoff_wait_seconds(60.0, 3) == 240.0
    # Capped at 15 min.
    assert wd.backoff_wait_seconds(60.0, 20) == wd.BACKOFF_CAP_S == 900.0


# ---------------------------------------------------------------------------
# Heartbeat writer
# ---------------------------------------------------------------------------

def test_write_heartbeat_atomic_json_fields(wd_home):
    path = wd.write_heartbeat(tick_count=1, last_tick_ok=True, home=wd_home)
    assert path == wd_home / "state" / "dispatcher.heartbeat"
    data = _read_json(path)
    assert data["pid"] == os.getpid()
    assert data["tick_count"] == 1
    assert data["last_tick_ok"] is True
    assert data["ts_monotonic"] > 0
    assert data["ts_wall"] > 0
    assert "updated_at" in data

    wd.write_heartbeat(tick_count=2, last_tick_ok=False, home=wd_home)
    data = _read_json(path)
    assert data["tick_count"] == 2
    assert data["last_tick_ok"] is False
    # Atomic write leaves no temp litter behind.
    leftovers = [p for p in path.parent.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_write_heartbeat_never_raises(tmp_path):
    # Unwritable target (a file where the parent dir should be).
    blocker = tmp_path / "state"
    blocker.write_text("not a dir", encoding="utf-8")
    wd.write_heartbeat(tick_count=1, last_tick_ok=True, home=tmp_path)  # no raise


# ---------------------------------------------------------------------------
# Stall watchdog thread (unit level, on_stall injected)
# ---------------------------------------------------------------------------

def test_stall_watchdog_fires_after_max_strikes(wd_home):
    fired = threading.Event()
    info: dict = {}

    def on_stall(payload):
        info.update(payload)
        fired.set()

    handle = wd.start_stall_watchdog(
        lambda: 7,  # frozen counter
        probe_interval=0.05,
        max_strikes=3,
        on_stall=on_stall,
        home=wd_home,
    )
    try:
        assert fired.wait(5.0), "watchdog did not fire on frozen counter"
    finally:
        handle.stop()
    assert info["strikes"] == 3
    assert info["tick_count"] == 7
    assert info["exit_code"] == 75
    dump = wd.get_watchdog_dump_path(wd_home)
    assert dump.exists()
    assert "dispatcher stall" in dump.read_text(encoding="utf-8")
    marker = _read_json(wd.get_stall_marker_path(wd_home))
    assert marker["strikes"] == 3
    assert marker["dump_path"] == str(dump)


def test_stall_watchdog_quiet_while_ticks_progress(wd_home):
    fired = threading.Event()
    counter = {"n": 0}

    def get_tick(counter=counter):
        counter["n"] += 1  # progresses on every probe
        return counter["n"]

    handle = wd.start_stall_watchdog(
        get_tick,
        probe_interval=0.03,
        max_strikes=2,
        on_stall=lambda _info: fired.set(),
        home=wd_home,
    )
    try:
        time.sleep(0.5)  # many probe windows
        assert not fired.is_set(), "watchdog fired despite tick progress"
        assert handle.is_alive()
    finally:
        handle.stop()
    handle.join(2.0)
    assert not handle.is_alive()


def test_stall_watchdog_stop_prevents_fire(wd_home):
    fired = threading.Event()
    handle = wd.start_stall_watchdog(
        lambda: 0,
        probe_interval=0.5,
        max_strikes=1,
        on_stall=lambda _info: fired.set(),
        home=wd_home,
    )
    handle.stop()
    handle.join(2.0)
    assert not fired.is_set()


# ---------------------------------------------------------------------------
# run_daemon integration (in-process, stubbed dispatch)
# ---------------------------------------------------------------------------

class _DummyConn:
    def close(self):
        pass


def _stub_connect(monkeypatch):
    monkeypatch.setattr(kb, "connect", lambda **_kw: _DummyConn())


def test_run_daemon_frozen_tick_invokes_stall(wd_home, monkeypatch):
    """The 08:52 freeze in miniature: dispatch_once blocks forever, the
    tick counter stays frozen, the watchdog fires after 3 missed probes."""
    _stub_connect(monkeypatch)
    release = threading.Event()

    def _frozen(_conn, **_kw):
        release.wait(60)  # simulated DB-lock hang; released at teardown

    monkeypatch.setattr(kb, "dispatch_once", _frozen)

    fired = threading.Event()
    info: dict = {}

    def on_stall(payload):
        info.update(payload)
        fired.set()

    stop = threading.Event()
    t = threading.Thread(
        target=kb.run_daemon,
        kwargs=dict(
            interval=0.05,
            stop_event=stop,
            watchdog_probe_interval=0.1,
            watchdog_max_strikes=3,
            watchdog_on_stall=on_stall,
        ),
        daemon=True,
    )
    t.start()
    try:
        assert fired.wait(10.0), "watchdog did not fire on frozen tick"
    finally:
        stop.set()
        release.set()
        t.join(5.0)
    assert info["strikes"] == 3
    assert info["tick_count"] == 0, "hung first tick must freeze the counter"
    assert wd.get_watchdog_dump_path(wd_home).exists()


def test_run_daemon_normal_ticks_watchdog_silent(wd_home, monkeypatch):
    """Healthy loop over many probe windows: watchdog stays quiet, the
    heartbeat file advances every tick."""
    _stub_connect(monkeypatch)
    monkeypatch.setattr(kb, "dispatch_once", lambda _conn, **_kw: kb.DispatchResult())

    fired = threading.Event()
    ticks = []
    stop = threading.Event()
    t = threading.Thread(
        target=kb.run_daemon,
        kwargs=dict(
            interval=0.03,
            stop_event=stop,
            on_tick=lambda res: ticks.append(res),
            watchdog_probe_interval=0.06,
            watchdog_max_strikes=2,
            watchdog_on_stall=lambda _info: fired.set(),
        ),
        daemon=True,
    )
    t.start()
    try:
        assert _wait_for(lambda: len(ticks) >= 8), "daemon did not tick"
        time.sleep(0.3)  # several extra probe windows on top
        assert not fired.is_set(), "watchdog fired despite healthy ticks"
    finally:
        stop.set()
        t.join(5.0)
    hb = _read_json(wd.get_heartbeat_path(wd_home))
    assert hb["tick_count"] >= 8
    assert hb["last_tick_ok"] is True
    assert hb["pid"] == os.getpid()


def test_run_daemon_failing_ticks_progress_and_mark_heartbeat(wd_home, monkeypatch):
    """Exceptions are progress, not stall: the loop survives, the watchdog
    stays quiet, and the heartbeat records last_tick_ok=False."""
    _stub_connect(monkeypatch)
    calls = {"n": 0}

    def _boom(_conn, **_kw):
        calls["n"] += 1
        raise RuntimeError("simulated tick failure")

    monkeypatch.setattr(kb, "dispatch_once", _boom)

    fired = threading.Event()
    stop = threading.Event()
    t = threading.Thread(
        target=kb.run_daemon,
        kwargs=dict(
            interval=0.02,
            stop_event=stop,
            watchdog_probe_interval=0.05,
            watchdog_max_strikes=3,
            watchdog_on_stall=lambda _info: fired.set(),
        ),
        daemon=True,
    )
    t.start()
    try:
        assert _wait_for(lambda: calls["n"] >= 3), "loop died on tick exception"
        assert not fired.is_set(), "watchdog fired on failing-but-alive loop"
    finally:
        stop.set()
        t.join(5.0)
    hb = _read_json(wd.get_heartbeat_path(wd_home))
    assert hb["last_tick_ok"] is False
    assert hb["tick_count"] >= 3


def test_run_daemon_kill_switch_starts_no_watchdog(wd_home, monkeypatch):
    """HERMES_DISPATCHER_WATCHDOG=0: no watchdog thread, and a frozen tick
    does NOT trigger a stall callback."""
    monkeypatch.setenv(wd.WATCHDOG_ENV_VAR, "0")
    _stub_connect(monkeypatch)
    release = threading.Event()
    started = threading.Event()

    def _frozen(_conn, **_kw):
        started.set()
        release.wait(60)

    monkeypatch.setattr(kb, "dispatch_once", _frozen)

    fired = threading.Event()
    stop = threading.Event()
    t = threading.Thread(
        target=kb.run_daemon,
        kwargs=dict(
            interval=0.05,
            stop_event=stop,
            watchdog_probe_interval=0.05,
            watchdog_max_strikes=2,
            watchdog_on_stall=lambda _info: fired.set(),
        ),
        daemon=True,
    )
    t.start()
    try:
        assert started.wait(5.0)
        time.sleep(0.5)  # would be ~10 strike windows if armed
        names = [th.name for th in threading.enumerate()]
        assert "dispatcher-stall-watchdog" not in names
        assert not fired.is_set()
    finally:
        stop.set()
        release.set()
        t.join(5.0)


# ---------------------------------------------------------------------------
# Hard-exit path (subprocess: real os._exit(75))
# ---------------------------------------------------------------------------

_SUBPROC_PRELUDE = """
import sys, threading, time
sys.path.insert(0, {root!r})
from hermes_cli import kanban_db as kb

class _Conn:
    def close(self):
        pass

kb.connect = lambda **_kw: _Conn()
"""


def _subprocess_env(home: Path) -> dict:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "HERMES_HOME": str(home),
        "TZ": "UTC",
        "LANG": "C.UTF-8",
        "PYTHONHASHSEED": "0",
    }


def test_run_daemon_frozen_tick_exits_75_subprocess(tmp_path):
    """Full hard-exit proof: frozen tick -> os._exit(75) after 3 missed
    probes, stack dump + stall marker written under the sandboxed home."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    script = textwrap.dedent(_SUBPROC_PRELUDE.format(root=str(PROJECT_ROOT))) + textwrap.dedent(
        """
        def _frozen(_conn, **_kw):
            time.sleep(600)
        kb.dispatch_once = _frozen
        kb.run_daemon(interval=0.05, watchdog_probe_interval=0.15,
                      watchdog_max_strikes=3)
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env=_subprocess_env(home),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 75, (
        f"expected exit 75, got {proc.returncode}; stderr:\n{proc.stderr}"
    )
    dump = home / "logs" / "dispatcher-watchdog.log"
    assert dump.exists(), "watchdog stack dump missing"
    dump_text = dump.read_text(encoding="utf-8")
    assert "dispatcher stall" in dump_text
    assert "Thread" in dump_text  # faulthandler stack content
    marker = _read_json(home / "state" / "dispatcher.last-stall.json")
    assert marker["exit_code"] == 75
    assert marker["strikes"] == 3
    assert marker["tick_count"] == 0


def test_run_daemon_kill_switch_no_exit_75_subprocess(tmp_path):
    """Kill switch proof: frozen tick + HERMES_DISPATCHER_WATCHDOG=0 ->
    the process does NOT exit 75; it survives many would-be strike windows."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    script = textwrap.dedent(_SUBPROC_PRELUDE.format(root=str(PROJECT_ROOT))) + textwrap.dedent(
        """
        def _frozen(_conn, **_kw):
            time.sleep(600)
        kb.dispatch_once = _frozen
        kb.run_daemon(interval=0.05, watchdog_probe_interval=0.1,
                      watchdog_max_strikes=2)
        """
    )
    env = _subprocess_env(home)
    env["HERMES_DISPATCHER_WATCHDOG"] = "0"
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        time.sleep(3.0)  # ~15 strike windows had the watchdog been armed
        assert proc.poll() is None, (
            f"process exited (rc={proc.returncode}) despite kill switch"
        )
    finally:
        proc.kill()
        proc.wait(10)
    assert proc.returncode != 75
    assert not (home / "state" / "dispatcher.last-stall.json").exists()


def test_run_daemon_normal_subprocess_survives_and_stops_clean(tmp_path):
    """No false positive at process level: healthy fast ticks across many
    probe windows -> no exit; SIGTERM then stops the daemon cleanly (rc 0)."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    script = textwrap.dedent(_SUBPROC_PRELUDE.format(root=str(PROJECT_ROOT))) + textwrap.dedent(
        """
        kb.dispatch_once = lambda _conn, **_kw: kb.DispatchResult()
        kb.run_daemon(interval=0.05, watchdog_probe_interval=0.1,
                      watchdog_max_strikes=3)
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        env=_subprocess_env(home),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        heartbeat = home / "state" / "dispatcher.heartbeat"
        assert _wait_for(heartbeat.exists, timeout=15.0), "heartbeat never appeared"
        assert _wait_for(
            lambda: proc.poll() is None and _read_json(heartbeat)["tick_count"] >= 5,
            timeout=15.0,
        ), "tick_count did not advance"
        time.sleep(1.0)  # several more probe windows
        assert proc.poll() is None, f"daemon exited early (rc={proc.returncode})"
        hb = _read_json(heartbeat)
        assert hb["tick_count"] >= 5
        assert hb["last_tick_ok"] is True
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(15)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
    assert rc == 0, "SIGTERM should stop the daemon cleanly"
