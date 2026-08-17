from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import PlatformSettings
from .runtime import PlatformRuntime, build_runtime


@dataclass(slots=True)
class MaintenanceRuntime:
    runtime: PlatformRuntime
    owns_runtime: bool = True

    def run_retention_once(self) -> None:
        metrics: Any = self.runtime.resolve("observability_metrics")
        prune = getattr(metrics, "prune", None)
        if not callable(prune):
            raise RuntimeError("observability adapter does not support retention maintenance")
        prune()
        retention = self.runtime.resolve("notification_retention_maintenance")
        if retention is not None:
            retired = getattr(retention, "run_once", None)
            if not callable(retired):
                raise RuntimeError("notification retention maintenance does not support run_once")
            retired()
        dispatcher = self.runtime.resolve("outbox_dispatcher")
        compact = getattr(dispatcher, "compact_due_events", None)
        if callable(compact):
            compact()
        retention_worker = self.runtime.resolve("retention_worker")
        run_retention = getattr(retention_worker, "run_once", None) if retention_worker else None
        if callable(run_retention):
            run_retention(owner="maintenance-retention")
        backfill = getattr(
            self.runtime.resolve("identity_access"), "backfill_directory_search_text", None
        )
        if callable(backfill):
            backfill()

    def close(self) -> None:
        if self.owns_runtime:
            self.runtime.close()


def create_maintenance_runtime(
    settings: PlatformSettings,
    *,
    runtime: PlatformRuntime | None = None,
) -> MaintenanceRuntime:
    owns_runtime = runtime is None
    return MaintenanceRuntime(runtime=runtime or build_runtime(settings), owns_runtime=owns_runtime)
