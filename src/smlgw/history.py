"""Time-series storage for meter readings, backed by SQLite.

Every numeric OBIS value a meter emits is recorded here so the dashboard can
plot history, not just the latest value.  Two knobs (both configurable from the
Settings page) keep the database bounded:

* ``sample_interval`` -- the minimum spacing between stored points for a given
  (meter, obis) series, so a meter reporting every second does not write 86400
  rows/day per register.
* ``retention_hours`` -- how far back to keep data; older rows are pruned.

The store is safe to call from the several meter worker threads and the web
thread concurrently: a single connection is guarded by a lock (fine for this
write rate), opened with ``check_same_thread=False``.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Callable

_PRUNE_INTERVAL = 60.0  # seconds between opportunistic prunes


@dataclass
class Sample:
    ts: float
    value: float


class HistoryStore:
    def __init__(
        self,
        db_path: str = ":memory:",
        *,
        retention_hours: float = 168.0,
        sample_interval: float = 10.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db_path = db_path
        self.retention_hours = retention_hours
        self.sample_interval = sample_interval
        self.clock = clock
        self._lock = threading.Lock()
        self._last_stored: dict[tuple[str, str], float] = {}
        self._last_prune = 0.0
        if db_path not in (":memory:", "") and (directory := os.path.dirname(os.path.abspath(db_path))):
            os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS samples (
                   ts REAL NOT NULL,
                   meter TEXT NOT NULL,
                   obis TEXT NOT NULL,
                   value REAL NOT NULL
               )"""
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_samples ON samples (meter, obis, ts)"
        )
        self._conn.commit()

    # -- writing ---------------------------------------------------------- #
    def record(self, meter: str, obis: str, value: float, ts: float | None = None) -> bool:
        """Store a sample if enough time has passed since the last one.

        Returns ``True`` if the sample was written, ``False`` if throttled.
        """
        now = self.clock() if ts is None else ts
        key = (meter, obis)
        with self._lock:
            last = self._last_stored.get(key)
            if last is not None and (now - last) < self.sample_interval:
                return False
            self._last_stored[key] = now
            self._conn.execute(
                "INSERT INTO samples (ts, meter, obis, value) VALUES (?, ?, ?, ?)",
                (now, meter, obis, float(value)),
            )
            self._conn.commit()
            self._maybe_prune(now)
        return True

    def _maybe_prune(self, now: float) -> None:
        if now - self._last_prune < _PRUNE_INTERVAL:
            return
        self._last_prune = now
        cutoff = now - self.retention_hours * 3600.0
        self._conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        self._conn.commit()

    def prune(self, now: float | None = None) -> int:
        """Delete samples older than the retention window; return rows removed."""
        now = self.clock() if now is None else now
        cutoff = now - self.retention_hours * 3600.0
        with self._lock:
            cur = self._conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            self._conn.commit()
            self._last_prune = now
            return cur.rowcount

    # -- reading ---------------------------------------------------------- #
    def query(
        self,
        meter: str,
        obis: str,
        *,
        since_seconds: float = 3600.0,
        until: float | None = None,
        max_points: int = 500,
    ) -> list[Sample]:
        """Return samples for a series in the window, downsampled to max_points."""
        now = self.clock() if until is None else until
        start = now - since_seconds
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, value FROM samples WHERE meter=? AND obis=? AND ts>=? AND ts<=? ORDER BY ts",
                (meter, obis, start, now),
            ).fetchall()
        samples = [Sample(ts, value) for ts, value in rows]
        return _downsample(samples, max_points)

    def latest(self, meter: str, obis: str) -> Sample | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT ts, value FROM samples WHERE meter=? AND obis=? ORDER BY ts DESC LIMIT 1",
                (meter, obis),
            ).fetchone()
        return Sample(row[0], row[1]) if row else None

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]

    def update_settings(self, *, retention_hours: float | None = None, sample_interval: float | None = None) -> None:
        if retention_hours is not None:
            self.retention_hours = retention_hours
        if sample_interval is not None:
            self.sample_interval = sample_interval

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _downsample(samples: list[Sample], max_points: int) -> list[Sample]:
    """Average samples into at most *max_points* evenly spaced time buckets."""
    if max_points <= 0 or len(samples) <= max_points:
        return samples
    start = samples[0].ts
    end = samples[-1].ts
    span = (end - start) or 1.0
    out_sum: dict[int, list[float]] = {}
    for s in samples:
        idx = int((s.ts - start) / span * max_points)
        if idx >= max_points:
            idx = max_points - 1
        acc = out_sum.setdefault(idx, [0.0, 0.0, 0.0])
        acc[0] += s.ts
        acc[1] += s.value
        acc[2] += 1
    result = []
    for idx in sorted(out_sum):
        ts_sum, val_sum, n = out_sum[idx]
        result.append(Sample(ts_sum / n, val_sum / n))
    return result
