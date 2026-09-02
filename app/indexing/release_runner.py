"""Retrieval release acceptance runner: frozen-suite replay executor.

Consumes a staged ``retrieval_releases`` row, replays the frozen acceptance
suite scope, and judges externally produced metrics against the referenced
release gate version. The judgment itself is ``RetrievalReleaseService.release``
— this runner never re-implements gate semantics and never calls a real model;
it is the deployment-side entry that turns a metrics input file into a judged
run record.

Metrics input interface (one JSON object per run)::

    {
      "hardware_profile": {"accelerator": "a100", ...},   # must equal the frozen suite profile
      "metrics": {                                        # REQUIRED_LATENCY_METRICS + REQUIRED_QUALITY_METRICS
        "p50_ms": 1.0, "p95_ms": 2.0, "p99_ms": 3.0,
        "error_rate": 0.0, "vram_mb": 1.0,
        "hit_at_k": 0.9, "mrr": 0.9, "ndcg": 0.9, "refusal": 1.0
      }
    }

Console-script form (``ragqs-retrieval-acceptance``) follows the existing
protected CLI entry pattern: deployment automation can drive it without an
HTTP hop while tests import the functions directly. ``RAG_MAINTENANCE_KEY`` is
required because the runner mutates release state outside HTTP auth.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select

from app.platform.config import PlatformSettings, load_platform_settings
from app.platform.errors import PlatformError
from app.platform.runtime import PlatformRuntime, build_runtime

from .release_gates import REQUIRED_LATENCY_METRICS, REQUIRED_QUALITY_METRICS
from .releases import RetrievalReleaseService
from .schema import retrieval_releases_table

_logger = logging.getLogger(__name__)

REQUIRED_ACCEPTANCE_METRICS = REQUIRED_LATENCY_METRICS | REQUIRED_QUALITY_METRICS


def parse_acceptance_metrics(payload: Any) -> tuple[dict[str, float], dict[str, Any]]:
    """Validate the metrics input interface; return (metrics, hardware_profile)."""

    if not isinstance(payload, Mapping):
        raise PlatformError("validation_error", "acceptance metrics input is required", {}, 422)
    metrics = payload.get("metrics")
    hardware = payload.get("hardware_profile")
    if not isinstance(metrics, Mapping) or not metrics:
        raise PlatformError(
            "validation_error", "acceptance metrics input requires metrics", {}, 422
        )
    if not isinstance(hardware, Mapping) or not hardware:
        raise PlatformError(
            "validation_error", "acceptance metrics input requires a hardware profile", {}, 422
        )
    missing = sorted(name for name in REQUIRED_ACCEPTANCE_METRICS if name not in metrics)
    if missing:
        raise PlatformError(
            "validation_error",
            "acceptance metrics input is incomplete",
            {"missing_metrics": missing},
            422,
        )
    normalized: dict[str, float] = {}
    for name in REQUIRED_ACCEPTANCE_METRICS:
        value = metrics[name]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise PlatformError(
                "validation_error",
                "acceptance metric value is invalid",
                {"metric": name},
                422,
            )
        normalized[name] = float(value)
    return normalized, dict(hardware)


def _release_row(engine: Any, release_id: str) -> Mapping[str, Any]:
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(
                    retrieval_releases_table.c.id,
                    retrieval_releases_table.c.generation_id,
                    retrieval_releases_table.c.profile_id,
                    retrieval_releases_table.c.version,
                    retrieval_releases_table.c.state,
                    retrieval_releases_table.c.gate_version_id,
                    retrieval_releases_table.c.acceptance_suite_json,
                    retrieval_releases_table.c.gate_judgment_json,
                ).where(retrieval_releases_table.c.id == release_id)
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise PlatformError("not_found", "retrieval release was not found", {}, 404)
    return row


def _run_record(row: Mapping[str, Any], *, passed: bool) -> dict[str, Any]:
    frozen = dict(row["acceptance_suite_json"] or {})
    samples = dict(frozen.get("samples") or {})
    judgment = dict(row["gate_judgment_json"] or {})
    return {
        "release_id": str(row["id"]),
        "generation_id": str(row["generation_id"]),
        "profile_id": str(row["profile_id"]),
        "version": str(row["version"]),
        "state": str(row["state"]),
        "passed": passed,
        "gate_version_id": row["gate_version_id"],
        "sample_count": sum(len(entries) for entries in samples.values()),
        "sample_categories": sorted(samples),
        "hardware_profile": dict(frozen.get("hardware_profile") or {}),
        "judgment": judgment,
    }


def run_release_acceptance(
    settings: PlatformSettings,
    *,
    runtime: PlatformRuntime | None = None,
    release_id: str,
    metrics_input: Mapping[str, Any],
) -> dict[str, Any]:
    """Judge one staged release against its frozen gate; return the run record.

    Gate violations return ``passed=False`` with the release left staged (the
    same state the judgment path itself leaves); other errors propagate.
    """

    metrics, hardware_profile = parse_acceptance_metrics(metrics_input)
    owns_runtime = runtime is None
    active_runtime = runtime if runtime is not None else build_runtime(settings)
    try:
        engine = active_runtime.resolve("database_engine")
        releases = RetrievalReleaseService(engine)
        try:
            releases.release(
                release_id,
                metrics=metrics,
                hardware_profile=hardware_profile,
            )
        except PlatformError as error:
            if error.code == "release_gate_failed":
                record = _run_record(_release_row(engine, release_id), passed=False)
                record["failure"] = {
                    "code": error.code,
                    "message": error.message,
                    "details": error.details,
                }
                return record
            raise
        row = _release_row(engine, release_id)
        record = _run_record(row, passed=True)
        results = dict(dict(row["acceptance_suite_json"] or {}).get("results") or {})
        record["metrics"] = dict(results.get("metrics") or {})
        return record
    finally:
        if owns_runtime:
            active_runtime.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ragqs-retrieval-acceptance")
    parser.add_argument("--release-id", required=True)
    parser.add_argument(
        "--metrics-file",
        required=True,
        help="JSON file with hardware_profile and the required acceptance metrics",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    try:
        settings = load_platform_settings()
        if (
            settings.maintenance_key is None
            or not settings.maintenance_key.get_secret_value().strip()
        ):
            raise ValueError("RAG_MAINTENANCE_KEY is required")
    except ValueError:
        print(
            "ragqs-retrieval-acceptance: configuration or RAG_MAINTENANCE_KEY error",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    try:
        with open(args.metrics_file, encoding="utf-8") as handle:
            metrics_input = json.load(handle)
        record = run_release_acceptance(
            settings, release_id=args.release_id, metrics_input=metrics_input
        )
    except PlatformError as error:
        print(
            json.dumps({"error": error.code, "message": error.message, "details": error.details}),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except OSError as error:
        print(f"ragqs-retrieval-acceptance: metrics file error: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(record, ensure_ascii=False))
    if not record["passed"]:
        raise SystemExit(1)


__all__ = [
    "REQUIRED_ACCEPTANCE_METRICS",
    "parse_acceptance_metrics",
    "run_release_acceptance",
]
