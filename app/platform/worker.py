from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import PlatformSettings
from .context import TaskContext, new_request_context, task_context
from .persistence import LeaseStore, MemoryLeaseStore
from .runtime import PlatformRuntime, build_runtime


@dataclass(slots=True)
class WorkerRuntime:
    runtime: PlatformRuntime
    leases: LeaseStore
    now: Callable[[], datetime]
    owns_runtime: bool = True

    def run_task(
        self,
        task_id: str,
        owner: str,
        callback: Callable[[TaskContext, Any], Any],
        *,
        ttl: timedelta | None = None,
    ) -> Any:
        ttl = ttl or timedelta(seconds=self.runtime.settings.worker.lease_seconds)
        lease = self.leases.acquire(task_id, owner, ttl)
        request = new_request_context()
        deadline = self.now().astimezone(UTC) + ttl
        context = task_context(
            request,
            task_id=task_id,
            lease_owner=owner,
            fence_token=lease.fence_token,
            deadline_utc=deadline,
        )
        with context:
            return self.leases.write_with_fence(
                lease,
                lambda connection: callback(context, connection),
            )

    def close(self) -> None:
        if self.owns_runtime:
            self.runtime.close()


def create_worker_runtime(
    settings: PlatformSettings,
    *,
    runtime: PlatformRuntime | None = None,
    leases: LeaseStore | None = None,
    now: Callable[[], datetime] | None = None,
) -> WorkerRuntime:
    owns_runtime = runtime is None
    runtime = runtime or build_runtime(settings)
    clock = runtime.resolve("database_clock")
    now = now or (clock.now_utc if clock is not None else lambda: datetime.now(UTC))
    leases = leases or runtime.resolve("lease_store") or MemoryLeaseStore(now)
    return WorkerRuntime(runtime, leases, now, owns_runtime)
