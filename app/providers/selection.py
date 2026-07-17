"""Provider selection metadata for configurable foundation deployments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SUPPORTED_CHAT_PROVIDERS = {"deepseek", "dashscope", "openai_compatible", "fake"}
SUPPORTED_EMBEDDING_PROVIDERS = {"dashscope", "openai_compatible", "fake"}
SUPPORTED_VECTOR_STORE_PROVIDERS = {"milvus", "fake"}
SUPPORTED_SESSION_STORE_PROVIDERS = {"memory", "sqlite", "postgres"}
SUPPORTED_RETRIEVAL_AUDIT_STORE_PROVIDERS = {"memory", "sqlite", "postgres"}
SUPPORTED_INGESTION_PROVIDERS = {"vector_index", "fake"}
SUPPORTED_CHECKPOINT_PROVIDERS = {"memory", "sqlite", "postgres"}


def is_valid_secret(value: object) -> bool:
    """Return True when value looks like a real secret rather than a placeholder."""

    if value is None:
        return False
    normalized = str(value).strip().strip('"').strip("'").lower()
    if not normalized:
        return False
    if normalized.startswith("your-") and "key" in normalized:
        return False
    return not any(
        marker in normalized for marker in ("your-api-key", "placeholder", "changeme")
    )


@dataclass(frozen=True)
class ProviderSelection:
    chat_provider: str = "deepseek"
    chat_provider_source: str = "default_candidate"
    embedding_provider: str = "dashscope"
    vector_store_provider: str = "milvus"
    session_store_provider: str = "sqlite"
    retrieval_audit_store_provider: str = "sqlite"
    ingestion_provider: str = "vector_index"
    checkpoint_provider: str = "sqlite"

    @classmethod
    def from_settings(cls, settings: Any) -> ProviderSelection:
        providers = getattr(settings, "providers", None)
        explicit_chat = _explicit_chat_provider(settings, providers)
        chat_provider, chat_provider_source = _select_chat_provider(settings, explicit_chat)
        return cls(
            chat_provider=chat_provider,
            chat_provider_source=chat_provider_source,
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


def _select_chat_provider(settings: Any, explicit: str | None) -> tuple[str, str]:
    if explicit is not None:
        return _normalize(explicit), "explicit"
    deepseek_key = _setting_value(
        settings, getattr(settings, "deepseek", None), "deepseek_api_key", "api_key", ""
    )
    dashscope_key = _setting_value(
        settings, getattr(settings, "dashscope", None), "dashscope_api_key", "api_key", ""
    )
    available = {
        "deepseek": is_valid_secret(deepseek_key),
        "dashscope": is_valid_secret(dashscope_key),
    }
    for provider in ("deepseek", "dashscope"):
        if available[provider]:
            return provider, "automatic"
    return "deepseek", "default_candidate"


def _explicit_chat_provider(settings: Any, providers: Any) -> str | None:
    raw = _setting_value(settings, providers, "chat_provider", "chat", None)
    if raw is None:
        return None
    stripped = str(raw).strip()
    return stripped or None


def _normalize(value: str) -> str:
    return str(value).strip().lower().replace("-", "_")


def _setting_value(
    settings: Any,
    group: Any,
    flat_name: str,
    group_name: str,
    default: str | None,
) -> str | None:
    if group is not None and hasattr(group, group_name):
        return getattr(group, group_name)
    return getattr(settings, flat_name, default)
