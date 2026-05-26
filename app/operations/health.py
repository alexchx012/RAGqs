"""Composable dependency health checks."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

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
    ) -> HealthCheckResult:
        return cls(status="healthy", message=message, details=details or {})

    @classmethod
    def unhealthy(
        cls,
        message: str = "unhealthy",
        details: dict[str, Any] | None = None,
    ) -> HealthCheckResult:
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


def create_default_health_checker(
    *,
    settings: Any | None = None,
    milvus_manager: Any | None = None,
    session_store_provider: Any | None = None,
) -> HealthChecker:
    """Create health checks for the default local RAG stack."""

    active_settings = settings or config
    service_name = _settings_value(active_settings, "app", "name", "app_name", "RAGqs")
    service_version = _settings_value(active_settings, "app", "version", "app_version", "0.0.0")
    return HealthChecker(
        checks=[
            DependencyHealthCheck(
                name="app",
                check=lambda: HealthCheckResult.healthy(
                    "ready",
                    {"service": service_name, "version": service_version},
                ),
            ),
            DependencyHealthCheck(
                name="modelProvider",
                check=lambda: _model_provider_health(active_settings),
            ),
            DependencyHealthCheck(
                name="embeddingProvider",
                check=lambda: _embedding_provider_health(active_settings),
            ),
            DependencyHealthCheck(
                name="vectorStore",
                check=lambda: _vector_store_health(active_settings, milvus_manager),
            ),
            DependencyHealthCheck(
                name="sessionStore",
                check=lambda: _session_store_health(session_store_provider),
            ),
        ],
        metadata={"service": service_name, "version": service_version},
    )


def _model_provider_health(settings: Any = config) -> HealthCheckResult:
    if _dashscope_configured(settings):
        return HealthCheckResult.healthy(
            "configured",
            {"model": _settings_value(settings, "rag", "model", "rag_model", "qwen-max")},
        )
    return HealthCheckResult.unhealthy("DASHSCOPE_API_KEY is not configured")


def _embedding_provider_health(settings: Any = config) -> HealthCheckResult:
    if _dashscope_configured(settings):
        return HealthCheckResult.healthy(
            "configured",
            {
                "model": _settings_value(
                    settings,
                    "dashscope",
                    "embedding_model",
                    "dashscope_embedding_model",
                    "text-embedding-v4",
                )
            },
        )
    return HealthCheckResult.unhealthy("DASHSCOPE_API_KEY is not configured")


def _vector_store_health(
    settings: Any = config,
    milvus_manager: Any | None = None,
) -> HealthCheckResult:
    if milvus_manager is None:
        from app.core.milvus_client import milvus_manager as default_milvus_manager

        milvus_manager = default_milvus_manager

    healthy = milvus_manager.health_check()
    details = {
        "host": _settings_value(settings, "milvus", "host", "milvus_host", "localhost"),
        "port": _settings_value(settings, "milvus", "port", "milvus_port", 19530),
    }
    if healthy:
        return HealthCheckResult.healthy("connected", details)
    return HealthCheckResult.unhealthy("disconnected", details)


def _session_store_health(session_store_provider: Any | None = None) -> HealthCheckResult:
    if session_store_provider is None:
        from app.providers.factory import get_default_provider_container

        session_store_provider = get_default_provider_container().session_store_provider

    return HealthCheckResult.healthy(
        "available",
        {"provider": session_store_provider.__class__.__name__},
    )


def _dashscope_configured(settings: Any = config) -> bool:
    api_key = _settings_value(settings, "dashscope", "api_key", "dashscope_api_key", "").strip()
    return bool(api_key and api_key != "your-api-key-here")


def _settings_value(
    settings: Any,
    group_name: str,
    group_field_name: str,
    flat_field_name: str,
    default: Any,
) -> Any:
    group = getattr(settings, group_name, None)
    if group is not None and hasattr(group, group_field_name):
        return getattr(group, group_field_name)
    return getattr(settings, flat_field_name, default)
