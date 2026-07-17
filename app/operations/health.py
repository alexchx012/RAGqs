"""Composable dependency health checks."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.config import config
from app.providers.selection import ProviderSelection

SQLITE_DEFAULT_PATHS = {
    "session_store_sqlite_path": "data/sessions.sqlite3",
    "retrieval_audit_sqlite_path": "data/retrieval-audits.sqlite3",
    "indexing_queue_sqlite_path": "data/indexing-queue.sqlite3",
    "indexing_job_store_sqlite_path": "data/indexing-jobs.sqlite3",
    "document_catalog_sqlite_path": "data/document-catalog.sqlite3",
    "checkpoint_sqlite_path": "data/checkpoints.sqlite3",
}


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
                check=lambda: _session_store_health(active_settings, session_store_provider),
            ),
            DependencyHealthCheck(
                name="checkpointStore",
                check=lambda: _store_provider_health(
                    active_settings,
                    provider_attr="checkpoint_provider",
                    sqlite_path_attr="checkpoint_sqlite_path",
                    postgres_dsn_attr="checkpoint_postgres_dsn",
                    provider_env="CHECKPOINT_PROVIDER",
                    sqlite_env="CHECKPOINT_SQLITE_PATH",
                    postgres_env="CHECKPOINT_POSTGRES_DSN",
                ),
            ),
            DependencyHealthCheck(
                name="retrievalAuditStore",
                check=lambda: _store_provider_health(
                    active_settings,
                    provider_attr="retrieval_audit_store_provider",
                    sqlite_path_attr="retrieval_audit_sqlite_path",
                    postgres_dsn_attr="retrieval_audit_postgres_dsn",
                    provider_env="RETRIEVAL_AUDIT_STORE_PROVIDER",
                    sqlite_env="RETRIEVAL_AUDIT_SQLITE_PATH",
                    postgres_env="RETRIEVAL_AUDIT_POSTGRES_DSN",
                ),
            ),
            DependencyHealthCheck(
                name="indexingQueue",
                check=lambda: _store_provider_health(
                    active_settings,
                    provider_attr="indexing_queue_provider",
                    sqlite_path_attr="indexing_queue_sqlite_path",
                    postgres_dsn_attr="indexing_queue_postgres_dsn",
                    provider_env="INDEXING_QUEUE_PROVIDER",
                    sqlite_env="INDEXING_QUEUE_SQLITE_PATH",
                    postgres_env="INDEXING_QUEUE_POSTGRES_DSN",
                ),
            ),
            DependencyHealthCheck(
                name="indexingJobStore",
                check=lambda: _store_provider_health(
                    active_settings,
                    provider_attr="indexing_job_store_provider",
                    sqlite_path_attr="indexing_job_store_sqlite_path",
                    postgres_dsn_attr="indexing_job_store_postgres_dsn",
                    provider_env="INDEXING_JOB_STORE_PROVIDER",
                    sqlite_env="INDEXING_JOB_STORE_SQLITE_PATH",
                    postgres_env="INDEXING_JOB_STORE_POSTGRES_DSN",
                ),
            ),
            DependencyHealthCheck(
                name="documentCatalog",
                check=lambda: _store_provider_health(
                    active_settings,
                    provider_attr="document_catalog_provider",
                    sqlite_path_attr="document_catalog_sqlite_path",
                    postgres_dsn_attr="document_catalog_postgres_dsn",
                    provider_env="DOCUMENT_CATALOG_PROVIDER",
                    sqlite_env="DOCUMENT_CATALOG_SQLITE_PATH",
                    postgres_env="DOCUMENT_CATALOG_POSTGRES_DSN",
                ),
            ),
        ],
        metadata={"service": service_name, "version": service_version},
    )


def _model_provider_health(settings: Any = config) -> HealthCheckResult:
    selection = ProviderSelection.from_settings(settings)
    provider = selection.chat_provider
    model = str(getattr(settings, "chat_model", "") or "").strip()
    if provider == "fake":
        return HealthCheckResult.healthy(
            "fake provider selected for local software-path checks",
            {"provider": "fake", "validation": "not_real_answer_quality"},
        )
    issue = _chat_provider_config_issue(provider, settings, model)
    if issue:
        return HealthCheckResult.unhealthy(issue, {"provider": provider})
    return HealthCheckResult.healthy(
        "configured; real provider call not verified",
        {
            "provider": provider,
            "model": model,
            "validation": "configured_not_smoke_tested",
        },
    )


def _chat_provider_config_issue(provider: str, settings: Any, model: str) -> str | None:
    """Return a config issue message for chat provider settings, or None when configured."""

    if provider == "deepseek":
        api_key = str(
            _settings_value(settings, "deepseek", "api_key", "deepseek_api_key", "")
        ).strip()
        if _is_placeholder_secret(api_key):
            return "DEEPSEEK_API_KEY is not configured"
        if not model.strip():
            return "CHAT_MODEL is not configured"
        return None

    if provider == "dashscope":
        if not _dashscope_configured(settings):
            return "DASHSCOPE_API_KEY is not configured"
        if not model.strip():
            return "CHAT_MODEL is not configured"
        return None

    if provider == "openai_compatible":
        api_key = str(
            _settings_value(
                settings,
                "openai_compatible",
                "api_key",
                "openai_compatible_api_key",
                "",
            )
        ).strip()
        base_url = str(
            _settings_value(
                settings,
                "openai_compatible",
                "base_url",
                "openai_compatible_base_url",
                "",
            )
        ).strip()
        if not api_key or not model.strip():
            return "OPENAI_COMPATIBLE_API_KEY and CHAT_MODEL must be configured"
        if not base_url:
            return "OPENAI_COMPATIBLE_BASE_URL must be configured"
        return None

    return f"unsupported chat provider: {provider}"


def _embedding_provider_health(settings: Any = config) -> HealthCheckResult:
    selection = ProviderSelection.from_settings(settings)
    provider = selection.embedding_provider
    if provider == "fake":
        return HealthCheckResult.healthy(
            "fake provider selected for local software-path checks",
            {"provider": "fake", "validation": "not_real_embedding_quality"},
        )
    if provider == "openai_compatible":
        api_key = str(
            _settings_value(
                settings,
                "openai_compatible",
                "api_key",
                "openai_compatible_api_key",
                "",
            )
        ).strip()
        model = str(
            _settings_value(
                settings,
                "openai_compatible",
                "embedding_model",
                "openai_compatible_embedding_model",
                "",
            )
        ).strip()
        if api_key and model:
            return HealthCheckResult.healthy(
                "configured; real provider call not verified",
                {"provider": "openai_compatible", "model": model},
            )
        return HealthCheckResult.unhealthy(
            "OPENAI_COMPATIBLE_API_KEY and OPENAI_COMPATIBLE_EMBEDDING_MODEL must be configured",
            {"provider": "openai_compatible"},
        )
    if provider == "dashscope":
        if not _dashscope_configured(settings):
            return HealthCheckResult.unhealthy(
                "DASHSCOPE_API_KEY is not configured",
                {"provider": "dashscope"},
            )
        model = str(
            _settings_value(
                settings,
                "dashscope",
                "embedding_model",
                "dashscope_embedding_model",
                "text-embedding-v4",
            )
        ).strip()
        if not model:
            return HealthCheckResult.unhealthy(
                "DASHSCOPE_EMBEDDING_MODEL is not configured",
                {"provider": "dashscope"},
            )
        return HealthCheckResult.healthy(
            "configured; real provider call not verified",
            {"provider": "dashscope", "model": model},
        )
    return HealthCheckResult.unhealthy(
        f"unsupported embedding provider: {provider}",
        {"provider": provider},
    )


def _vector_store_health(
    settings: Any = config,
    milvus_manager: Any | None = None,
) -> HealthCheckResult:
    selection = ProviderSelection.from_settings(settings)
    if selection.vector_store_provider == "fake":
        return HealthCheckResult.healthy(
            "fake vector store selected for local software-path checks",
            {"provider": "fake", "validation": "not_real_retrieval_quality"},
        )

    if milvus_manager is None:
        from app.core.milvus_client import milvus_manager as default_milvus_manager

        milvus_manager = default_milvus_manager

    healthy = milvus_manager.health_check()
    details = {
        "provider": "milvus",
        "host": _settings_value(settings, "milvus", "host", "milvus_host", "localhost"),
        "port": _settings_value(settings, "milvus", "port", "milvus_port", 19530),
    }
    if healthy:
        return HealthCheckResult.healthy("connected", details)
    return HealthCheckResult.unhealthy("disconnected", details)


def _session_store_health(
    settings: Any = config,
    session_store_provider: Any | None = None,
) -> HealthCheckResult:
    if session_store_provider is None:
        return _store_provider_health(
            settings,
            provider_attr="session_store_provider",
            sqlite_path_attr="session_store_sqlite_path",
            postgres_dsn_attr="session_store_postgres_dsn",
            provider_env="SESSION_STORE_PROVIDER",
            sqlite_env="SESSION_STORE_SQLITE_PATH",
            postgres_env="SESSION_STORE_POSTGRES_DSN",
        )

    return HealthCheckResult.healthy(
        "available",
        {"provider": session_store_provider.__class__.__name__},
    )


def _store_provider_health(
    settings: Any,
    *,
    provider_attr: str,
    sqlite_path_attr: str,
    postgres_dsn_attr: str,
    provider_env: str,
    sqlite_env: str,
    postgres_env: str,
) -> HealthCheckResult:
    provider = _setting_id(_settings_provider_value(settings, provider_attr, "sqlite"))
    if provider == "memory":
        return HealthCheckResult.healthy(
            "process memory selected",
            {"provider": provider, "durable": False},
        )
    if provider == "sqlite":
        path = str(
            _settings_storage_value(
                settings,
                sqlite_path_attr,
                SQLITE_DEFAULT_PATHS.get(sqlite_path_attr, ""),
            )
        ).strip()
        if path:
            return HealthCheckResult.healthy(
                "configured",
                {"provider": provider, "path": path, "durable": True},
            )
        return HealthCheckResult.unhealthy(f"{sqlite_env} must be configured")
    if provider == "postgres":
        dsn = str(_settings_storage_value(settings, postgres_dsn_attr, "")).strip()
        if dsn:
            return HealthCheckResult.healthy(
                "configured; run postgres smoke for reachability",
                {
                    "provider": provider,
                    "dsnConfigured": True,
                    "connectivity": "not_checked",
                },
            )
        return HealthCheckResult.unhealthy(f"{postgres_env} must be configured")
    return HealthCheckResult.unhealthy(f"{provider_env} unsupported provider: {provider}")


def _dashscope_configured(settings: Any = config) -> bool:
    api_key = _settings_value(settings, "dashscope", "api_key", "dashscope_api_key", "").strip()
    return not _is_placeholder_secret(api_key)


def _is_placeholder_secret(value: str) -> bool:
    normalized = value.strip().strip('"').strip("'").lower()
    if not normalized:
        return True
    if normalized.startswith("your-") and "key" in normalized:
        return True
    return any(
        marker in normalized
        for marker in ["your-api-key", "placeholder", "changeme"]
    )


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


def _settings_storage_value(settings: Any, flat_field_name: str, default: Any) -> Any:
    group = getattr(settings, "storage", None)
    if group is not None and hasattr(group, flat_field_name):
        return getattr(group, flat_field_name)
    return getattr(settings, flat_field_name, default)


def _settings_provider_value(settings: Any, flat_field_name: str, default: Any) -> Any:
    selection = ProviderSelection.from_settings(settings)
    if hasattr(selection, flat_field_name):
        return getattr(selection, flat_field_name)
    return _settings_storage_value(settings, flat_field_name, default)


def _setting_id(value: Any) -> str:
    return str(value).strip().lower().replace("-", "_")
