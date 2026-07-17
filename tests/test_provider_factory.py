from types import SimpleNamespace

import pytest
from langchain_core.documents import Document

from app.ingestion.worker import reset_background_indexing_worker
from app.observability.retrieval_audit import (
    PostgresRetrievalAuditStore,
    SQLiteRetrievalAuditStore,
)
from app.providers import (
    ChatModelProvider,
    CheckpointProvider,
    FakeChatModelProvider,
    FakeEmbeddingProvider,
    FakeIngestionProvider,
    IngestionProvider,
    InMemorySessionStoreProvider,
    RetrievalAuditStoreProvider,
    RetrieverProvider,
    SessionStoreProvider,
    VectorStoreProvider,
)
from app.providers.dashscope import DashScopeChatModelProvider
from app.providers.deepseek import DeepSeekChatModelProvider
from app.providers.factory import ProviderContainer, create_default_provider_container
from app.providers.ingestion import VectorIndexIngestionProvider
from app.providers.milvus import MilvusVectorStoreProvider
from app.providers.openai_compatible import (
    OpenAICompatibleChatModelProvider,
    OpenAICompatibleEmbeddingProvider,
)
from app.providers.sqlite_session import SQLiteSessionStoreProvider
from app.retrieval import RetrievalPipeline


class RecordingMilvusManager:
    def __init__(self):
        self.connect_count = 0

    def connect(self):
        self.connect_count += 1
        return object()


def test_default_provider_container_prefers_grouped_openai_compatible_settings():
    settings = SimpleNamespace(
        providers=SimpleNamespace(
            chat="openai_compatible",
            embedding="openai_compatible",
            vector_store="fake",
            session_store="memory",
            retrieval_audit_store="memory",
            ingestion="fake",
            checkpoint="memory",
        ),
        openai_compatible=SimpleNamespace(
            api_key="sk-grouped",
            base_url="https://models.example.com/v1",
            embedding_model="group-embedding",
        ),
        chat_model="group-chat",
        rag=SimpleNamespace(top_k=3),
    )

    container = create_default_provider_container(settings=settings, milvus_manager=object())

    assert isinstance(container.chat_model_provider, OpenAICompatibleChatModelProvider)
    assert container.chat_model_provider.api_key == "sk-grouped"
    assert container.chat_model_provider.model_name == "group-chat"
    assert container.chat_model_provider.base_url == "https://models.example.com/v1"
    assert isinstance(container.embedding_provider, OpenAICompatibleEmbeddingProvider)
    assert container.embedding_provider.model == "group-embedding"


def test_default_provider_container_prefers_grouped_rag_and_milvus_settings():
    settings = SimpleNamespace(
        providers=SimpleNamespace(
            chat="fake",
            embedding="fake",
            vector_store="milvus",
            session_store="memory",
            retrieval_audit_store="memory",
            ingestion="fake",
            checkpoint="memory",
        ),
        milvus=SimpleNamespace(host="milvus.internal", port=19630),
        chat_model="test-chat-model",
        rag=SimpleNamespace(
            top_k=9,
            retrieval_profile="high_recall",
            retrieval_high_recall_top_k_multiplier=3,
            retrieval_relaxed_filter_preserve_keys="space_id,tenant_id",
            query_rewriter_provider="none",
            reranker_provider="none",
            context_compressor_provider="none",
            context_compressor_max_characters=600,
        ),
    )

    container = create_default_provider_container(settings=settings, milvus_manager=object())

    assert isinstance(container.vector_store_provider, MilvusVectorStoreProvider)
    assert container.vector_store_provider.host == "milvus.internal"
    assert container.vector_store_provider.port == 19630
    assert container.retriever_provider.default_top_k == 9
    assert len(container.retriever_provider.additional_retrievers) == 1


