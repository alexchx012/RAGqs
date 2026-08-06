from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass
from functools import partial

from sqlalchemy.engine import Connection

from app.platform.config import PlatformSettings, load_platform_settings
from app.platform.context import TaskContext
from app.platform.errors import PlatformError
from app.platform.persistence import FenceViolation, LeaseUnavailable
from app.platform.runtime import PlatformRuntime
from app.platform.worker import WorkerRuntime, create_worker_runtime

from .service import IdentityAccessService

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IdentityDeletionWorkerStats:
    completed: int
    deferred: int


class IdentityDeletionWorker:
    """Leased worker that finalizes due identity-deletion workflows."""

    def __init__(self, worker_runtime: WorkerRuntime) -> None:
        self._worker_runtime = worker_runtime
        identity_access = worker_runtime.runtime.resolve("identity_access")
        if not isinstance(identity_access, IdentityAccessService):
            raise RuntimeError("identity access service is not configured")
        self._identity_access = identity_access

    def run_once(self, *, owner: str, limit: int = 100) -> IdentityDeletionWorkerStats:
        normalized_owner = owner.strip()
        if not normalized_owner:
            raise ValueError("worker owner must not be empty")
        completed = 0
        deferred = 0
        for operation_id in self._identity_access.list_pending_object_cleanup_operations(
            limit=limit
        ):
            try:
                self._worker_runtime.run_task(
                    f"identity-object-cleanup:{operation_id}",
                    normalized_owner,
                    partial(self._finalize_object_cleanup, operation_id=operation_id),
                )
            except (FenceViolation, LeaseUnavailable, PlatformError):
                deferred += 1
            else:
                completed += 1
        for user_id in self._identity_access.list_due_deletion_workflows(limit=limit):
            try:
                self._worker_runtime.run_task(
                    f"identity-deletion:{user_id}",
                    normalized_owner,
                    partial(self._finalize, user_id=user_id),
                )
            except (FenceViolation, LeaseUnavailable, PlatformError):
                deferred += 1
            else:
                completed += 1
        return IdentityDeletionWorkerStats(completed=completed, deferred=deferred)

    def _finalize(
        self,
        context: TaskContext,
        connection: Connection,
        *,
        user_id: str,
    ) -> dict[str, object]:
        del context
        return self._identity_access.finalize_pending_deletion(
            user_id=user_id,
            connection=connection,
        )

    def _finalize_object_cleanup(
        self,
        context: TaskContext,
        connection: Connection,
        *,
        operation_id: str,
    ) -> dict[str, str]:
        del context
        return self._identity_access.finalize_object_cleanup(
            operation_id=operation_id,
            connection=connection,
        )


def run_identity_deletion_worker_once(
    settings: PlatformSettings,
    *,
    runtime: PlatformRuntime | None = None,
    owner: str | None = None,
    limit: int = 100,
) -> IdentityDeletionWorkerStats:
    worker_runtime = create_worker_runtime(settings, runtime=runtime)
    owns_runtime = runtime is None
    try:
        resolved_owner = owner or f"identity-deletion:{socket.gethostname()}:{os.getpid()}"
        return IdentityDeletionWorker(worker_runtime).run_once(owner=resolved_owner, limit=limit)
    finally:
        if owns_runtime:
            worker_runtime.close()


def main() -> None:
    stats = run_identity_deletion_worker_once(load_platform_settings())
    _logger.info(
        "identity deletion worker completed=%s deferred=%s",
        stats.completed,
        stats.deferred,
    )
