from __future__ import annotations

from typing import Any

from app.platform.config import PlatformSettings
from app.platform.errors import PlatformError

from .embedding import (
    DEFAULT_EMBEDDING_BASE_URL,
    EmbeddingConfig,
    EmbeddingProvider,
    InMemoryEmbeddingProvider,
    OpenAICompatibleEmbedding,
)
from .meilisearch import HttpMeilisearchClient, MeilisearchSparseIndexProvider
from .milvus import HttpMilvusClient, MilvusIndexWriter
from .observability import PROVIDER_ANALYZER_PROBE_ROUTE, record_index_observation
from .opensearch import HttpOpenSearchClient, OpenSearchSparseIndexProvider
from .providers import InMemoryIndexWriter, InMemorySparseIndexProvider, build_sparse_provider


def _secret(value: Any) -> str | None:
    if value is None:
        return None
    raw = value.get_secret_value() if hasattr(value, "get_secret_value") else value
    text = str(raw).strip()
    return text or None


def build_embedding_config(settings: PlatformSettings) -> EmbeddingConfig:
    index = settings.index
    return EmbeddingConfig(
        base_url=index.embedding_base_url or DEFAULT_EMBEDDING_BASE_URL,
        api_key=_secret(index.embedding_api_key) or "",
        model=index.embedding_model or "",
        revision=index.embedding_revision or index.embedding_model or "",
        dimension=int(index.embedding_dimension or 0),
        metric=index.embedding_metric,
    )


def build_embedding_provider(settings: PlatformSettings) -> EmbeddingProvider:
    if settings.index.embedding_provider == "memory":
        if settings.profile == "production":
            raise RuntimeError("production does not accept memory or test indexing adapters")
        return InMemoryEmbeddingProvider(build_embedding_config(settings))
    return OpenAICompatibleEmbedding(build_embedding_config(settings))


def build_dense_writer(
    settings: PlatformSettings,
    embedding: EmbeddingProvider | None,
    *,
    allow_create: bool,
) -> Any:
    if settings.index.vector_provider == "memory":
        if settings.profile == "production":
            raise RuntimeError("production does not accept memory or test indexing adapters")
        return InMemoryIndexWriter(provider_name="dense-memory")
    if embedding is None:
        raise RuntimeError("Milvus dense backend requires an embedding provider")
    if not settings.index.vector_uri:
        raise RuntimeError("Milvus dense backend requires RAG_INDEX_VECTOR_URI")
    return MilvusIndexWriter(
        HttpMilvusClient(
            settings.index.vector_uri,
            token=_secret(settings.index.vector_token),
        ),
        embedding,
        collection_prefix=settings.index.vector_collection_prefix,
        allow_create_collection=allow_create,
    )


def build_configured_sparse_provider(settings: PlatformSettings, *, allow_create: bool) -> Any:
    if settings.index.sparse_provider.startswith("opensearch"):
        if not settings.index.sparse_url:
            raise RuntimeError("OpenSearch requires RAG_INDEX_SPARSE_URL")
        username = settings.index.sparse_username
        password = _secret(settings.index.sparse_password)
        ca_path = settings.index.sparse_ca_path
        if not username or not password or not ca_path:
            raise RuntimeError(
                "OpenSearch requires username, password, and RAG_INDEX_SPARSE_CA_PATH"
            )
        return OpenSearchSparseIndexProvider(
            HttpOpenSearchClient(
                settings.index.sparse_url,
                username=username,
                password=password,
                ca_path=ca_path,
            ),
            index_name=settings.index.sparse_index,
            allow_create_index=allow_create,
            jvm_heap_min_bytes=int(settings.index.sparse_jvm_heap_min_gb * 1024**3),
        )
    if settings.index.sparse_url:
        if settings.index.sparse_provider != "meilisearch":
            raise PlatformError(
                "provider_not_supported", "Sparse index provider is not supported", {}, 422
            )
        api_key = _secret(settings.index.sparse_api_key)
        if not api_key:
            raise RuntimeError("Meilisearch requires RAG_INDEX_SPARSE_API_KEY")
        return MeilisearchSparseIndexProvider(
            HttpMeilisearchClient(settings.index.sparse_url, api_key=api_key),
            index_name=settings.index.sparse_index,
            data_path=settings.index.sparse_data_path,
            allow_create_index=allow_create,
        )
    if settings.profile == "production":
        raise RuntimeError("production requires explicit indexing dense and sparse backends")
    return build_sparse_provider(settings.index.sparse_provider)


def probe_configured_backends(*backends: Any, metrics: Any | None = None) -> None:
    for backend in backends:
        probe = getattr(backend, "probe", None)
        if callable(probe):
            try:
                probe()
            except Exception:
                record_index_observation(
                    metrics,
                    PROVIDER_ANALYZER_PROBE_ROUTE,
                    success=False,
                )
                raise
            record_index_observation(
                metrics,
                PROVIDER_ANALYZER_PROBE_ROUTE,
                success=True,
            )


def is_memory_indexing_adapter(value: Any) -> bool:
    return isinstance(
        value,
        (InMemoryIndexWriter, InMemorySparseIndexProvider, InMemoryEmbeddingProvider),
    )


__all__ = [
    "build_configured_sparse_provider",
    "build_dense_writer",
    "build_embedding_config",
    "build_embedding_provider",
    "is_memory_indexing_adapter",
    "probe_configured_backends",
]
