"""向量嵌入服务 - lazy provider-backed compatibility module."""

from typing import Any

from app.config import config
from app.providers.contracts import EmbeddingProvider
from app.providers.dashscope import DashScopeEmbeddingProvider

_vector_embedding_service: EmbeddingProvider | None = None


def build_vector_embedding_service(settings: Any | None = None) -> EmbeddingProvider:
    """Build the configured embedding provider from grouped settings first."""
    active_settings = settings or config
    return DashScopeEmbeddingProvider(
        api_key=_settings_value(
            active_settings,
            "dashscope",
            "api_key",
            "dashscope_api_key",
            "",
        ),
        model=_settings_value(
            active_settings,
            "dashscope",
            "embedding_model",
            "dashscope_embedding_model",
            "text-embedding-v4",
        ),
        dimensions=1024,
    )


def get_vector_embedding_service() -> EmbeddingProvider:
    """Return the configured embedding provider, initialized on first use."""
    global _vector_embedding_service
    if _vector_embedding_service is None:
        _vector_embedding_service = build_vector_embedding_service()
    return _vector_embedding_service


class LazyEmbeddingProvider:
    """Compatibility wrapper matching the embedding provider protocol."""

    dimensions = 1024

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return get_vector_embedding_service().embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        return get_vector_embedding_service().embed_query(text)


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


vector_embedding_service = LazyEmbeddingProvider()
