"""Protected one-shot maintenance for document submission object cleanup."""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from typing import NoReturn

from app.platform.config import PlatformSettings, load_platform_settings
from app.platform.runtime import PlatformRuntime, build_runtime
from app.usage.maintenance import _require_maintenance_key

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DocumentsMaintenanceStats:
    cleaned: int = 0


def run_documents_maintenance_once(
    settings: PlatformSettings,
    *,
    runtime: PlatformRuntime | None = None,
    limit: int = 100,
) -> DocumentsMaintenanceStats:
    _require_maintenance_key(settings)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("documents maintenance limit must be a non-negative integer")
    if limit == 0:
        return DocumentsMaintenanceStats()
    owns_runtime = runtime is None
    active_runtime = runtime if runtime is not None else build_runtime(settings)
    try:
        service = active_runtime.resolve("documents_service")
        if service is None:
            raise RuntimeError("documents service is not configured")
        cleaned = service.cleanup_scheduled_submissions(limit=limit)
        return DocumentsMaintenanceStats(cleaned=len(cleaned))
    finally:
        if owns_runtime:
            active_runtime.close()


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> NoReturn:
        self.exit(2, f"{self.prog}: invalid arguments\n")


def main(argv: list[str] | None = None) -> None:
    parser = _SafeArgumentParser(prog="ragqs-documents-maintenance")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)
    try:
        settings = load_platform_settings()
    except ValueError:
        print("ragqs-documents-maintenance: configuration error", file=sys.stderr)
        raise SystemExit(2) from None
    try:
        _require_maintenance_key(settings)
    except ValueError:
        print(
            "ragqs-documents-maintenance: RAG_MAINTENANCE_KEY is required",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    stats = run_documents_maintenance_once(settings, limit=args.limit)
    _logger.info("documents maintenance cleaned=%s", stats.cleaned)


if __name__ == "__main__":
    main()


__all__ = [
    "DocumentsMaintenanceStats",
    "main",
    "run_documents_maintenance_once",
]
