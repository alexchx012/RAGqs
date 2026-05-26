"""Default provider composition for the RAG foundation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.providers.contracts import (
    ChatModelProvider,
    CheckpointProvider,
    EmbeddingProvider,
    IngestionProvider,
    RetrieverProvider,
    SessionStoreProvider,
    VectorStoreProvider,
)
from app.providers.checkpoints import (
    InMemoryCheckpointProvider,
    PostgresCheckpointProvider,
    SQLiteCheckpointProvider,
)
from app.providers.dashscope import DashScopeChatModelProvider
from app.providers.fakes import (
    FakeChatModelProvider,
    FakeEmbeddingProvider,
    FakeIngestionProvider,
    FakeVectorStoreProvider,
    InMemorySessionStoreProvider,
)
from app.providers.ingestion import VectorIndexIngestionProvider
from app.providers.milvus import MilvusVectorStoreProvider
from app.providers.openai_compatible import (
    OpenAICompatibleChatModelProvider,
    OpenAICompatibleEmbeddingProvider,
)
from app.providers.postgres_session import PostgresSessionStoreProvider
from app.providers.retrieval import VectorStoreRetrieverProvider
from app.providers.selection import ProviderSelection, validate_provider_selection
from app.providers.sqlite_session import SQLiteSessionStoreProvider
from app.retrieval import LLMContextCompressor, LLMQueryRewriter, LLMReranker, RetrievalPipeline


@dataclass(frozen=True)
class ProviderContainer:
    """All replaceable providers required by the current application."""

    chat_model_provider: ChatModelProvider
    embedding_provider: EmbeddingProvider
    vector_store_provider: VectorStoreProvider
    retriever_provider: RetrieverProvider
    session_store_provider: SessionStoreProvider
    ingestion_provider: IngestionProvider
    checkpoint_provider: CheckpointProvider


_default_provider_container: ProviderContainer | None = None


def create_default_provider_container(
    *,
    settings: Any | None = None,
    milvus_manager: Any | None = None,
    chat_model_provider: ChatModelProvider | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    vector_store_provider: VectorStoreProvider | None = None,
    retriever_provider: RetrieverProvider | None = None,
    session_store_provider: SessionStoreProvider | None = None,
    ingestion_provider: IngestionProvider | None = None,
    checkpoint_provider: CheckpointProvider | None = None,
) -> ProviderContainer:
    """Create the default provider graph without opening external connections."""

    if settings is None:
        from app.config import config as settings

    selection = ProviderSelection.from_settings(settings)
    selection_errors = validate_provider_selection(settings)
    if selection_errors:
        raise ValueError(
            "; ".join(f"{field}: {message}" for field, message in selection_errors)
        )

    if milvus_manager is None:
        from app.core.milvus_client import milvus_manager

    if chat_model_provider is None:
        if selection.chat_provider == "fake":
            chat_model_provider = FakeChatModelProvider()
        elif selection.chat_provider == "openai_compatible":
            chat_model_provider = OpenAICompatibleChatModelProvider(
                api_key=settings.openai_compatible_api_key,
                model_name=settings.openai_compatible_model,
                base_url=settings.openai_compatible_base_url,
                temperature=0.7,
            )
        else:
            chat_model_provider = DashScopeChatModelProvider(
                api_key=settings.dashscope_api_key,
                model_name=settings.rag_model,
                temperature=0.7,
            )

    if embedding_provider is None:
        if selection.embedding_provider == "fake":
            embedding_provider = FakeEmbeddingProvider()
        elif selection.embedding_provider == "openai_compatible":
            embedding_provider = OpenAICompatibleEmbeddingProvider(
                api_key=settings.openai_compatible_api_key,
                model=settings.openai_compatible_embedding_model,
                base_url=settings.openai_compatible_base_url,
            )
        else:
            from app.services.vector_embedding_service import vector_embedding_service

            embedding_provider = vector_embedding_service

    if vector_store_provider is None:
        if selection.vector_store_provider == "fake":
            vector_store_provider = FakeVectorStoreProvider()
        else:
            vector_store_provider = MilvusVectorStoreProvider(
                embedding_provider=embedding_provider,
                milvus_manager=milvus_manager,
                collection_name="biz",
                host=settings.milvus_host,
                port=settings.milvus_port,
            )

    if retriever_provider is None:
        base_retriever_provider = VectorStoreRetrieverProvider(
            vector_store_provider=vector_store_provider,
            default_top_k=settings.rag_top_k,
        )
        query_rewriter = None
        if _setting_id(settings, "query_rewriter_provider", "none") == "llm":
            query_rewriter = LLMQueryRewriter(chat_model_provider)

        reranker = None
        if _setting_id(settings, "reranker_provider", "none") == "llm":
            reranker = LLMReranker(chat_model_provider)

        compressor = None
        if _setting_id(settings, "context_compressor_provider", "none") == "llm":
            compressor = LLMContextCompressor(
                chat_model_provider,
                max_characters=getattr(settings, "context_compressor_max_characters", 1200),
            )

        retriever_provider = RetrievalPipeline(
            primary_retriever=base_retriever_provider,
            query_rewriter=query_rewriter,
            reranker=reranker,
            compressor=compressor,
            default_top_k=settings.rag_top_k,
        )

    if session_store_provider is None:
        if selection.session_store_provider == "sqlite":
            session_store_provider = SQLiteSessionStoreProvider(
                settings.session_store_sqlite_path
            )
        elif selection.session_store_provider == "postgres":
            session_store_provider = PostgresSessionStoreProvider(
                settings.session_store_postgres_dsn
            )
        else:
            session_store_provider = InMemorySessionStoreProvider()

    if ingestion_provider is None:
        if selection.ingestion_provider == "fake":
            ingestion_provider = FakeIngestionProvider()
        else:
            from app.ingestion.worker import get_background_indexing_worker
            from app.services.vector_index_service import vector_index_service

            execution_mode = _setting_id(settings, "indexing_execution_mode", "sync")
            background_worker = None
            if execution_mode == "background":
                background_worker = get_background_indexing_worker(
                    index_service=vector_index_service,
                    settings=settings,
                )
            ingestion_provider = VectorIndexIngestionProvider(
                vector_index_service,
                execution_mode=execution_mode,
                background_worker=background_worker,
            )

    if checkpoint_provider is None:
        if selection.checkpoint_provider == "sqlite":
            checkpoint_provider = SQLiteCheckpointProvider(settings.checkpoint_sqlite_path)
        elif selection.checkpoint_provider == "postgres":
            checkpoint_provider = PostgresCheckpointProvider(settings.checkpoint_postgres_dsn)
        else:
            checkpoint_provider = InMemoryCheckpointProvider()

    return ProviderContainer(
        chat_model_provider=chat_model_provider,
        embedding_provider=embedding_provider,
        vector_store_provider=vector_store_provider,
        retriever_provider=retriever_provider,
        session_store_provider=session_store_provider,
        ingestion_provider=ingestion_provider,
        checkpoint_provider=checkpoint_provider,
    )


def get_default_provider_container() -> ProviderContainer:
    """Return the process-wide provider container."""

    global _default_provider_container
    if _default_provider_container is None:
        _default_provider_container = create_default_provider_container()
    return _default_provider_container


def reset_default_provider_container() -> None:
    """Reset the default provider container; intended for tests."""

    global _default_provider_container
    _default_provider_container = None


def _setting_id(settings: Any, field_name: str, default: str) -> str:
    return str(getattr(settings, field_name, default)).strip().lower().replace("-", "_")
