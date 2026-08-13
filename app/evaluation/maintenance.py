"""Protected CLI trigger for the shadow-evaluation and calibration-close workers.

The API process never runs a business scheduler: an external CronJob invokes
this entry point with the maintenance key, mirroring the usage/chat
maintenance patterns. One invocation claims at most ``max_runs`` shadow runs
and then runs one calibration-close pass.
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


@dataclass(slots=True)
class EvaluationMaintenanceStats:
    runs_processed: int = 0
    runs_requeued: int = 0
    runs_failed: int = 0
    windows_closed: int = 0


def _require_maintenance_key(settings: PlatformSettings) -> None:
    if settings.maintenance_key is None or not settings.maintenance_key.get_secret_value().strip():
        raise ValueError("RAG_MAINTENANCE_KEY is required")


def run_evaluation_maintenance_once(
    settings: PlatformSettings,
    *,
    runtime: PlatformRuntime | None = None,
    max_runs: int = 20,
) -> EvaluationMaintenanceStats:
    _require_maintenance_key(settings)
    owns_runtime = runtime is None
    active_runtime = runtime if runtime is not None else build_runtime(settings)
    try:
        shadow_worker = active_runtime.resolve("evaluation_worker")
        close_worker = active_runtime.resolve("calibration_close_worker")
        stats = EvaluationMaintenanceStats()
        for _ in range(max_runs):
            outcome = shadow_worker.run_once()
            stats.runs_processed += outcome.runs_processed
            stats.runs_requeued += outcome.runs_requeued
            stats.runs_failed += outcome.runs_failed
            if outcome.runs_processed == 0:
                break
        if close_worker is not None:
            stats.windows_closed = close_worker.run_once()
        return stats
    finally:
        if owns_runtime:
            active_runtime.close()


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> NoReturn:
        self.exit(2, f"{self.prog}: invalid arguments\n")


def main(argv: list[str] | None = None) -> None:
    parser = _SafeArgumentParser(prog="ragqs-evaluation-maintenance")
    args = parser.parse_args(argv)
    del args
    try:
        settings = load_platform_settings()
    except ValueError:
        print("ragqs-evaluation-maintenance: configuration error", file=sys.stderr)
        raise SystemExit(2) from None
    try:
        _require_maintenance_key(settings)
    except ValueError:
        print("ragqs-evaluation-maintenance: RAG_MAINTENANCE_KEY is required", file=sys.stderr)
        raise SystemExit(2) from None
    stats = run_evaluation_maintenance_once(settings)
    _logger.info(
        "evaluation maintenance processed=%s requeued=%s failed=%s windows_closed=%s",
        stats.runs_processed,
        stats.runs_requeued,
        stats.runs_failed,
        stats.windows_closed,
    )


if __name__ == "__main__":
    main()
