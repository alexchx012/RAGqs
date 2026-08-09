"""Outbox-owned compaction worker: re-evaluates accepted compaction commands.

A compaction command is an asynchronous request, not a promise that every
event was compacted. This worker re-examines the causally-associated full
events of the retired account under a fence: fully-delivered events are
compacted atomically, blocked events keep the command accepted, and only when
nothing is blocked does the command advance to completed with cumulative
counts.
"""

from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Connection

from app.platform.config import PlatformSettings, load_platform_settings
from app.platform.errors import PlatformError
from app.platform.persistence import FenceViolation
from app.platform.runtime import PlatformRuntime, build_runtime
from app.platform.worker import WorkerRuntime

from .lifecycle import SqlAlchemyOutboxLifecycle
from .schema import outbox_compaction_command_table

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CompactionWorkerStats:
    processed: int
    completed: int
    deferred: int


class CompactionWorker:
    """Applies accepted eligible-compaction commands under a fence.

    The worker holds the lifecycle object INJECTED at assembly time (it is
    never resolved from the runtime adapters; the raw lifecycle is not
    registered there). It only processes accepted command rows through the
    lifecycle's internal no-token entry and carries no token/secret.
    """

    def __init__(
        self,
        worker_runtime: WorkerRuntime,
        *,
        lifecycle: SqlAlchemyOutboxLifecycle | None = None,
    ) -> None:
        self._worker_runtime = worker_runtime
        if lifecycle is None or not hasattr(lifecycle, "apply_compaction_command"):
            raise RuntimeError("outbox lifecycle is not configured")
        self._lifecycle = lifecycle

    def list_accepted_commands(self, *, limit: int = 100) -> list[str]:
        with self._worker_runtime.runtime.resolve("database_engine").connect() as connection:
            return [
                str(operation_id)
                for operation_id in connection.execute(
                    select(outbox_compaction_command_table.c.operation_id)
                    .where(outbox_compaction_command_table.c.state == "accepted")
                    .limit(limit)
                ).scalars()
            ]

    def run_once(self, *, owner: str, limit: int = 100) -> CompactionWorkerStats:
        normalized_owner = owner.strip()
        if not normalized_owner:
            raise ValueError("worker owner must not be empty")
        processed = 0
        completed = 0
        deferred = 0
        for operation_id in self.list_accepted_commands(limit=limit):
            try:
                result = self._worker_runtime.run_task(
                    f"outbox-compaction:{operation_id}",
                    normalized_owner,
                    self._apply_for(operation_id),
                )
            except (FenceViolation, PlatformError, RuntimeError):
                deferred += 1
            else:
                processed += 1
                if result.get("state") == "completed":
                    completed += 1
        return CompactionWorkerStats(
            processed=processed,
            completed=completed,
            deferred=deferred,
        )

    def _apply_for(self, operation_id: str):
        def run(context: Any, connection: Connection) -> dict[str, object]:
            return self._apply(context, connection, operation_id=operation_id)

        return run

    def _apply(
        self,
        context: Any,
        connection: Connection,
        *,
        operation_id: str,
    ) -> dict[str, object]:
        del context
        return self._lifecycle.apply_compaction_command(operation_id, connection=connection)

    def run_forever(
        self,
        *,
        owner: str,
        interval_seconds: float = 60.0,
        limit: int = 100,
        stop: Any = None,
    ) -> None:
        """Continuous compaction loop; stops when `stop()` returns True."""
        import time

        while True:
            if stop is not None and stop():
                return
            try:
                self.run_once(owner=owner, limit=limit)
            except Exception:
                _logger.exception("outbox compaction loop iteration failed")
            time.sleep(interval_seconds)

    def close(self) -> None:
        self._worker_runtime.close()


def create_compaction_worker(
    settings: PlatformSettings,
    *,
    runtime: PlatformRuntime | None = None,
    owner: str | None = None,
    limit: int = 100,
) -> tuple[CompactionWorker, CompactionWorkerStats]:
    """Resolve the already-assembled compaction worker from the runtime.

    The worker (with its injected lifecycle) is built inside `build_runtime`;
    this function never rebuilds `CompactionWorker(worker_runtime)` and never
    touches the raw lifecycle or any secret.
    """
    owns_runtime = runtime is None
    runtime = runtime or build_runtime(settings)
    try:
        resolved_owner = owner or f"outbox-compaction:{socket.gethostname()}:{os.getpid()}"
        worker = runtime.resolve("compaction_worker", None)
        if worker is None:
            raise RuntimeError("compaction worker is not configured")
        try:
            return worker, worker.run_once(owner=resolved_owner, limit=limit)
        finally:
            worker.close()
    finally:
        if owns_runtime:
            runtime.close()


def run_compaction_worker_once(
    settings: PlatformSettings,
    *,
    runtime: PlatformRuntime | None = None,
    owner: str | None = None,
    limit: int = 100,
) -> CompactionWorkerStats:
    _, stats = create_compaction_worker(settings, runtime=runtime, owner=owner, limit=limit)
    return stats


def main() -> None:
    """Resident compaction loop entrypoint (console script).

    Resolves the already-assembled `compaction_worker` from the runtime and
    enters run_forever; it never rebuilds a worker from a raw lifecycle.
    """
    settings = load_platform_settings()
    runtime = build_runtime(settings)
    try:
        resolved_owner = f"outbox-compaction:{socket.gethostname()}:{os.getpid()}"
        worker = runtime.resolve("compaction_worker", None)
        if worker is None:
            raise RuntimeError("compaction worker is not configured")
        try:
            _logger.info("outbox compaction resident loop starting owner=%s", resolved_owner)
            worker.run_forever(owner=resolved_owner)
        finally:
            worker.close()
    finally:
        runtime.close()
