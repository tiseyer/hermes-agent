"""Gemeinsame Basis für die PG-Backend-Mixins."""
import time


def now_epoch() -> int:
    return int(time.time())
