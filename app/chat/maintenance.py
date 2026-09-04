"""Protected CLI trigger for the chat-generation worker and maintenance reaper.

The API process never runs a business scheduler: an external CronJob invokes this
entry point with the maintenance key, mirroring the usage maintenance pattern.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from typing import NoReturn

from app.platform.config import PlatformSettings, load_platform_settings
from app.platform.errors import PlatformError
from app.platform.runtime import PlatformRuntime, build_runtime

_logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ChatMaintenanceStats:
    maintenance: dict[str, int]
    executions_claimed: int


def _require_maintenance_key(settings: PlatformSettings) -> None:
    if settings.maintenance_key is None or not settings.maintenance_key.get_secret_value().strip():
        raise ValueError("RAG_MAINTENANCE_KEY is required")


def run_chat_maintenance_once(
    settings: PlatformSettings,
    *,
    runtime: PlatformRuntime | None = None,
    max_executions: int = 50,
) -> ChatMaintenanceStats:
    _require_maintenance_key(settings)
    owns_runtime = runtime is None
    active_runtime = runtime if runtime is not None else build_runtime(settings)
    try:
        worker = active_runtime.resolve("chat_generation_worker")
        maintenance = worker.run_maintenance()
        claimed = 0
        for _ in range(max_executions):
            outcome = worker.run_once()
            if outcome.executed is None:
                break
            claimed += 1
        return ChatMaintenanceStats(maintenance=maintenance, executions_claimed=claimed)
    finally:
        if owns_runtime:
            active_runtime.close()


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> NoReturn:
        self.exit(2, f"{self.prog}: invalid arguments\n")


def main(argv: list[str] | None = None) -> None:
    parser = _SafeArgumentParser(prog="ragqs-chat-maintenance")
    parser.add_argument(
        "--loop",
        action="store_true",
        help="run maintenance passes until interrupted (resident generation driver)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="seconds between passes in --loop mode (default: 2)",
    )
    args = parser.parse_args(argv)
    try:
        settings = load_platform_settings()
    except ValueError:
        print("ragqs-chat-maintenance: configuration error", file=sys.stderr)
        raise SystemExit(2) from None
    try:
        _require_maintenance_key(settings)
    except ValueError:
        print("ragqs-chat-maintenance: RAG_MAINTENANCE_KEY is required", file=sys.stderr)
        raise SystemExit(2) from None
    if args.interval < 0:
        print("ragqs-chat-maintenance: --interval must be non-negative", file=sys.stderr)
        raise SystemExit(2) from None
    if not args.loop:
        stats = run_chat_maintenance_once(settings)
        _logger.info(
            "chat maintenance executions=%s maintenance=%s",
            stats.executions_claimed,
            stats.maintenance,
        )
        return
    runtime = build_runtime(settings)
    try:
        while True:
            try:
                stats = run_chat_maintenance_once(settings, runtime=runtime)
            except PlatformError as error:
                # Transient infrastructure failures must not kill the resident
                # driver; the next pass retries claimed/retry_wait executions.
                _logger.warning("chat maintenance pass failed: %s", error.code)
            else:
                if stats.executions_claimed:
                    _logger.info(
                        "chat maintenance executions=%s maintenance=%s",
                        stats.executions_claimed,
                        stats.maintenance,
                    )
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
