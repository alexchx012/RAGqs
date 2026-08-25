"""Backup write gate reader and in-flight write tracking.

The write gate state is persisted in Postgres by the backup worker (single row
in `backup_write_gate`) so API processes observe the same closing/closed state
after restarts. While the gate is not `open`, new business writes are rejected
with 503 `backup_in_progress`; reads keep working. This module provides the
API-side reader (short in-process cache, fail-open like the maintenance gate
reader) and the process-local in-flight write counter the worker drains
against before moving the gate from `closing` to `closed`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

WRITE_GATE_OPEN = "open"
WRITE_GATE_CLOSING = "closing"
WRITE_GATE_CLOSED = "closed"


class BackupWriteGateReader:
    def __init__(self, engine: Any, *, cache_ttl_seconds: float = 1.0) -> None:
        self._engine = engine
        self._cache_ttl_seconds = cache_ttl_seconds
        self._lock = threading.Lock()
        self._cached_state = WRITE_GATE_OPEN
        self._cached_at = 0.0

    def state(self) -> str:
        now = time.monotonic()
        with self._lock:
            if now - self._cached_at < self._cache_ttl_seconds:
                return self._cached_state
        try:
            from sqlalchemy import select

            from .schema import backup_write_gate_table

            with self._engine.connect() as connection:
                row = connection.execute(
                    select(backup_write_gate_table.c.state).where(backup_write_gate_table.c.id == 1)
                ).scalar_one_or_none()
                state = str(row) if row is not None else WRITE_GATE_OPEN
        except Exception:  # noqa: BLE001 - gate check must not take the API down
            state = WRITE_GATE_OPEN
        with self._lock:
            self._cached_state = state
            self._cached_at = now
        return state

    def writes_open(self) -> bool:
        return self.state() == WRITE_GATE_OPEN


class InFlightWriteTracker:
    """Process-local count of admitted business writes.

    The backup worker waits for `in_flight == 0` after the gate enters
    `closing` so snapshots are taken only after already-admitted writes drain.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._in_flight = 0

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    def inc(self) -> None:
        with self._lock:
            self._in_flight += 1

    def dec(self) -> None:
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)

    @contextmanager
    def track(self) -> Iterator[None]:
        self.inc()
        try:
            yield
        finally:
            self.dec()
