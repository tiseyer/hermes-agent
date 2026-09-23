"""Dispatcher liveness watchdog: heartbeat file + stall hard-exit.

The kanban dispatcher loop (``kanban_db.run_daemon``) survives *exceptions* —
every tick runs inside try/except — but it cannot survive a *hang*: a tick
that blocks forever (e.g. an SQLite lock wait inside the dispatch path) stops
the loop silently, and the service supervisor only sees "PID alive". This
module provides the out-of-loop backstop, ported from the upstream gateway
pattern (``gateway/shutdown_watchdog.py``), self-contained for our fork:

* :func:`write_heartbeat` — atomically rewrites
  ``<HERMES_HOME>/state/dispatcher.heartbeat`` (JSON: pid, tick_count,
  last_tick_ok, monotonic + wall timestamps) so supervisors and humans can
  tell "process alive" from "loop frozen". Never raises.
* :func:`start_stall_watchdog` — a daemon OS-thread that samples a tick
  counter every ``probe_interval`` seconds. After ``max_strikes`` consecutive
  probes without progress it dumps all thread stacks to
  ``<HERMES_HOME>/logs/dispatcher-watchdog.log``, writes
  ``<HERMES_HOME>/state/dispatcher.last-stall.json`` for the next health
  check to surface, and hard-exits with :data:`DISPATCHER_RESTART_EXIT_CODE`
  (75). The systemd unit ``hermes-gateway.service`` already carries
  ``RestartForceExitStatus=75`` + ``Restart=always``, so exit 75 IS the
  restart channel — no unit change required.

Kill switch: ``HERMES_DISPATCHER_WATCHDOG=0`` (or ``false``/``no``/``off``)
disables the watchdog thread entirely — the documented rollback path if the
stall threshold ever false-positives into a restart loop (drop-in with
``Environment=HERMES_DISPATCHER_WATCHDOG=0`` + restart, no revert needed).
"""

from __future__ import annotations

import faulthandler
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from hermes_constants import get_hermes_home
from utils import atomic_json_write

_log = logging.getLogger(__name__)

# Matches RestartForceExitStatus=75 in hermes-gateway.service: systemd
# force-restarts on this code even where Restart= would not.
DISPATCHER_RESTART_EXIT_CODE = 75

# Probe cadence and strike budget mirror the upstream loop-liveness watchdog
# (DEFAULT_LOOP_WATCHDOG_INTERVAL_S=30, MAX_STRIKES=3). Our dispatcher ticks
# legitimately take long when workers are spawned, so run_daemon widens the
# probe interval to at least the tick interval (stall threshold
# >= max(3*interval, 180s)) — a false-positive restart is expensive.
DEFAULT_PROBE_INTERVAL_S = 60.0
DEFAULT_MAX_STRIKES = 3

# Exponential tick backoff cap for run_daemon on consecutive failing ticks.
BACKOFF_CAP_S = 900.0

WATCHDOG_ENV_VAR = "HERMES_DISPATCHER_WATCHDOG"
_DISABLED_VALUES = {"0", "false", "no", "off"}

_HEARTBEAT_RELATIVE = ("state", "dispatcher.heartbeat")
_STALL_MARKER_RELATIVE = ("state", "dispatcher.last-stall.json")
_DUMP_RELATIVE = ("logs", "dispatcher-watchdog.log")


def watchdog_enabled() -> bool:
    """Return False when the kill switch env var disables the watchdog."""
    raw = os.environ.get(WATCHDOG_ENV_VAR, "").strip().lower()
    return raw not in _DISABLED_VALUES


def _home(home: Optional[Path]) -> Path:
    return Path(home) if home is not None else get_hermes_home()


def get_heartbeat_path(home: Optional[Path] = None) -> Path:
    return _home(home).joinpath(*_HEARTBEAT_RELATIVE)


def get_stall_marker_path(home: Optional[Path] = None) -> Path:
    return _home(home).joinpath(*_STALL_MARKER_RELATIVE)


def get_watchdog_dump_path(home: Optional[Path] = None) -> Path:
    return _home(home).joinpath(*_DUMP_RELATIVE)


def backoff_wait_seconds(
    interval: float, consecutive_failures: int, cap: float = BACKOFF_CAP_S
) -> float:
    """Plain ``interval`` while healthy; doubles per consecutive failing
    tick, capped — so a persistently failing dispatcher stops hammering
    the DB while it has no chance of making progress."""
    if consecutive_failures <= 0:
        return interval
    return min(interval * (2 ** (consecutive_failures - 1)), cap)


