"""Default provider composition for the RAG foundation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.observability.retrieval_audit import (
    InMemoryRetrievalAuditStore,
    PostgresRetrievalAuditStore,
    SQLiteRetrievalAuditStore,
)
from app.providers.checkpoints import (
    InMemoryCheckpointProvider,
    PostgresCheckpointProvider,
    SQLiteCheckpointProvider,
)
from app.providers.contracts import (
    ChatModelProvider,
    CheckpointProvider,
    EmbeddingProvider,
    IngestionProvider,
    RetrievalAuditStoreProvider,
    RetrieverProvider,
    SessionStoreProvider,
    VectorStoreProvider,
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
from app.retrieval import (
    LLMContextCompressor,
    LLMQueryRewriter,
    LLMReranker,
    RetrievalPipeline,
    build_default_retrieval_profile_registry,
    build_retrievers_for_profile,
)


@dataclass(frozen=True)
class ProviderContainer:
    """All replaceable providers required by the current application."""

    chat_model_provider: ChatModelProvider
    embedding_provider: EmbeddingProvider
    vector_store_provider: VectorStoreProvider
    retriever_provider: RetrieverProvider
    session_store_provider: SessionStoreProvider
    retrieval_audit_store_provider: RetrievalAuditStoreProvider
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
    retrieval_audit_store_provider: RetrievalAuditStoreProvider | None = None,
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
                api_key=_settings_value(
                    settings,
                    "openai_compatible",
                    "api_key",
                    "openai_compatible_api_key",
                    "",
                ),
                model_name=getattr(settings, "chat_model", ""),
                base_url=_settings_value(
                    settings,
                    "openai_compatible",
                    "base_url",
                    "openai_compatible_base_url",
                    "",
                ),
                temperature=0.7,
            )
        else:
            chat_model_provider = DashScopeChatModelProvider(
                api_key=_settings_value(
                    settings,
                    "dashscope",
                    "api_key",
                    "dashscope_api_key",
                    "",
                ),
                model_name=getattr(settings, "chat_model", ""),
                temperature=0.7,
            )

    if embedding_provider is None:
        if selection.embedding_provider == "fake":
            embedding_provider = FakeEmbeddingProvider()
        elif selection.embedding_provider == "openai_compatible":
            embedding_provider = OpenAICompatibleEmbeddingProvider(
                api_key=_settings_value(
                    settings,
                    "openai_compatible",
                    "api_key",
                    "openai_compatible_api_key",
                    "",
                ),
                model=_settings_value(
                    settings,
                    "openai_compatible",
                    "embedding_model",
                    "openai_compatible_embedding_model",
                    "",
                ),
                base_url=_settings_value(
                    settings,
                    "openai_compatible",
                    "base_url",
                    "openai_compatible_base_url",
                    "",
                ),
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
                host=_settings_value(settings, "milvus", "host", "milvus_host", "localhost"),
                port=_settings_value(settings, "milvus", "port", "milvus_port", 19530),
            )

    if retriever_provider is None:
        rag_top_k = _settings_value(settings, "rag", "top_k", "rag_top_k", 3)
        base_retriever_provider = VectorStoreRetrieverProvider(
            vector_store_provider=vector_store_provider,
            default_top_k=rag_top_k,
        )
        retrieval_profile_registry = build_default_retrieval_profile_registry(
            high_recall_top_k_multiplier=_settings_value(
                settings,
                "rag",
                "retrieval_high_recall_top_k_multiplier",
                "retrieval_high_recall_top_k_multiplier",
                2,
            ),
            relaxed_filter_preserve_keys=_settings_value(
                settings,
                "rag",
                "retrieval_relaxed_filter_preserve_keys",
                "retrieval_relaxed_filter_preserve_keys",
                "space_id,spaceId,tenant_id,tenantId",
            ),
        )
        retrieval_profile_name = _setting_id(
            _settings_value(
                settings,
                "rag",
                "retrieval_profile",
                "retrieval_profile",
                "default",
            )
        )
        try:
            retrieval_profile = retrieval_profile_registry.get(retrieval_profile_name)
        except KeyError as exc:
            raise ValueError(
                f"RETRIEVAL_PROFILE: unsupported profile: {retrieval_profile_name}"
            ) from exc
        primary_retriever, additional_retrievers = build_retrievers_for_profile(
            base_retriever_provider,
            retrieval_profile,
        )

        query_rewriter = None
        if (
            _setting_id(
                _settings_value(
                    settings,
                    "rag",
                    "query_rewriter_provider",
                    "query_rewriter_provider",
                    "none",
                )
            )
            == "llm"
        ):
            query_rewriter = LLMQueryRewriter(chat_model_provider)

        reranker = None
        if (
            _setting_id(
                _settings_value(
                    settings,
                    "rag",
                    "reranker_provider",
                    "reranker_provider",
                    "none",
                )
            )
            == "llm"
        ):
            reranker = LLMReranker(chat_model_provider)

        compressor = None
        if (
            _setting_id(
                _settings_value(
                    settings,
                    "rag",
                    "context_compressor_provider",
                    "context_compressor_provider",
                    "none",
                )
            )
            == "llm"
        ):
            compressor = LLMContextCompressor(
                chat_model_provider,
                max_characters=_settings_value(
                    settings,
                    "rag",
                    "context_compressor_max_characters",
                    "context_compressor_max_characters",
                    1200,
                ),
            )

        retriever_provider = RetrievalPipeline(
            primary_retriever=primary_retriever,
            additional_retrievers=additional_retrievers,
            query_rewriter=query_rewriter,
            reranker=reranker,
            compressor=compressor,
            default_top_k=rag_top_k,
        )

    if session_store_provider is None:
        if selection.session_store_provider == "sqlite":
            session_store_provider = SQLiteSessionStoreProvider(
                _settings_value(
                    settings,
                    "storage",
                    "session_store_sqlite_path",
                    "session_store_sqlite_path",
                    "data/sessions.sqlite3",
                )
            )
        elif selection.session_store_provider == "postgres":
            session_store_provider = PostgresSessionStoreProvider(
                _settings_value(
                    settings,
                    "storage",
                    "session_store_postgres_dsn",
                    "session_store_postgres_dsn",
                    "",
                )
            )
        else:
            session_store_provider = InMemorySessionStoreProvider()

    if retrieval_audit_store_provider is None:
        if selection.retrieval_audit_store_provider == "sqlite":
            retrieval_audit_store_provider = SQLiteRetrievalAuditStore(
                _settings_value(
                    settings,
                    "storage",
                    "retrieval_audit_sqlite_path",
                    "retrieval_audit_sqlite_path",
                    "data/retrieval-audits.sqlite3",
                )
            )
        elif selection.retrieval_audit_store_provider == "postgres":
            retrieval_audit_store_provider = PostgresRetrievalAuditStore(
                _settings_value(
                    settings,
                    "storage",
                    "retrieval_audit_postgres_dsn",
                    "retrieval_audit_postgres_dsn",
                    "",
                )
            )
        else:
            retrieval_audit_store_provider = InMemoryRetrievalAuditStore()

    if ingestion_provider is None:
        if selection.ingestion_provider == "fake":
            ingestion_provider = FakeIngestionProvider()
        else:
            from app.ingestion.worker import get_background_indexing_worker
            from app.services.vector_index_service import vector_index_service

            execution_mode = _setting_id(
                _settings_value(
                    settings,
                    "storage",
                    "indexing_execution_mode",
                    "indexing_execution_mode",
                    "sync",
                )
            )
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
            checkpoint_provider = SQLiteCheckpointProvider(
                _settings_value(
                    settings,
                    "storage",
                    "checkpoint_sqlite_path",
                    "checkpoint_sqlite_path",
                    "data/checkpoints.sqlite3",
                )
            )
        elif selection.checkpoint_provider == "postgres":
            checkpoint_provider = PostgresCheckpointProvider(
                _settings_value(
                    settings,
                    "storage",
                    "checkpoint_postgres_dsn",
                    "checkpoint_postgres_dsn",
                    "",
                )
            )
        else:
            checkpoint_provider = InMemoryCheckpointProvider()

    return ProviderContainer(
        chat_model_provider=chat_model_provider,
        embedding_provider=embedding_provider,
        vector_store_provider=vector_store_provider,
        retriever_provider=retriever_provider,
        session_store_provider=session_store_provider,
        retrieval_audit_store_provider=retrieval_audit_store_provider,
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


def _setting_id(value: Any) -> str:
    return str(value).strip().lower().replace("-", "_")
