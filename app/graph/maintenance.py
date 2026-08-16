"""Protected CLI trigger for the public graph build worker.

The API process never runs a business scheduler. An external CronJob invokes
this entry point with the maintenance key, matching the chat and evaluation
maintenance flows.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from typing import NoReturn

from app.platform.config import PlatformSettings, load_platform_settings
from app.platform.runtime import PlatformRuntime, build_runtime

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GraphMaintenanceStats:
    builds_processed: int = 0
    runs_requeued: int = 0
    runs_failed: int = 0


def _require_maintenance_key(settings: PlatformSettings) -> None:
    if settings.maintenance_key is None or not settings.maintenance_key.get_secret_value().strip():
        raise ValueError("RAG_MAINTENANCE_KEY is required")


def run_graph_maintenance_once(
    settings: PlatformSettings,
    *,
    runtime: PlatformRuntime | None = None,
    max_builds: int = 20,
) -> GraphMaintenanceStats:
    _require_maintenance_key(settings)
    owns_runtime = runtime is None
    active_runtime = runtime if runtime is not None else build_runtime(settings)
    try:
        worker = active_runtime.resolve("graph_build_worker")
        stats = GraphMaintenanceStats()
        for _ in range(max_builds):
            outcome = worker.run_once()
            stats = GraphMaintenanceStats(
                builds_processed=stats.builds_processed + outcome.builds_processed,
                runs_requeued=stats.runs_requeued + outcome.runs_requeued,
                runs_failed=stats.runs_failed + outcome.runs_failed,
            )
            if outcome.builds_processed == 0:
                break
        return stats
    finally:
        if owns_runtime:
            active_runtime.close()


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> NoReturn:
        self.exit(2, f"{self.prog}: invalid arguments\n")


def main(argv: list[str] | None = None) -> None:
    parser = _SafeArgumentParser(prog="ragqs-graph-maintenance")
    args = parser.parse_args(argv)
    del args
    try:
        settings = load_platform_settings()
    except ValueError:
        print("ragqs-graph-maintenance: configuration error", file=sys.stderr)
        raise SystemExit(2) from None
    try:
        _require_maintenance_key(settings)
    except ValueError:
        print("ragqs-graph-maintenance: RAG_MAINTENANCE_KEY is required", file=sys.stderr)
        raise SystemExit(2) from None
    stats = run_graph_maintenance_once(settings)
    _logger.info(
        "graph maintenance processed=%s requeued=%s failed=%s",
        stats.builds_processed,
        stats.runs_requeued,
        stats.runs_failed,
    )


if __name__ == "__main__":
    main()