def write_heartbeat(
    *,
    tick_count: int,
    last_tick_ok: bool,
    pid: Optional[int] = None,
    home: Optional[Path] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Path:
    """Atomically rewrite the dispatcher heartbeat file; never raises.

    ``tick_count`` is the loop's monotonic progress counter; ``last_tick_ok``
    distinguishes "alive but failing every tick" from "actually dispatching".
    """
    path = get_heartbeat_path(home)
    payload: Dict[str, Any] = {
        "pid": int(pid if pid is not None else os.getpid()),
        "tick_count": int(tick_count),
        "last_tick_ok": bool(last_tick_ok),
        "ts_monotonic": time.monotonic(),
        "ts_wall": time.time(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        payload.update(extra)
    try:
        atomic_json_write(path, payload, indent=None)
    except Exception:
        _log.debug("dispatcher heartbeat write failed", exc_info=True)
    return path


class StallWatchdogHandle:
    """Owner handle for a running stall watchdog thread."""

    def __init__(self, stop_event: threading.Event, thread: threading.Thread):
        self._stop_event = stop_event
        self._thread = thread

    def stop(self) -> None:
        self._stop_event.set()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def join(self, timeout: Optional[float] = None) -> None:
        self._thread.join(timeout)


def _write_stall_dump(dump_path: Path, info: Dict[str, Any]) -> None:
    """Best-effort faulthandler stack dump (all threads), appended."""
    try:
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        with open(dump_path, "a", encoding="utf-8") as fh:
            fh.write(
                "\n=== dispatcher stall %s pid=%s tick_count=%s strikes=%s ===\n"
                % (
                    datetime.now(timezone.utc).isoformat(),
                    info.get("pid"),
                    info.get("tick_count"),
                    info.get("strikes"),
                )
            )
            fh.flush()
            faulthandler.dump_traceback(file=fh, all_threads=True)
    except Exception:
        _log.debug("dispatcher stall stack dump failed", exc_info=True)


def start_stall_watchdog(
    get_tick_count: Callable[[], int],
    *,
    probe_interval: float = DEFAULT_PROBE_INTERVAL_S,
    max_strikes: int = DEFAULT_MAX_STRIKES,
    on_stall: Optional[Callable[[Dict[str, Any]], None]] = None,
    home: Optional[Path] = None,
    exit_code: int = DISPATCHER_RESTART_EXIT_CODE,
) -> Optional[StallWatchdogHandle]:
    """Start the out-of-loop stall watchdog thread.

    Samples ``get_tick_count()`` every ``probe_interval`` seconds. After
    ``max_strikes`` consecutive probes without progress: stack dump, stall
    marker, then ``os._exit(exit_code)`` so the supervisor restarts us.

    ``on_stall`` is a test seam: when provided it is called with the stall
    info dict INSTEAD of the hard exit (the thread then terminates).
    Returns None when the thread cannot be started (never raises).
    """
    stop_event = threading.Event()
    resolved_home = _home(home)

    def _watchdog() -> None:
        try:
            last_seen = int(get_tick_count())
        except Exception:
            last_seen = 0
        strikes = 0
        while not stop_event.wait(timeout=probe_interval):
            try:
                current = int(get_tick_count())
            except Exception:
                _log.debug("stall watchdog: tick probe failed", exc_info=True)
                continue
            if current != last_seen:
                last_seen = current
                strikes = 0
                continue
            strikes += 1
            if strikes < max_strikes:
                continue
            if stop_event.is_set():  # a late stop() wins over the kill
                return
            dump_path = get_watchdog_dump_path(resolved_home)
            info: Dict[str, Any] = {
                "pid": os.getpid(),
                "tick_count": last_seen,
                "strikes": strikes,
                "probe_interval_s": probe_interval,
                "detected_at": datetime.now(timezone.utc).isoformat(),
                "dump_path": str(dump_path),
                "exit_code": exit_code,
            }
            _log.critical(
                "kanban dispatcher loop made no tick progress for %d "
                "consecutive probes (%.0fs each, tick_count frozen at %d); "
                "dumping thread stacks to %s and exiting %d so systemd "
                "(RestartForceExitStatus=%d) restarts the service.",
                strikes,
                probe_interval,
                last_seen,
                dump_path,
                exit_code,
                exit_code,
            )
            _write_stall_dump(dump_path, info)
            try:
                atomic_json_write(get_stall_marker_path(resolved_home), info)
            except Exception:
                _log.debug("dispatcher stall marker write failed", exc_info=True)
            if on_stall is not None:
                try:
                    on_stall(info)
                except Exception:
                    _log.debug("stall watchdog on_stall hook failed", exc_info=True)
                return
            os._exit(exit_code)

    thread = threading.Thread(
        target=_watchdog, daemon=True, name="dispatcher-stall-watchdog"
    )
    try:
        thread.start()
    except Exception:
        _log.warning("failed to start dispatcher stall watchdog", exc_info=True)
        return None
    return StallWatchdogHandle(stop_event, thread)
