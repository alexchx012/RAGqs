"""Protected CLI trigger for the chat-generation worker and maintenance reaper.

The API process never runs a business scheduler: an external CronJob invokes this
entry point with the maintenance key, mirroring the usage maintenance pattern.
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
    args = parser.parse_args(argv)
    del args
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
    stats = run_chat_maintenance_once(settings)
    _logger.info(
        "chat maintenance executions=%s maintenance=%s",
        stats.executions_claimed,
        stats.maintenance,
    )


if __name__ == "__main__":
    main()
