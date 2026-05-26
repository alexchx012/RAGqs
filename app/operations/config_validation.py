"""Startup configuration validation for local and deployed runtimes."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any, TextIO

from app.config import Settings, config
from app.extensions.tools import build_default_tool_registry, parse_enabled_tool_names
from app.prompts.profiles import build_default_prompt_registry
from app.providers.selection import ProviderSelection, validate_provider_selection
from app.retrieval import build_default_retrieval_profile_registry, parse_filter_key_list
from app.security.cors import parse_cors_origins
from app.security.uploads import parse_allowed_extensions


@dataclass(frozen=True)
class ConfigIssue:
    """One actionable configuration validation issue."""

    field: str
    message: str
    severity: str = "error"

    def to_dict(self) -> dict[str, str]:
        return {
            "field": self.field,
            "message": self.message,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class ConfigValidationReport:
    """Aggregate configuration validation result."""

    errors: list[ConfigIssue] = field(default_factory=list)
    warnings: list[ConfigIssue] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.is_valid,
            "errors": [issue.to_dict() for issue in self.errors],
            "warnings": [issue.to_dict() for issue in self.warnings],
        }


def validate_settings(settings: Settings) -> ConfigValidationReport:
    """Validate settings that must be correct before starting the RAG service."""

    errors: list[ConfigIssue] = []
    selection = ProviderSelection.from_settings(settings)
    app_config = getattr(settings, "app", settings)
    agent_config = getattr(settings, "agent", settings)
    chunking_config = getattr(settings, "chunking", settings)
    cors_config = getattr(settings, "cors", settings)
    dashscope_config = getattr(settings, "dashscope", settings)
    milvus_config = getattr(settings, "milvus", settings)
    openai_config = getattr(settings, "openai_compatible", settings)
    rag_config = getattr(settings, "rag", settings)
    storage_config = getattr(settings, "storage", settings)
    upload_config = getattr(settings, "upload", settings)

    for field_name, message in validate_provider_selection(settings):
        errors.append(ConfigIssue(field=field_name, message=message))

    uses_dashscope = (
        selection.chat_provider == "dashscope" or selection.embedding_provider == "dashscope"
    )
    if uses_dashscope and _is_placeholder_secret(
        _group_value(dashscope_config, settings, "api_key", "dashscope_api_key")
    ):
        errors.append(
            ConfigIssue(
                field="DASHSCOPE_API_KEY",
                message="must be set to a non-placeholder value",
            )
        )

    uses_openai_compatible = (
        selection.chat_provider == "openai_compatible"
        or selection.embedding_provider == "openai_compatible"
    )
    openai_api_key = _group_value(
        openai_config, settings, "api_key", "openai_compatible_api_key"
    )
    openai_model = _group_value(
        openai_config, settings, "model", "openai_compatible_model"
    )
    openai_embedding_model = _group_value(
        openai_config,
        settings,
        "embedding_model",
        "openai_compatible_embedding_model",
    )

    if uses_openai_compatible and _is_placeholder_secret(openai_api_key):
        errors.append(
            ConfigIssue(
                field="OPENAI_COMPATIBLE_API_KEY",
                message="must be set when an OpenAI-compatible provider is selected",
            )
        )
    if selection.chat_provider == "openai_compatible" and not openai_model.strip():
        errors.append(
            ConfigIssue(
                field="OPENAI_COMPATIBLE_MODEL",
                message="must be set when an OpenAI-compatible provider is selected",
            )
        )
    if (
        selection.embedding_provider == "openai_compatible"
        and not openai_embedding_model.strip()
    ):
        errors.append(
            ConfigIssue(
                field="OPENAI_COMPATIBLE_EMBEDDING_MODEL",
                message="must be set when an OpenAI-compatible provider is selected",
            )
        )

    if selection.session_store_provider == "sqlite":
        sqlite_path = _group_value(
            storage_config, settings, "session_store_sqlite_path", "session_store_sqlite_path"
        )
        if not sqlite_path.strip():
            errors.append(
                ConfigIssue(
                    field="SESSION_STORE_SQLITE_PATH",
                    message="must be set when SESSION_STORE_PROVIDER=sqlite",
                )
            )

    if selection.session_store_provider == "postgres":
        postgres_dsn = _group_value(
            storage_config,
            settings,
            "session_store_postgres_dsn",
            "session_store_postgres_dsn",
        )
        if not postgres_dsn.strip():
            errors.append(
                ConfigIssue(
                    field="SESSION_STORE_POSTGRES_DSN",
                    message="must be set when SESSION_STORE_PROVIDER=postgres",
                )
            )

    retrieval_audit_store_provider = _normalize_config_id(
        getattr(selection, "retrieval_audit_store_provider", "memory")
    )
    if retrieval_audit_store_provider == "sqlite":
        sqlite_path = _group_value(
            storage_config,
            settings,
            "retrieval_audit_sqlite_path",
            "retrieval_audit_sqlite_path",
        )
        if not sqlite_path.strip():
            errors.append(
                ConfigIssue(
                    field="RETRIEVAL_AUDIT_SQLITE_PATH",
                    message="must be set when RETRIEVAL_AUDIT_STORE_PROVIDER=sqlite",
                )
            )
    if retrieval_audit_store_provider == "postgres":
        postgres_dsn = _group_value(
            storage_config,
            settings,
            "retrieval_audit_postgres_dsn",
            "retrieval_audit_postgres_dsn",
        )
        if not postgres_dsn.strip():
            errors.append(
                ConfigIssue(
                    field="RETRIEVAL_AUDIT_POSTGRES_DSN",
                    message="must be set when RETRIEVAL_AUDIT_STORE_PROVIDER=postgres",
                )
            )

    if selection.checkpoint_provider == "sqlite":
        sqlite_path = _group_value(
            storage_config, settings, "checkpoint_sqlite_path", "checkpoint_sqlite_path"
        )
        if not sqlite_path.strip():
            errors.append(
                ConfigIssue(
                    field="CHECKPOINT_SQLITE_PATH",
                    message="must be set when CHECKPOINT_PROVIDER=sqlite",
                )
            )
    if selection.checkpoint_provider == "postgres":
        postgres_dsn = _group_value(
            storage_config,
            settings,
            "checkpoint_postgres_dsn",
            "checkpoint_postgres_dsn",
        )
        if not postgres_dsn.strip():
            errors.append(
                ConfigIssue(
                    field="CHECKPOINT_POSTGRES_DSN",
                    message="must be set when CHECKPOINT_PROVIDER=postgres",
                )
            )

    indexing_execution_mode = _normalize_config_id(
        _group_value(
            storage_config,
            settings,
            "indexing_execution_mode",
            "indexing_execution_mode",
            default="sync",
        )
    )
    if indexing_execution_mode not in {"sync", "background"}:
        errors.append(
            ConfigIssue(
                field="INDEXING_EXECUTION_MODE",
                message=f"unsupported mode: {indexing_execution_mode}",
            )
        )

    if (
        _group_value(
            storage_config,
            settings,
            "indexing_worker_poll_interval_seconds",
            "indexing_worker_poll_interval_seconds",
            default=0.25,
        )
        <= 0
    ):
        errors.append(
            ConfigIssue(
                field="INDEXING_WORKER_POLL_INTERVAL_SECONDS",
                message="must be greater than 0",
            )
        )

    if (
        _group_value(
            storage_config,
            settings,
            "indexing_worker_shutdown_timeout_seconds",
            "indexing_worker_shutdown_timeout_seconds",
            default=5.0,
        )
        <= 0
    ):
        errors.append(
            ConfigIssue(
                field="INDEXING_WORKER_SHUTDOWN_TIMEOUT_SECONDS",
                message="must be greater than 0",
            )
        )

    indexing_job_store_provider = _normalize_config_id(
        _group_value(
            storage_config,
            settings,
            "indexing_job_store_provider",
            "indexing_job_store_provider",
            default="memory",
        )
    )
    if indexing_job_store_provider not in {"memory", "sqlite", "postgres"}:
        errors.append(
            ConfigIssue(
                field="INDEXING_JOB_STORE_PROVIDER",
                message=f"unsupported provider: {indexing_job_store_provider}",
            )
        )
    if indexing_job_store_provider == "sqlite":
        sqlite_path = _group_value(
            storage_config,
            settings,
            "indexing_job_store_sqlite_path",
            "indexing_job_store_sqlite_path",
        )
        if not sqlite_path.strip():
            errors.append(
                ConfigIssue(
                    field="INDEXING_JOB_STORE_SQLITE_PATH",
                    message="must be set when INDEXING_JOB_STORE_PROVIDER=sqlite",
                )
            )
    if indexing_job_store_provider == "postgres":
        postgres_dsn = _group_value(
            storage_config,
            settings,
            "indexing_job_store_postgres_dsn",
            "indexing_job_store_postgres_dsn",
        )
        if not postgres_dsn.strip():
            errors.append(
                ConfigIssue(
                    field="INDEXING_JOB_STORE_POSTGRES_DSN",
                    message="must be set when INDEXING_JOB_STORE_PROVIDER=postgres",
                )
            )

    document_catalog_provider = _normalize_config_id(
        _group_value(
            storage_config,
            settings,
            "document_catalog_provider",
            "document_catalog_provider",
            default="memory",
        )
    )
    if document_catalog_provider not in {"memory", "sqlite", "postgres"}:
        errors.append(
            ConfigIssue(
                field="DOCUMENT_CATALOG_PROVIDER",
                message=f"unsupported provider: {document_catalog_provider}",
            )
        )
    if document_catalog_provider == "sqlite":
        sqlite_path = _group_value(
            storage_config,
            settings,
            "document_catalog_sqlite_path",
            "document_catalog_sqlite_path",
        )
        if not sqlite_path.strip():
            errors.append(
                ConfigIssue(
                    field="DOCUMENT_CATALOG_SQLITE_PATH",
                    message="must be set when DOCUMENT_CATALOG_PROVIDER=sqlite",
                )
            )
    if document_catalog_provider == "postgres":
        postgres_dsn = _group_value(
            storage_config,
            settings,
            "document_catalog_postgres_dsn",
            "document_catalog_postgres_dsn",
        )
        if not postgres_dsn.strip():
            errors.append(
                ConfigIssue(
                    field="DOCUMENT_CATALOG_POSTGRES_DSN",
                    message="must be set when DOCUMENT_CATALOG_PROVIDER=postgres",
                )
            )

    agent_runtime = _normalize_config_id(
        _group_value(agent_config, settings, "runtime", "agent_runtime", default="explicit_graph")
    )
    if agent_runtime not in {"explicit_graph", "legacy"}:
        errors.append(
            ConfigIssue(
                field="AGENT_RUNTIME",
                message=f"unsupported runtime: {agent_runtime}",
            )
        )

    prompt_profiles = build_default_prompt_registry().names()
    prompt_profile = _group_value(
        agent_config, settings, "prompt_profile", "prompt_profile", default="default"
    )
    if prompt_profile not in prompt_profiles:
        errors.append(
            ConfigIssue(
                field="PROMPT_PROFILE",
                message=f"unsupported prompt profile: {prompt_profile}",
            )
        )

    known_tools = set(build_default_tool_registry().names())
    for tool_name in parse_enabled_tool_names(
        _group_value(
            agent_config,
            settings,
            "enabled_tools",
            "enabled_tools",
            default="retrieve_knowledge,get_current_time",
        )
    ):
        if tool_name not in known_tools:
            errors.append(
                ConfigIssue(field="ENABLED_TOOLS", message=f"unsupported tool: {tool_name}")
            )

    if _group_value(rag_config, settings, "top_k", "rag_top_k", default=3) < 1:
        errors.append(
            ConfigIssue(field="RAG_TOP_K", message="must be greater than or equal to 1")
        )
    retrieval_profile_registry = build_default_retrieval_profile_registry()
    retrieval_profile = _normalize_config_id(
        _group_value(
            rag_config,
            settings,
            "retrieval_profile",
            "retrieval_profile",
            default="default",
        )
    )
    if retrieval_profile not in retrieval_profile_registry.names():
        errors.append(
            ConfigIssue(
                field="RETRIEVAL_PROFILE",
                message=f"unsupported profile: {retrieval_profile}",
            )
        )

    if (
        _group_value(
            rag_config,
            settings,
            "retrieval_high_recall_top_k_multiplier",
            "retrieval_high_recall_top_k_multiplier",
            default=2,
        )
        < 1
    ):
        errors.append(
            ConfigIssue(
                field="RETRIEVAL_HIGH_RECALL_TOP_K_MULTIPLIER",
                message="must be greater than or equal to 1",
            )
        )

    relaxed_filter_preserve_keys = parse_filter_key_list(
        _group_value(
            rag_config,
            settings,
            "retrieval_relaxed_filter_preserve_keys",
            "retrieval_relaxed_filter_preserve_keys",
            default="space_id,spaceId,tenant_id,tenantId",
        )
    )
    if not relaxed_filter_preserve_keys:
        errors.append(
            ConfigIssue(
                field="RETRIEVAL_RELAXED_FILTER_PRESERVE_KEYS",
                message="must contain at least one filter key",
            )
        )

    query_rewriter_provider = _normalize_config_id(
        _group_value(
            rag_config,
            settings,
            "query_rewriter_provider",
            "query_rewriter_provider",
            default="none",
        )
    )
    if query_rewriter_provider not in {"none", "llm"}:
        errors.append(
            ConfigIssue(
                field="QUERY_REWRITER_PROVIDER",
                message=f"unsupported provider: {query_rewriter_provider}",
            )
        )

    reranker_provider = _normalize_config_id(
        _group_value(
            rag_config,
            settings,
            "reranker_provider",
            "reranker_provider",
            default="none",
        )
    )
    if reranker_provider not in {"none", "llm"}:
        errors.append(
            ConfigIssue(
                field="RERANKER_PROVIDER",
                message=f"unsupported provider: {reranker_provider}",
            )
        )

    context_compressor_provider = _normalize_config_id(
        _group_value(
            rag_config,
            settings,
            "context_compressor_provider",
            "context_compressor_provider",
            default="none",
        )
    )
    if context_compressor_provider not in {"none", "llm"}:
        errors.append(
            ConfigIssue(
                field="CONTEXT_COMPRESSOR_PROVIDER",
                message=f"unsupported provider: {context_compressor_provider}",
            )
        )

    if (
        _group_value(
            rag_config,
            settings,
            "context_compressor_max_characters",
            "context_compressor_max_characters",
            default=1200,
        )
        < 1
    ):
        errors.append(
            ConfigIssue(
                field="CONTEXT_COMPRESSOR_MAX_CHARACTERS",
                message="must be greater than or equal to 1",
            )
        )

    chunk_max_size = _group_value(
        chunking_config, settings, "max_size", "chunk_max_size", default=800
    )
    chunk_overlap = _group_value(
        chunking_config, settings, "overlap", "chunk_overlap", default=100
    )
    if chunk_max_size < 1:
        errors.append(
            ConfigIssue(field="CHUNK_MAX_SIZE", message="must be greater than or equal to 1")
        )

    if chunk_overlap < 0:
        errors.append(
            ConfigIssue(field="CHUNK_OVERLAP", message="must be greater than or equal to 0")
        )
    elif chunk_overlap >= chunk_max_size:
        errors.append(
            ConfigIssue(field="CHUNK_OVERLAP", message="must be less than CHUNK_MAX_SIZE")
        )

    if not _group_value(app_config, settings, "host", "host").strip():
        errors.append(ConfigIssue(field="HOST", message="must not be empty"))

    if not 1 <= _group_value(app_config, settings, "port", "port", default=9900) <= 65535:
        errors.append(ConfigIssue(field="PORT", message="must be between 1 and 65535"))

    milvus_port = _group_value(milvus_config, settings, "port", "milvus_port", default=19530)
    milvus_timeout = _group_value(
        milvus_config, settings, "timeout", "milvus_timeout", default=10000
    )
    if not 1 <= milvus_port <= 65535:
        errors.append(
            ConfigIssue(field="MILVUS_PORT", message="must be between 1 and 65535")
        )

    if milvus_timeout < 1:
        errors.append(
            ConfigIssue(field="MILVUS_TIMEOUT", message="must be greater than or equal to 1")
        )

    cors_origins = parse_cors_origins(
        _group_value(cors_config, settings, "allow_origins", "cors_allow_origins")
    )
    if not cors_origins:
        errors.append(
            ConfigIssue(field="CORS_ALLOW_ORIGINS", message="must contain at least one origin")
        )
    elif _group_value(
        cors_config, settings, "allow_credentials", "cors_allow_credentials", default=True
    ) and "*" in cors_origins:
        errors.append(
            ConfigIssue(
                field="CORS_ALLOW_ORIGINS",
                message="cannot be '*' when CORS_ALLOW_CREDENTIALS is true",
            )
        )

    upload_extensions = parse_allowed_extensions(
        _group_value(upload_config, settings, "allowed_extensions", "upload_allowed_extensions")
    )
    if not upload_extensions:
        errors.append(
            ConfigIssue(
                field="UPLOAD_ALLOWED_EXTENSIONS",
                message="must contain at least one extension",
            )
        )
    if _group_value(upload_config, settings, "max_bytes", "upload_max_bytes", default=0) < 1:
        errors.append(
            ConfigIssue(
                field="UPLOAD_MAX_BYTES",
                message="must be greater than or equal to 1",
            )
        )

    return ConfigValidationReport(errors=errors)


def main(
    argv: list[str] | None = None,
    *,
    settings: Settings | None = None,
    output: TextIO = sys.stdout,
) -> int:
    parser = argparse.ArgumentParser(description="Validate RAGqs startup configuration.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args(argv)

    report = validate_settings(settings or config)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=True, indent=2), file=output)
    elif report.is_valid:
        print("configuration valid", file=output)
    else:
        for issue in report.errors:
            print(f"{issue.field}: {issue.message}", file=output)

    return 0 if report.is_valid else 1


def _is_placeholder_secret(value: str) -> bool:
    normalized = value.strip().strip('"').strip("'").lower()
    if not normalized:
        return True
    return any(
        marker in normalized
        for marker in ["your-api-key", "placeholder", "changeme"]
    )


def _group_value(
    group: Any,
    settings: Any,
    group_name: str,
    flat_name: str,
    default: Any = "",
) -> Any:
    if group is not None and hasattr(group, group_name):
        return getattr(group, group_name)
    return getattr(settings, flat_name, default)


def _normalize_config_id(value: Any) -> str:
    return str(value).strip().lower().replace("-", "_")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
