"""Leased maintenance worker driving retention-owned orchestration steps."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.platform.config import PlatformSettings
from app.platform.context import TaskContext
from app.platform.errors import PlatformError
from app.platform.persistence import FenceViolation, LeaseUnavailable
from app.platform.worker import WorkerRuntime, create_worker_runtime

from .service import RetentionOpsService

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RetentionMaintenanceWorkerStats:
    completed: int
    deferred: int


class RetentionMaintenanceWorker:
    """Leased worker that drives retention orchestration targets.

    Every destructive effect happens inside the owner domains; this worker
    only calls owner entries under a platform lease.
    """

    def __init__(self, worker_runtime: WorkerRuntime) -> None:
        self._worker_runtime = worker_runtime
        retention = worker_runtime.runtime.resolve("retention_ops")
        if not isinstance(retention, RetentionOpsService):
            raise RuntimeError("retention ops service is not configured")
        self._retention = retention

    def run_once(self, *, owner: str, limit: int = 100) -> RetentionMaintenanceWorkerStats:
        normalized_owner = owner.strip()
        if not normalized_owner:
            raise ValueError("worker owner must not be empty")
        completed = 0
        deferred = 0
        for task_id, callback in self._tasks():
            try:
                self._worker_runtime.run_task(
                    task_id,
                    normalized_owner,
                    callback,
                )
            except (FenceViolation, LeaseUnavailable, PlatformError):
                deferred += 1
            except SQLAlchemyError:
                # Owner tables may not exist in every environment (e.g. a
                # minimal maintenance runtime); the target is simply deferred.
                _logger.warning("retention task %s deferred by database error", task_id)
                deferred += 1
            else:
                completed += 1
        return RetentionMaintenanceWorkerStats(completed=completed, deferred=deferred)

    def _tasks(self) -> list[tuple[str, Callable[[TaskContext, Any], Any]]]:
        return [
            (
                "retention:documents-version-purge",
                partial(self._purge_versions, limit=100),
            ),
            (
                "retention:document-finalize",
                partial(self._finalize_deletions, limit=100),
            ),
            (
                "retention:reconcile-document-deletions",
                partial(self._reconcile_documents, limit=100),
            ),
            (
                "retention:reconcile-index-generations",
                partial(self._reconcile_generations, limit=50),
            ),
            (
                "retention:gc-handoffs",
                partial(self._gc_handoffs, limit=50),
            ),
            (
                "retention:account-compaction",
                partial(self._compaction, limit=100),
            ),
            (
                "retention:identity-history-prune",
                partial(self._prune_identity_history, limit=100),
            ),
        ]

    def _purge_versions(
        self, _context: object, _connection: object, *, limit: int
    ) -> dict[str, object]:
        del _context, _connection
        return {"purged": self._retention.purge_due_versions(limit=limit)}

    def _finalize_deletions(
        self, _context: object, _connection: object, *, limit: int
    ) -> dict[str, object]:
        del _context, _connection
        return dict(self._retention.finalize_due_deletions(limit=limit))

    def _reconcile_documents(
        self, _context: object, _connection: object, *, limit: int
    ) -> dict[str, object]:
        del _context, _connection
        return dict(self._retention.run_document_reconciliation(limit=limit))

    def _reconcile_generations(
        self, _context: object, _connection: object, *, limit: int
    ) -> dict[str, object]:
        del _context, _connection
        return dict(self._retention.run_generation_reconciliation(limit=limit))

    def _gc_handoffs(
        self, _context: object, _connection: object, *, limit: int
    ) -> dict[str, object]:
        del _context, _connection
        return dict(self._retention.run_gc_handoffs(limit=limit))

    def _compaction(
        self, _context: object, _connection: object, *, limit: int
    ) -> dict[str, object]:
        del _context, _connection
        return dict(self._retention.run_compaction_requests(limit=limit))

    def _prune_identity_history(
        self, _context: object, _connection: object, *, limit: int
    ) -> dict[str, object]:
        del _context, _connection
        return dict(self._retention.prune_identity_history(limit=limit))

    def run_forever(
        self,
        *,
        owner: str,
        interval_seconds: int = 60,
        stop: Callable[[], bool] | None = None,
    ) -> None:
        while True:
            if stop is not None and stop():
                return
            try:
                self.run_once(owner=owner)
            except Exception:
                _logger.exception("retention maintenance loop iteration failed")
            time.sleep(interval_seconds)


def main() -> None:
    """Resident entry point: run the retention maintenance loop."""
    settings = load_platform_settings_for_main()
    worker_runtime = create_worker_runtime(settings)
    worker = RetentionMaintenanceWorker(worker_runtime)
    try:
        worker.run_forever(owner=f"retention-worker:{_hostname()}")
    finally:
        worker_runtime.close()


def load_platform_settings_for_main() -> PlatformSettings:
    from app.platform.config import load_platform_settings

    return load_platform_settings()


def _hostname() -> str:
    import socket

    return socket.gethostname() or "local"