def test_default_provider_container_prefers_grouped_storage_settings(tmp_path):
    settings = SimpleNamespace(
        providers=SimpleNamespace(
            chat="fake",
            embedding="fake",
            vector_store="fake",
            session_store="sqlite",
            retrieval_audit_store="sqlite",
            ingestion="fake",
            checkpoint="sqlite",
        ),
        rag=SimpleNamespace(
            top_k=4,
            retrieval_profile="default",
            retrieval_high_recall_top_k_multiplier=2,
            retrieval_relaxed_filter_preserve_keys="space_id,tenant_id",
            query_rewriter_provider="none",
            reranker_provider="none",
            context_compressor_provider="none",
            context_compressor_max_characters=1200,
        ),
        storage=SimpleNamespace(
            session_store_sqlite_path=str(tmp_path / "grouped-sessions.sqlite3"),
            session_store_postgres_dsn="postgresql://rag:secret@db/ragqs-sessions",
            retrieval_audit_sqlite_path=str(tmp_path / "grouped-audits.sqlite3"),
            retrieval_audit_postgres_dsn="postgresql://rag:secret@db/ragqs-audits",
            indexing_execution_mode="sync",
            checkpoint_sqlite_path=str(tmp_path / "grouped-checkpoints.sqlite3"),
            checkpoint_postgres_dsn="postgresql://rag:secret@db/ragqs-checkpoints",
        ),
    )

    container = create_default_provider_container(settings=settings, milvus_manager=object())

    assert isinstance(container.session_store_provider, SQLiteSessionStoreProvider)
    assert container.session_store_provider.db_path == tmp_path / "grouped-sessions.sqlite3"
    assert isinstance(container.retrieval_audit_store_provider, SQLiteRetrievalAuditStore)
    assert (
        container.retrieval_audit_store_provider.db_path
        == tmp_path / "grouped-audits.sqlite3"
    )
    assert container.checkpoint_provider.path == str(tmp_path / "grouped-checkpoints.sqlite3")


def test_default_provider_container_wires_all_boundaries_without_connecting():
    settings = SimpleNamespace(
        dashscope_api_key="unit-test-key",
        chat_model="test-chat-model",
        rag_top_k=5,
        milvus_host="127.0.0.1",
        milvus_port=19530,
    )
    manager = RecordingMilvusManager()

    container = create_default_provider_container(
        settings=settings,
        milvus_manager=manager,
        embedding_provider=FakeEmbeddingProvider(),
        ingestion_provider=FakeIngestionProvider(),
        session_store_provider=InMemorySessionStoreProvider(),
    )

    assert isinstance(container, ProviderContainer)
    assert isinstance(container.chat_model_provider, ChatModelProvider)
    assert isinstance(container.vector_store_provider, VectorStoreProvider)
    assert isinstance(container.retriever_provider, RetrieverProvider)
    assert isinstance(container.retriever_provider, RetrievalPipeline)
    assert isinstance(container.session_store_provider, SessionStoreProvider)
    assert isinstance(container.retrieval_audit_store_provider, RetrievalAuditStoreProvider)
    assert isinstance(container.ingestion_provider, IngestionProvider)
    assert isinstance(container.checkpoint_provider, CheckpointProvider)
    assert container.checkpoint_provider.create_checkpointer() is (
        container.checkpoint_provider.create_checkpointer()
    )
    assert container.retriever_provider.default_top_k == 5
    assert manager.connect_count == 0


def test_default_provider_container_can_enable_background_indexing_mode():
    reset_background_indexing_worker()
    settings = SimpleNamespace(
        dashscope_api_key="unit-test-key",
        chat_model="test-chat-model",
        rag_top_k=5,
        milvus_host="127.0.0.1",
        milvus_port=19530,
        chat_provider="fake",
        embedding_provider="fake",
        vector_store_provider="fake",
        session_store_provider="memory",
        ingestion_provider="vector_index",
        checkpoint_provider="memory",
        indexing_execution_mode="background",
        indexing_worker_poll_interval_seconds=0.01,
    )

    try:
        container = create_default_provider_container(settings=settings, milvus_manager=object())

        assert isinstance(container.ingestion_provider, VectorIndexIngestionProvider)
        assert container.ingestion_provider.execution_mode == "background"
        assert container.ingestion_provider.background_worker is not None
    finally:
        reset_background_indexing_worker()


