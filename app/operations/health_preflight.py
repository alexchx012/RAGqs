"""HTTP health preflight for a running RAGqs API."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TextIO

REQUIRED_DEPENDENCIES = [
    "app",
    "modelProvider",
    "embeddingProvider",
    "vectorStore",
    "sessionStore",
    "checkpointStore",
    "retrievalAuditStore",
    "indexingQueue",
    "indexingJobStore",
    "documentCatalog",
]


class HealthPreflightError(RuntimeError):
    """Raised when the running API health response is not deployable."""


@dataclass(frozen=True)
class HealthPreflightSummary:
    status: str
    dependencies: list[str]


UrlOpen = Callable[..., Any]


def validate_health_payload(
    payload: dict[str, Any],
    *,
    http_status: int,
    required_dependencies: list[str] | None = None,
) -> HealthPreflightSummary:
    """Validate the FastAPI `/health` response envelope."""

    required = required_dependencies or REQUIRED_DEPENDENCIES
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise HealthPreflightError("health response is missing data object")

    dependencies = data.get("dependencies")
    if not isinstance(dependencies, dict):
        raise HealthPreflightError("health response is missing dependencies object")

    problems: list[str] = []
    for name in required:
        dependency = dependencies.get(name)
        if not isinstance(dependency, dict):
            problems.append(f"{name}: missing")
            continue

        status = str(dependency.get("status", "")).strip()
        if status != "healthy":
            message = str(dependency.get("message") or status or "unhealthy").strip()
            problems.append(f"{name}: {message}")

    overall_status = str(data.get("status", "")).strip()
    envelope_code = int(payload.get("code", http_status) or http_status)
    if not problems and (overall_status != "healthy" or http_status >= 500 or envelope_code >= 500):
        problems.append(f"overall status: {overall_status or http_status}")

    if problems:
        raise HealthPreflightError("; ".join(problems))

    return HealthPreflightSummary(status="healthy", dependencies=list(required))


def fetch_health_payload(
    url: str,
    *,
    timeout_seconds: float = 5.0,
    opener: UrlOpen | None = None,
) -> tuple[dict[str, Any], int]:
    """Fetch and decode a health endpoint response, including HTTP error bodies."""

    active_opener = opener or urllib.request.urlopen
    request = urllib.request.Request(url, headers={"Accept": "application/json"})

    try:
        response = active_opener(request, timeout=timeout_seconds)
        http_status = int(getattr(response, "status", getattr(response, "code", 200)))
        body = response.read()
    except urllib.error.HTTPError as exc:
        http_status = int(exc.code)
        body = exc.read()
    except urllib.error.URLError as exc:
        raise HealthPreflightError(f"could not reach health endpoint: {exc.reason}") from exc

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HealthPreflightError("health endpoint did not return valid UTF-8 JSON") from exc

    if not isinstance(payload, dict):
        raise HealthPreflightError("health endpoint returned a non-object JSON payload")

    return payload, http_status


def run_health_preflight(url: str, *, timeout_seconds: float = 5.0) -> HealthPreflightSummary:
    payload, http_status = fetch_health_payload(url, timeout_seconds=timeout_seconds)
    return validate_health_payload(payload, http_status=http_status)


def main(argv: list[str] | None = None, *, output: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a running RAGqs /health endpoint.")
    parser.add_argument("--url", default="http://127.0.0.1:9900/health")
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args(argv)

    stream = output or sys.stdout
    try:
        summary = run_health_preflight(args.url, timeout_seconds=args.timeout)
    except HealthPreflightError as exc:
        print(f"[fail] API health preflight failed: {exc}", file=stream)
        return 1

    print(
        "[ ok ] API health preflight passed: "
        f"{summary.status} ({', '.join(summary.dependencies)})",
        file=stream,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
