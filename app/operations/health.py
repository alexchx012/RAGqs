"""Composable dependency health checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from app.config import config


@dataclass(frozen=True)
class HealthCheckResult:
    """Result for one dependency health check."""

    status: str
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def healthy(
        cls,
        message: str = "healthy",
        details: dict[str, Any] | None = None,
    ) -> "HealthCheckResult":
        return cls(status="healthy", message=message, details=details or {})

    @classmethod
    def unhealthy(
        cls,
        message: str = "unhealthy",
        details: dict[str, Any] | None = None,
    ) -> "HealthCheckResult":
        return cls(status="unhealthy", message=message, details=details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class DependencyHealthCheck:
    """Named dependency check used by readiness endpoints."""

    name: str
    check: Callable[[], HealthCheckResult | bool]
    required: bool = True

    def run(self) -> HealthCheckResult:
        try:
            result = self.check()
        except Exception as exc:
            return HealthCheckResult.unhealthy(str(exc))

        if isinstance(result, HealthCheckResult):
            return result
        if result is True:
            return HealthCheckResult.healthy()
        return HealthCheckResult.unhealthy()


class HealthChecker:
    """Aggregate dependency health checks into an HTTP-ready payload."""

    def __init__(
        self,
        checks: list[DependencyHealthCheck],
        *,
        metadata: dict[str, Any] | None = None,
    ):
        self.checks = checks
        self.metadata = metadata or {}

    def as_response(self) -> tuple[dict[str, Any], int]:
        dependency_results = {
            dependency.name: dependency.run()
            for dependency in self.checks
        }
        unhealthy_required = [
            dependency.name
            for dependency in self.checks
            if dependency.required
            and dependency_results[dependency.name].status != "healthy"
        ]
        status = "unhealthy" if unhealthy_required else "healthy"
        status_code = 503 if unhealthy_required else 200
        payload: dict[str, Any] = {
            **self.metadata,
            "status": status,
            "dependencies": {
                name: result.to_dict()
                for name, result in dependency_results.items()
            },
        }
        return payload, status_code


def create_default_health_checker() -> HealthChecker:
    """Create health checks for the default local RAG stack."""

    return HealthChecker(
        checks=[
            DependencyHealthCheck(
                name="app",
                check=lambda: HealthCheckResult.healthy(
                    "ready",
                    {"service": config.app_name, "version": config.app_version},
                ),
            ),
            DependencyHealthCheck(name="modelProvider", check=_model_provider_health),
            DependencyHealthCheck(name="embeddingProvider", check=_embedding_provider_health),
            DependencyHealthCheck(name="vectorStore", check=_vector_store_health),
            DependencyHealthCheck(name="sessionStore", check=_session_store_health),
        ],
        metadata={"service": config.app_name, "version": config.app_version},
    )


def _model_provider_health() -> HealthCheckResult:
    if _dashscope_configured():
        return HealthCheckResult.healthy("configured", {"model": config.rag_model})
    return HealthCheckResult.unhealthy("DASHSCOPE_API_KEY is not configured")


def _embedding_provider_health() -> HealthCheckResult:
    if _dashscope_configured():
        return HealthCheckResult.healthy(
            "configured",
            {"model": config.dashscope_embedding_model},
        )
    return HealthCheckResult.unhealthy("DASHSCOPE_API_KEY is not configured")


def _vector_store_health() -> HealthCheckResult:
    from app.core.milvus_client import milvus_manager

    healthy = milvus_manager.health_check()
    if healthy:
        return HealthCheckResult.healthy(
            "connected",
            {"host": config.milvus_host, "port": config.milvus_port},
        )
    return HealthCheckResult.unhealthy(
        "disconnected",
        {"host": config.milvus_host, "port": config.milvus_port},
    )


def _session_store_health() -> HealthCheckResult:
    from app.providers.factory import get_default_provider_container

    provider = get_default_provider_container().session_store_provider
    return HealthCheckResult.healthy(
        "available",
        {"provider": provider.__class__.__name__},
    )


def _dashscope_configured() -> bool:
    api_key = config.dashscope_api_key.strip()
    return bool(api_key and api_key != "your-api-key-here")
