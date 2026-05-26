"""PostgreSQL integration smoke checks for configured durable stores."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TextIO
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from app.config import config


class PostgresSmokeError(RuntimeError):
    """Raised when a configured PostgreSQL integration is unavailable."""


class PostgresSmokeIssue(BaseModel):
    """One actionable PostgreSQL smoke issue."""

    field: str
    message: str
    severity: str = "error"


class PostgresSmokeCheck(BaseModel):
    """Result for one PostgreSQL-backed store boundary."""

    name: str
    status: str
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class PostgresSmokeReport(BaseModel):
    """Machine-readable PostgreSQL smoke report."""

    ready: bool
    checks: list[PostgresSmokeCheck] = Field(default_factory=list)
    errors: list[PostgresSmokeIssue] = Field(default_factory=list)


PostgresProbe = Callable[[str, float], dict[str, Any]]


@dataclass(frozen=True)
class PostgresStoreTarget:
    name: str
    provider_attr: str
    dsn_attr: str
    provider_env: str
    dsn_env: str


POSTGRES_STORE_TARGETS = (
    PostgresStoreTarget(
        name="sessionStore",
        provider_attr="session_store_provider",
        dsn_attr="session_store_postgres_dsn",
        provider_env="SESSION_STORE_PROVIDER",
        dsn_env="SESSION_STORE_POSTGRES_DSN",
    ),
    PostgresStoreTarget(
        name="retrievalAuditStore",
        provider_attr="retrieval_audit_store_provider",
        dsn_attr="retrieval_audit_postgres_dsn",
        provider_env="RETRIEVAL_AUDIT_STORE_PROVIDER",
        dsn_env="RETRIEVAL_AUDIT_POSTGRES_DSN",
    ),
    PostgresStoreTarget(
        name="indexingJobStore",
        provider_attr="indexing_job_store_provider",
        dsn_attr="indexing_job_store_postgres_dsn",
        provider_env="INDEXING_JOB_STORE_PROVIDER",
        dsn_env="INDEXING_JOB_STORE_POSTGRES_DSN",
    ),
    PostgresStoreTarget(
        name="documentCatalog",
        provider_attr="document_catalog_provider",
        dsn_attr="document_catalog_postgres_dsn",
        provider_env="DOCUMENT_CATALOG_PROVIDER",
        dsn_env="DOCUMENT_CATALOG_POSTGRES_DSN",
    ),
    PostgresStoreTarget(
        name="checkpointStore",
        provider_attr="checkpoint_provider",
        dsn_attr="checkpoint_postgres_dsn",
        provider_env="CHECKPOINT_PROVIDER",
        dsn_env="CHECKPOINT_POSTGRES_DSN",
    ),
)


def run_postgres_smoke(
    *,
    settings: Any | None = None,
    postgres_probe: PostgresProbe | None = None,
    timeout_seconds: float = 5.0,
    require_configured: bool = False,
) -> PostgresSmokeReport:
    """Run non-destructive PostgreSQL checks for configured Postgres-backed stores."""

    settings = settings or config
    active_probe = postgres_probe or probe_postgres
    checks: list[PostgresSmokeCheck] = []
    errors: list[PostgresSmokeIssue] = []

    configured_targets = [
        target
        for target in POSTGRES_STORE_TARGETS
        if _setting_id(getattr(settings, target.provider_attr, "sqlite")) == "postgres"
    ]

    if not configured_targets:
        message = "no postgres-backed stores configured"
        checks.append(PostgresSmokeCheck(name="postgres", status="skipped", message=message))
        if require_configured:
            errors.append(PostgresSmokeIssue(field="POSTGRES", message=message))
        return PostgresSmokeReport(ready=not errors, checks=checks, errors=errors)

    for target in configured_targets:
        _append_store_check(
            target,
            settings=settings,
            postgres_probe=active_probe,
            timeout_seconds=timeout_seconds,
            checks=checks,
            errors=errors,
        )

    return PostgresSmokeReport(ready=not errors, checks=checks, errors=errors)


def probe_postgres(dsn: str, timeout_seconds: float) -> dict[str, Any]:
    """Open a PostgreSQL connection and execute a read-only liveness query."""

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise PostgresSmokeError(
            "psycopg is not installed; install the postgres extras, "
            'for example: uv pip install -e ".[postgres]"'
        ) from exc

    connect_timeout = max(1, int(round(timeout_seconds)))
    try:
        with psycopg.connect(
            dsn,
            connect_timeout=connect_timeout,
            row_factory=dict_row,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 AS ok")
                row = cursor.fetchone()
    except Exception as exc:
        raise PostgresSmokeError(str(exc)) from exc

    return {"serverReachable": bool(row and row["ok"] == 1)}


def main(
    argv: list[str] | None = None,
    *,
    settings: Any | None = None,
    postgres_probe: PostgresProbe | None = None,
    output: TextIO | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Run RAGqs PostgreSQL smoke checks.")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument(
        "--require-configured",
        action="store_true",
        help="Fail when no Postgres-backed stores are selected.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args(argv)

    stream = output or sys.stdout
    report = run_postgres_smoke(
        settings=settings or config,
        postgres_probe=postgres_probe,
        timeout_seconds=args.timeout,
        require_configured=args.require_configured,
    )

    if args.json:
        print(report.model_dump_json(indent=2), file=stream)
    else:
        _print_text_report(report, stream)
    return 0 if report.ready else 1


def _append_store_check(
    target: PostgresStoreTarget,
    *,
    settings: Any,
    postgres_probe: PostgresProbe,
    timeout_seconds: float,
    checks: list[PostgresSmokeCheck],
    errors: list[PostgresSmokeIssue],
) -> None:
    dsn = str(getattr(settings, target.dsn_attr, "")).strip()
    if not dsn:
        message = f"must be set when {target.provider_env}=postgres"
        checks.append(PostgresSmokeCheck(name=target.name, status="unhealthy", message=message))
        errors.append(PostgresSmokeIssue(field=target.dsn_env, message=message))
        return

    try:
        details = postgres_probe(dsn, timeout_seconds)
    except PostgresSmokeError as exc:
        checks.append(PostgresSmokeCheck(name=target.name, status="unhealthy", message=str(exc)))
        errors.append(PostgresSmokeIssue(field=target.dsn_env, message=str(exc)))
    except Exception as exc:
        checks.append(PostgresSmokeCheck(name=target.name, status="unhealthy", message=str(exc)))
        errors.append(PostgresSmokeIssue(field=target.dsn_env, message=str(exc)))
    else:
        checks.append(
            PostgresSmokeCheck(
                name=target.name,
                status="healthy",
                message="connected",
                details={"dsn": _redact_dsn(dsn), **dict(details)},
            )
        )


def _redact_dsn(dsn: str) -> str:
    parsed = urlsplit(dsn)
    if parsed.scheme and parsed.netloc:
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        username = parsed.username or ""
        userinfo = f"{username}:***@" if parsed.password is not None else f"{username}@"
        return f"{parsed.scheme}://{userinfo}{host}{port}/..."

    return re.sub(r"(?i)(password\s*=\s*)\S+", r"\1***", dsn)


def _setting_id(value: Any) -> str:
    return str(value).strip().lower().replace("-", "_")


def _print_text_report(report: PostgresSmokeReport, stream: TextIO) -> None:
    print(f"ready={str(report.ready).lower()}", file=stream)
    for check in report.checks:
        print(f"{check.name}={check.status} {check.message}".strip(), file=stream)
    for issue in report.errors:
        print(f"error {issue.field}: {issue.message}", file=stream)


if __name__ == "__main__":
    raise SystemExit(main())
