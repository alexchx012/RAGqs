"""Retirement worker: durable account-retirement processing and idempotent retry.

Retention-ops calls `RetireAccountNotificationState` with mode="durable"; the
command is persisted as accepted and this worker applies the durable work
inside its own fenced transaction, advancing the command to completed. Any
transient failure keeps the command accepted so retention (or the worker loop)
may retry with the same operation id.
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

from .schema import outbox_retirement_command_table

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RetirementWorkCommand:
    operation_id: str
    user_id: str
    deletion_id: str
    verified_archive_ref: str
    archive_checksum: str
    transaction_id: str
    canonical_input_fingerprint: str


@dataclass(frozen=True, slots=True)
class RetirementWorkerStats:
    completed: int
    deferred: int


def build_retirement_processor(lifecycle: Any) -> Any:
    """Build the NARROW scoped processor that applies the durable retirement
    work for one accepted operation.

    The processor calls the lifecycle's INTERNAL no-token entry
    `apply_accepted_durable_retirement(operation_id)`, which can only process
    commands already accepted in the database. There is NO token and NO
    signing secret anywhere in the closure or the worker object graph; the
    callback cannot construct arbitrary retirement commands.
    """

    def process(command: RetirementWorkCommand, *, connection: Connection) -> dict[str, object]:
        receipt = lifecycle.apply_accepted_durable_retirement(
            command.operation_id,
            connection=connection,
        )
        return {
            "operation_id": receipt.operation_id,
            "state": receipt.state,
            "notification_retired_count": receipt.notification_retired_count,
        }

    return process


class RetirementWorker:
    """Applies accepted durable retirement commands under a fence.

    The worker holds ONLY a narrow scoped processor callback (built at
    assembly time around the lifecycle's internal no-token entry). It has no
    token/secret attribute and no lifecycle+token combination, so it can only
    process accepted tasks already present in the database — it can never be
    used to construct an arbitrary retention command.
    """

    def __init__(
        self,
        worker_runtime: WorkerRuntime,
        *,
        processor: Any = None,
    ) -> None:
        self._worker_runtime = worker_runtime
        if not callable(processor):
            raise RuntimeError("retirement processor is not configured")
        self._processor = processor

    def list_accepted_commands(self, *, limit: int = 100) -> list[RetirementWorkCommand]:
        with self._worker_runtime.runtime.resolve("database_engine").connect() as connection:
            rows = (
                connection.execute(
                    select(outbox_retirement_command_table)
                    .where(
                        outbox_retirement_command_table.c.mode == "durable",
                        outbox_retirement_command_table.c.state == "accepted",
                    )
                    .limit(limit)
                )
                .mappings()
                .all()
            )
        return [
            RetirementWorkCommand(
                operation_id=str(row["operation_id"]),
                user_id=str(row["user_id"]),
                deletion_id=str(row["deletion_id"]),
                verified_archive_ref=str(row["archive_ref"]),
                archive_checksum=str(row["archive_checksum"]),
                transaction_id="",
                canonical_input_fingerprint=str(row["input_fingerprint"]),
            )
            for row in rows
        ]

    def run_once(self, *, owner: str, limit: int = 100) -> RetirementWorkerStats:
        normalized_owner = owner.strip()
        if not normalized_owner:
            raise ValueError("worker owner must not be empty")
        completed = 0
        deferred = 0
        for command in self.list_accepted_commands(limit=limit):
            try:
                self._worker_runtime.run_task(
                    f"outbox-retirement:{command.operation_id}",
                    normalized_owner,
                    self._apply_for(command),
                )
            except (FenceViolation, PlatformError, RuntimeError):
                deferred += 1
            else:
                completed += 1
        return RetirementWorkerStats(completed=completed, deferred=deferred)

    def _apply_for(self, command: RetirementWorkCommand):
        def run(context: Any, connection: Connection) -> dict[str, object]:
            return self._apply(context, connection, command=command)

        return run

    def _apply(
        self,
        context: Any,
        connection: Connection,
        *,
        command: RetirementWorkCommand,
    ) -> dict[str, object]:
        del context
        return self._processor(command, connection=connection)

    def close(self) -> None:
        self._worker_runtime.close()

    def run_forever(
        self,
        *,
        owner: str,
        interval_seconds: float = 5.0,
        limit: int = 100,
        stop: Any = None,
    ) -> None:
        """Continuous retirement loop: accepted commands, transient retries
        and newly accepted commands. `stop()` returns True to exit gracefully."""
        import time

        while True:
            if stop is not None and stop():
                return
            try:
                self.run_once(owner=owner, limit=limit)
            except Exception:
                _logger.exception("outbox retirement loop iteration failed")
            time.sleep(interval_seconds)


def create_retirement_worker(
    settings: PlatformSettings,
    *,
    runtime: PlatformRuntime | None = None,
    owner: str | None = None,
    limit: int = 100,
) -> tuple[RetirementWorker, RetirementWorkerStats]:
    """Resolve the already-assembled retirement worker from the runtime.

    The worker holds only its narrow processor, which calls the lifecycle's
    INTERNAL no-token entry `apply_accepted_durable_retirement`; there is no
    token and no signing secret anywhere in the worker, its processor or the
    runtime adapter graph. `build_runtime` assembled it; this function never
    touches a token, a master secret or a raw lifecycle.
    """
    owns_runtime = runtime is None
    runtime = runtime or build_runtime(settings)
    try:
        resolved_owner = owner or f"outbox-retirement:{socket.gethostname()}:{os.getpid()}"
        worker = runtime.resolve("retirement_worker", None)
        if worker is None:
            raise RuntimeError("retirement worker is not configured")
        try:
            return worker, worker.run_once(owner=resolved_owner, limit=limit)
        finally:
            worker.close()
    finally:
        if owns_runtime:
            runtime.close()


def main() -> None:
    """Resident retirement loop entrypoint (console script)."""
    settings = load_platform_settings()
    runtime = build_runtime(settings)
    try:
        resolved_owner = f"outbox-retirement:{socket.gethostname()}:{os.getpid()}"
        worker = runtime.resolve("retirement_worker", None)
        if worker is None:
            raise RuntimeError("retirement worker is not configured")
        try:
            _logger.info("outbox retirement resident loop starting owner=%s", resolved_owner)
            worker.run_forever(owner=resolved_owner)
        finally:
            worker.close()
    finally:
        runtime.close()
