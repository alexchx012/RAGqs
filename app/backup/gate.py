"""Maintenance read gate reader for the API layer.

The gate state itself is persisted in Postgres by the backup/restore
orchestration service (single row in `maintenance_gate`) so workers and
restarted processes observe the same closed/open state. This reader adds a
short in-process cache so the API does not query the gate table on every
request.
"""

from __future__ import annotations

import threading
import time
from typing import Any


class MaintenanceGateReader:
    def __init__(self, engine: Any, *, cache_ttl_seconds: float = 1.0) -> None:
        self._engine = engine
        self._cache_ttl_seconds = cache_ttl_seconds
        self._lock = threading.Lock()
        self._cached_reads_closed = False
        self._cached_at = 0.0

    def reads_closed(self) -> bool:
        now = time.monotonic()
        with self._lock:
            if now - self._cached_at < self._cache_ttl_seconds:
                return self._cached_reads_closed
        try:
            from sqlalchemy import select

            from .schema import maintenance_gate_table

            with self._engine.connect() as connection:
                row = connection.execute(
                    select(maintenance_gate_table.c.reads_closed).where(
                        maintenance_gate_table.c.id == 1
                    )
                ).scalar_one_or_none()
                reads_closed = bool(row)
        except Exception:  # noqa: BLE001 - gate check must not take the API down
            reads_closed = False
        with self._lock:
            self._cached_reads_closed = reads_closed
            self._cached_at = now
        return reads_closed
