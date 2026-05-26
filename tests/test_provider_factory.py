from types import SimpleNamespace

from langchain_core.documents import Document

from app.ingestion.worker import reset_background_indexing_worker
from app.providers import (
    ChatModelProvider,
    CheckpointProvider,
    FakeEmbeddingProvider,
    FakeIngestionProvider,
    InMemorySessionStoreProvider,
    IngestionProvider,
    RetrieverProvider,
    SessionStoreProvider,
    VectorStoreProvider,
)
from app.providers.factory import ProviderContainer, create_default_provider_container
from app.providers.ingestion import VectorIndexIngestionProvider
from app.retrieval import RetrievalPipeline


class RecordingMilvusManager:
    def __init__(self):
        self.connect_count = 0

    def connect(self):
        self.connect_count += 1
        return object()


def test_default_provider_container_wires_all_boundaries_without_connecting():
    settings = SimpleNamespace(
        dashscope_api_key="unit-test-key",
        rag_model="qwen-max",
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
        rag_model="qwen-max",
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

    from app.providers.retrieval import VectorStoreRetrieverProvider
    from app.providers import RetrievalRequest

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
