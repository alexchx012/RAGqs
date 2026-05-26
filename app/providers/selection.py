"""Provider selection metadata for configurable foundation deployments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SUPPORTED_CHAT_PROVIDERS = {"dashscope", "openai_compatible", "fake"}
SUPPORTED_EMBEDDING_PROVIDERS = {"dashscope", "openai_compatible", "fake"}
SUPPORTED_VECTOR_STORE_PROVIDERS = {"milvus", "fake"}
SUPPORTED_SESSION_STORE_PROVIDERS = {"memory", "sqlite", "postgres"}
SUPPORTED_RETRIEVAL_AUDIT_STORE_PROVIDERS = {"memory", "sqlite", "postgres"}
SUPPORTED_INGESTION_PROVIDERS = {"vector_index", "fake"}
SUPPORTED_CHECKPOINT_PROVIDERS = {"memory", "sqlite", "postgres"}


@dataclass(frozen=True)
class ProviderSelection:
    chat_provider: str = "dashscope"
    embedding_provider: str = "dashscope"
    vector_store_provider: str = "milvus"
    session_store_provider: str = "sqlite"
    retrieval_audit_store_provider: str = "sqlite"
    ingestion_provider: str = "vector_index"
    checkpoint_provider: str = "sqlite"

    @classmethod
    def from_settings(cls, settings: Any) -> ProviderSelection:
        providers = getattr(settings, "providers", None)
        return cls(
            chat_provider=_normalize(
                _setting_value(settings, providers, "chat_provider", "chat", cls.chat_provider)
            ),
            embedding_provider=_normalize(
                _setting_value(
                    settings,
                    providers,
                    "embedding_provider",
                    "embedding",
                    cls.embedding_provider,
                )
            ),
            vector_store_provider=_normalize(
                _setting_value(
                    settings,
                    providers,
                    "vector_store_provider",
                    "vector_store",
                    cls.vector_store_provider,
                )
            ),
            session_store_provider=_normalize(
                _setting_value(
                    settings,
                    providers,
                    "session_store_provider",
                    "session_store",
                    cls.session_store_provider,
                )
            ),
            retrieval_audit_store_provider=_normalize(
                _setting_value(
                    settings,
                    providers,
                    "retrieval_audit_store_provider",
                    "retrieval_audit_store",
                    cls.retrieval_audit_store_provider,
                )
            ),
            ingestion_provider=_normalize(
                _setting_value(
                    settings,
                    providers,
                    "ingestion_provider",
                    "ingestion",
                    cls.ingestion_provider,
                )
            ),
            checkpoint_provider=_normalize(
                _setting_value(
                    settings,
                    providers,
                    "checkpoint_provider",
                    "checkpoint",
                    cls.checkpoint_provider,
                )
            ),
        )


def validate_provider_selection(settings: Any) -> list[tuple[str, str]]:
    selection = ProviderSelection.from_settings(settings)
    checks = [
        ("CHAT_PROVIDER", selection.chat_provider, SUPPORTED_CHAT_PROVIDERS),
        ("EMBEDDING_PROVIDER", selection.embedding_provider, SUPPORTED_EMBEDDING_PROVIDERS),
        ("VECTOR_STORE_PROVIDER", selection.vector_store_provider, SUPPORTED_VECTOR_STORE_PROVIDERS),
        ("SESSION_STORE_PROVIDER", selection.session_store_provider, SUPPORTED_SESSION_STORE_PROVIDERS),
        (
            "RETRIEVAL_AUDIT_STORE_PROVIDER",
            selection.retrieval_audit_store_provider,
            SUPPORTED_RETRIEVAL_AUDIT_STORE_PROVIDERS,
        ),
        ("INGESTION_PROVIDER", selection.ingestion_provider, SUPPORTED_INGESTION_PROVIDERS),
        ("CHECKPOINT_PROVIDER", selection.checkpoint_provider, SUPPORTED_CHECKPOINT_PROVIDERS),
    ]

    errors: list[tuple[str, str]] = []
    for field, value, supported in checks:
        if value not in supported:
            errors.append((field, f"unsupported provider: {value}"))
    return errors


def _normalize(value: str) -> str:
    return str(value).strip().lower().replace("-", "_")


def _setting_value(
    settings: Any,
    group: Any,
    flat_name: str,
    group_name: str,
    default: str,
) -> str:
    if group is not None and hasattr(group, group_name):
        return getattr(group, group_name)
    return getattr(settings, flat_name, default)