def test_default_provider_container_can_use_sqlite_retrieval_audit_store(tmp_path):
    settings = SimpleNamespace(
        dashscope_api_key="unit-test-key",
        chat_model="test-chat-model",
        rag_top_k=5,
        milvus_host="127.0.0.1",
        milvus_port=19530,
        chat_provider="fake",
        embedding_provider="fake",
        vector_store_provider="fake",
        session_store_provider="memory",
        retrieval_audit_store_provider="sqlite",
        retrieval_audit_sqlite_path=str(tmp_path / "retrieval-audits.sqlite3"),
        ingestion_provider="fake",
        checkpoint_provider="memory",
    )

    container = create_default_provider_container(settings=settings, milvus_manager=object())

    assert isinstance(container.retrieval_audit_store_provider, SQLiteRetrievalAuditStore)


def test_default_provider_container_can_use_postgres_retrieval_audit_store():
    settings = SimpleNamespace(
        dashscope_api_key="unit-test-key",
        chat_model="test-chat-model",
        rag_top_k=5,
        milvus_host="127.0.0.1",
        milvus_port=19530,
        chat_provider="fake",
        embedding_provider="fake",
        vector_store_provider="fake",
        session_store_provider="memory",
        retrieval_audit_store_provider="postgres",
        retrieval_audit_postgres_dsn="postgresql://rag:secret@db/ragqs-audit",
        ingestion_provider="fake",
        checkpoint_provider="memory",
    )

    container = create_default_provider_container(settings=settings, milvus_manager=object())

    assert isinstance(container.retrieval_audit_store_provider, PostgresRetrievalAuditStore)
    assert (
        container.retrieval_audit_store_provider.dsn
        == "postgresql://rag:secret@db/ragqs-audit"
    )


def test_vector_store_retriever_provider_returns_structured_debug_output():
    class RecordingVectorStore:
        def __init__(self):
            self.calls = []

        def similarity_search(self, query, k=3, filters=None):
            self.calls.append((query, k, filters))
            return [Document(page_content="graph result", metadata={"_source": "graph.md"})]

        def add_documents(self, documents):
            return []

        def delete_by_source(self, source):
            return 0

    from app.providers import RetrievalRequest
    from app.providers.retrieval import VectorStoreRetrieverProvider

    vector_store = RecordingVectorStore()
    retriever = VectorStoreRetrieverProvider(vector_store_provider=vector_store, default_top_k=7)

    result = retriever.retrieve(RetrievalRequest(query="graph", filters={"space": "base"}))

    assert result.query == "graph"
    assert result.documents[0].page_content == "graph result"
    assert result.debug == {
        "provider": "vector_store",
        "top_k": 7,
        "filters": {"space": "base"},
        "returned": 1,
    }
    assert vector_store.calls == [("graph", 7, {"space": "base"})]


