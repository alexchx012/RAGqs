from __future__ import annotations

import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import PlatformSettings
from .context import TaskContext, new_request_context, task_context
from .persistence import LeaseStore, MemoryLeaseStore
from .runtime import PlatformRuntime, build_runtime


def install_stop_signal_handlers() -> tuple[threading.Event, dict[int, Any]]:
    """Install SIGINT/SIGTERM handlers that cooperatively stop resident workers.

    Resident entry points feed ``stop_event.is_set`` into their
    ``run_forever`` loop so a termination signal finishes the current
    iteration instead of hard-killing the process; the returned mapping
    carries the previous handlers for ``restore_signal_handlers``.
    """

    stop_event = threading.Event()
    previous: dict[int, Any] = {}

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    for name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is not None:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)
    return stop_event, previous


def restore_signal_handlers(previous: dict[int, Any]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


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
