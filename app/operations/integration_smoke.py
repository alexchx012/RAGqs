"""Integration smoke checks for real local/deployed RAGqs dependencies."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from typing import Any, TextIO

from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings, config
from app.operations.config_validation import validate_settings
from app.operations.health_preflight import HealthPreflightError, run_health_preflight
from app.providers.selection import ProviderSelection


class IntegrationSmokeError(RuntimeError):
    """Raised by dependency probes when a real integration is unavailable."""


class IntegrationSmokeIssue(BaseModel):
    """One actionable integration smoke issue."""

    field: str
    message: str
    severity: str = "error"


class IntegrationSmokeCheck(BaseModel):
    """Result for one smoke check boundary."""

    name: str
    status: str
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class IntegrationSmokeReport(BaseModel):
    """Machine-readable integration smoke report."""

    model_config = ConfigDict(populate_by_name=True)

    ready: bool
    checks: list[IntegrationSmokeCheck] = Field(default_factory=list)
    errors: list[IntegrationSmokeIssue] = Field(default_factory=list)
    warnings: list[IntegrationSmokeIssue] = Field(default_factory=list)


MilvusProbe = Callable[[Settings], dict[str, Any]]
ApiHealthProbe = Callable[[str, float], dict[str, Any]]


def run_integration_smoke(
    *,
    settings: Settings | None = None,
    milvus_probe: MilvusProbe | None = None,
    api_url: str = "",
    api_health_probe: ApiHealthProbe | None = None,
    timeout_seconds: float = 5.0,
) -> IntegrationSmokeReport:
    """Run non-destructive checks for config, Milvus, and optional API health."""

    settings = settings or config
    active_milvus_probe = milvus_probe or probe_milvus
    active_api_health_probe = api_health_probe or probe_api_health
    checks: list[IntegrationSmokeCheck] = []
    errors: list[IntegrationSmokeIssue] = []

    _append_configuration_check(settings, checks, errors)
    _append_milvus_check(settings, active_milvus_probe, checks, errors)

    if api_url.strip():
        _append_api_health_check(
            api_url.strip(),
            active_api_health_probe,
            checks,
            errors,
            timeout_seconds=timeout_seconds,
        )

    return IntegrationSmokeReport(ready=not errors, checks=checks, errors=errors)


def probe_milvus(settings: Settings) -> dict[str, Any]:
    """Open a read-only Milvus client and list collections."""

    from pymilvus import MilvusClient

    uri = f"http://{settings.milvus_host}:{settings.milvus_port}"
    timeout = settings.milvus_timeout / 1000
    client = MilvusClient(uri=uri, timeout=timeout)
    try:
        collections = client.list_collections(timeout=timeout)
        return {
            "host": settings.milvus_host,
            "port": settings.milvus_port,
            "collections": len(collections),
        }
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


def probe_api_health(url: str, timeout_seconds: float) -> dict[str, Any]:
    """Validate a running API `/health` endpoint."""

    summary = run_health_preflight(url, timeout_seconds=timeout_seconds)
    return {"status": summary.status, "dependencies": summary.dependencies}


def main(
    argv: list[str] | None = None,
    *,
    settings: Settings | None = None,
    milvus_probe: MilvusProbe | None = None,
    api_health_probe: ApiHealthProbe | None = None,
    output: TextIO | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Run RAGqs integration smoke checks.")
    parser.add_argument("--api-url", default="", help="Optional running API /health URL.")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args(argv)

    stream = output or sys.stdout
    report = run_integration_smoke(
        settings=settings or config,
        milvus_probe=milvus_probe,
        api_url=args.api_url,
        api_health_probe=api_health_probe,
        timeout_seconds=args.timeout,
    )

    if args.json:
        print(report.model_dump_json(indent=2), file=stream)
    else:
        _print_text_report(report, stream)
    return 0 if report.ready else 1


def _append_configuration_check(
    settings: Settings,
    checks: list[IntegrationSmokeCheck],
    errors: list[IntegrationSmokeIssue],
) -> None:
    report = validate_settings(settings)
    if report.is_valid:
        checks.append(IntegrationSmokeCheck(name="configuration", status="healthy", message="valid"))
        return

    checks.append(
        IntegrationSmokeCheck(
            name="configuration",
            status="unhealthy",
            message="invalid",
            details={"errors": [issue.to_dict() for issue in report.errors]},
        )
    )
    for issue in report.errors:
        errors.append(IntegrationSmokeIssue(field=issue.field, message=issue.message))


def _append_milvus_check(
    settings: Settings,
    milvus_probe: MilvusProbe,
    checks: list[IntegrationSmokeCheck],
    errors: list[IntegrationSmokeIssue],
) -> None:
    selection = ProviderSelection.from_settings(settings)
    if selection.vector_store_provider != "milvus":
        message = "must be milvus for integration smoke"
        checks.append(IntegrationSmokeCheck(name="milvus", status="unhealthy", message=message))
        errors.append(IntegrationSmokeIssue(field="VECTOR_STORE_PROVIDER", message=message))
        return

    try:
        details = milvus_probe(settings)
    except IntegrationSmokeError as exc:
        checks.append(IntegrationSmokeCheck(name="milvus", status="unhealthy", message=str(exc)))
        errors.append(IntegrationSmokeIssue(field="MILVUS", message=str(exc)))
    except Exception as exc:
        checks.append(IntegrationSmokeCheck(name="milvus", status="unhealthy", message=str(exc)))
        errors.append(IntegrationSmokeIssue(field="MILVUS", message=str(exc)))
    else:
        checks.append(
            IntegrationSmokeCheck(
                name="milvus",
                status="healthy",
                message="connected",
                details=dict(details),
            )
        )


def _append_api_health_check(
    api_url: str,
    api_health_probe: ApiHealthProbe,
    checks: list[IntegrationSmokeCheck],
    errors: list[IntegrationSmokeIssue],
    *,
    timeout_seconds: float,
) -> None:
    try:
        details = api_health_probe(api_url, timeout_seconds)
    except HealthPreflightError as exc:
        checks.append(IntegrationSmokeCheck(name="apiHealth", status="unhealthy", message=str(exc)))
        errors.append(IntegrationSmokeIssue(field="API_HEALTH", message=str(exc)))
    except Exception as exc:
        checks.append(IntegrationSmokeCheck(name="apiHealth", status="unhealthy", message=str(exc)))
        errors.append(IntegrationSmokeIssue(field="API_HEALTH", message=str(exc)))
    else:
        checks.append(
            IntegrationSmokeCheck(
                name="apiHealth",
                status="healthy",
                message="healthy",
                details=dict(details),
            )
        )


def _print_text_report(report: IntegrationSmokeReport, stream: TextIO) -> None:
    print(f"ready={str(report.ready).lower()}", file=stream)
    for check in report.checks:
        print(f"{check.name}={check.status} {check.message}".strip(), file=stream)
    for issue in report.errors:
        print(f"error {issue.field}: {issue.message}", file=stream)


if __name__ == "__main__":
    raise SystemExit(main())
