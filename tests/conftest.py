"""Test configuration: make the ``src`` layout importable without installing."""

from __future__ import annotations

import os
import sys

import time

SRC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def wait_until(predicate, timeout: float = 3.0, interval: float = 0.02) -> bool:
    """Poll *predicate* until it returns truthy or *timeout* elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())
