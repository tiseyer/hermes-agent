"""Gemeinsame Basis für die PG-Backend-Mixins."""
import os
import sys
import time


def now_epoch() -> int:
    return int(time.time())


def claimer_id() -> str:
    """``host:pid``-Kennung dieses Claimers (faithful zu kanban_db._claimer_id)."""
    import socket
    try:
        host = socket.gethostname() or "unknown"
    except Exception:
        host = "unknown"
    return f"{host}:{os.getpid()}"


def default_pid_alive(pid) -> bool:
    """Schlanke, dependency-freie Liveness-Prüfung (POSIX ``kill(pid,0)`` + Linux-Zombie).

    Faithful-genuger Default für den reinen DB-Kern; die Watchers-Welle kann die
    volle ``gateway.status``-Variante injizieren. Bewusst KEIN hermes_cli-Import,
    damit ``kanban_pg`` in der schlanken CI-Umgebung ladbar bleibt.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # existiert, gehört uns nur nicht
    except OSError:
        return False
    if sys.platform == "linux":
        try:
            with open(f"/proc/{pid}/status", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("State:"):
                        if "Z" in line.split(":", 1)[1]:
                            return False
                        break
        except OSError:
            pass
    return True
