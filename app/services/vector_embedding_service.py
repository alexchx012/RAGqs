"""向量嵌入服务 - lazy provider-backed compatibility module."""

from app.config import config
from app.providers.contracts import EmbeddingProvider
from app.providers.dashscope import DashScopeEmbeddingProvider


_vector_embedding_service: EmbeddingProvider | None = None


def get_vector_embedding_service() -> EmbeddingProvider:
    """Return the configured embedding provider, initialized on first use."""
    global _vector_embedding_service
    if _vector_embedding_service is None:
        _vector_embedding_service = DashScopeEmbeddingProvider(
            api_key=config.dashscope_api_key,
            model=config.dashscope_embedding_model,
            dimensions=1024,
        )
    return _vector_embedding_service


class LazyEmbeddingProvider:
    """Compatibility wrapper matching the embedding provider protocol."""

    dimensions = 1024

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return get_vector_embedding_service().embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        return get_vector_embedding_service().embed_query(text)


vector_embedding_service = LazyEmbeddingProvider()