def _settings(**overrides):
    values = {
        "dashscope_api_key": "sk-valid",
        "chat_model": "test-chat-model",
        "deepseek_api_key": "",
        "deepseek_base_url": "https://api.deepseek.com",
        "rag_top_k": 3,
        "milvus_host": "127.0.0.1",
        "milvus_port": 19530,
        "chat_provider": None,
        "embedding_provider": "dashscope",
        "vector_store_provider": "milvus",
        "session_store_provider": "memory",
        "session_store_sqlite_path": "data/sessions.sqlite3",
        "session_store_postgres_dsn": "",
        "retrieval_audit_store_provider": "memory",
        "retrieval_audit_sqlite_path": "data/retrieval-audits.sqlite3",
        "retrieval_audit_postgres_dsn": "",
        "indexing_job_store_provider": "memory",
        "indexing_job_store_sqlite_path": "data/indexing-jobs.sqlite3",
        "indexing_job_store_postgres_dsn": "",
        "document_catalog_provider": "memory",
        "document_catalog_sqlite_path": "data/document-catalog.sqlite3",
        "document_catalog_postgres_dsn": "",
        "checkpoint_provider": "memory",
        "checkpoint_sqlite_path": "data/checkpoints.sqlite3",
        "checkpoint_postgres_dsn": "",
        "agent_runtime": "explicit_graph",
        "ingestion_provider": "vector_index",
        "openai_compatible_api_key": "sk-compatible",
        "openai_compatible_base_url": "https://api.example.com/v1",
        "prompt_profile": "default",
        "retrieval_profile": "default",
        "retrieval_high_recall_top_k_multiplier": 2,
        "retrieval_relaxed_filter_preserve_keys": "space_id,spaceId,tenant_id,tenantId",
        "query_rewriter_provider": "none",
        "reranker_provider": "none",
        "context_compressor_provider": "none",
        "context_compressor_max_characters": 1200,
        "enabled_tools": "retrieve_knowledge,get_current_time",
        "cors_allow_origins": "http://127.0.0.1:9900",
        "cors_allow_credentials": True,
        "chunk_max_size": 800,
        "chunk_overlap": 100,
        "host": "0.0.0.0",
        "port": 9900,
        "milvus_timeout": 10000,
        "dashscope_embedding_model": "text-embedding-v4",
        "openai_compatible_embedding_model": "compatible-embedding",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    ("settings", "provider_type"),
    [
        (_settings(deepseek_api_key="ds", chat_model="deepseek-v4-pro"), DeepSeekChatModelProvider),
        (
            _settings(chat_provider="dashscope", dashscope_api_key="qwen", chat_model="qwen-max"),
            DashScopeChatModelProvider,
        ),
        (
            _settings(
                chat_provider="openai_compatible",
                openai_compatible_api_key="sk",
                openai_compatible_base_url="https://x/v1",
                chat_model="remote-chat",
            ),
            OpenAICompatibleChatModelProvider,
        ),
        (_settings(chat_provider="fake", chat_model="ignored-by-fake"), FakeChatModelProvider),
    ],
)
def test_factory_selects_explicit_or_default_chat_provider(settings, provider_type):
    container = create_default_provider_container(settings=settings, milvus_manager=object())
    assert isinstance(container.chat_model_provider, provider_type)


def test_factory_passes_chat_model_to_every_real_chat_provider():
    container = create_default_provider_container(
        settings=_settings(
            chat_provider="dashscope",
            dashscope_api_key="qwen",
            chat_model="qwen-plus",
        ),
        milvus_manager=object(),
    )
    assert container.chat_model_provider.model_name == "qwen-plus"


def test_factory_rejects_unknown_provider_without_dashscope_fallback():
    with pytest.raises(ValueError, match="CHAT_PROVIDER.*unknown"):
        create_default_provider_container(
            settings=_settings(chat_provider="unknown"),
            milvus_manager=object(),
        )


def test_dashscope_embedding_keeps_its_own_model_source(monkeypatch):
    settings = _settings(
        chat_provider="fake",
        chat_model="deepseek-v4-pro",
        embedding_provider="dashscope",
        dashscope_api_key="qwen-key",
        dashscope_embedding_model="text-embedding-v4",
    )
    container = create_default_provider_container(settings=settings, milvus_manager=object())

    assert container.embedding_provider.model == "text-embedding-v4"
    assert container.embedding_provider.dimensions == 1024


def test_explicit_fake_providers_wire_no_key_software_path():
    settings = _settings(
        chat_provider="fake",
        embedding_provider="fake",
        vector_store_provider="fake",
        session_store_provider="memory",
        ingestion_provider="fake",
        checkpoint_provider="memory",
        chat_model="deepseek-v4-pro",
    )

    container = create_default_provider_container(settings=settings, milvus_manager=object())

    assert isinstance(container.chat_model_provider, FakeChatModelProvider)
    assert isinstance(container.embedding_provider, FakeEmbeddingProvider)
    assert container.chat_model_provider.create_chat_model(streaming=False).invoke([]).content == (
        "fake answer"
    )
    assert len(container.embedding_provider.embed_query("hello")) == (
        container.embedding_provider.dimensions
    )
