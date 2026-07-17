from types import SimpleNamespace

import pytest
from langchain_core.documents import Document
from langchain_core.messages import AIMessage, HumanMessage

from app.providers import (
    ChatModelProvider,
    EmbeddingProvider,
    FakeChatModelProvider,
    FakeEmbeddingProvider,
    FakeIngestionProvider,
    FakeRetrieverProvider,
    FakeVectorStoreProvider,
    IngestionProvider,
    InMemorySessionStoreProvider,
    RetrievalRequest,
    RetrieverProvider,
    SessionStoreProvider,
    VectorStoreProvider,
)
from app.providers.dashscope import DashScopeChatModelProvider, DashScopeEmbeddingProvider
from app.providers.milvus import MilvusVectorStoreProvider


def test_fake_embedding_provider_is_deterministic_and_matches_contract():
    provider = FakeEmbeddingProvider(dimensions=4)

    assert isinstance(provider, EmbeddingProvider)
    assert provider.embed_query("hello") == provider.embed_query("hello")
    assert provider.embed_query("hello") != provider.embed_query("world")
    assert provider.embed_documents(["hello", "world"]) == [
        provider.embed_query("hello"),
        provider.embed_query("world"),
    ]


def test_dashscope_embedding_provider_validates_api_key_before_client_use():
    with pytest.raises(ValueError, match="DASHSCOPE_API_KEY"):
        DashScopeEmbeddingProvider(api_key="")


def test_dashscope_embedding_provider_rejects_env_example_placeholder():
    with pytest.raises(ValueError, match="DASHSCOPE_API_KEY"):
        DashScopeEmbeddingProvider(api_key="your-dashscope-api-key")


def test_dashscope_chat_model_provider_requires_model_name_from_caller():
    with pytest.raises(TypeError):
        DashScopeChatModelProvider(api_key="sk-valid")  # type: ignore[call-arg]


def test_dashscope_chat_model_provider_validates_api_key_when_creating_model():
    provider = DashScopeChatModelProvider(api_key="", model_name="test-chat-model")

    with pytest.raises(ValueError, match="DASHSCOPE_API_KEY"):
        provider.create_chat_model(streaming=False)


def test_dashscope_chat_model_provider_rejects_env_example_placeholder():
    provider = DashScopeChatModelProvider(
        api_key="your-dashscope-api-key",
        model_name="test-chat-model",
    )

    with pytest.raises(ValueError, match="DASHSCOPE_API_KEY"):
        provider.create_chat_model(streaming=False)


def test_vector_embedding_service_is_lazy_provider_backed():
    from app.services import vector_embedding_service as module

    assert hasattr(module, "get_vector_embedding_service")
    assert isinstance(module.vector_embedding_service, EmbeddingProvider)


def test_vector_embedding_service_builder_prefers_grouped_dashscope_settings():
    from app.services.vector_embedding_service import build_vector_embedding_service

    settings = SimpleNamespace(
        dashscope=SimpleNamespace(
            api_key="sk-grouped",
            embedding_model="grouped-embedding",
        ),
        dashscope_api_key="sk-flat",
        dashscope_embedding_model="flat-embedding",
    )

    provider = build_vector_embedding_service(settings=settings)

    assert provider.model == "grouped-embedding"
    assert provider.dimensions == 1024


def test_fake_vector_store_provider_adds_searches_and_deletes_by_source():
    provider = FakeVectorStoreProvider()
    documents = [
        Document(page_content="alpha rag content", metadata={"_source": "a.md"}),
        Document(page_content="beta operations content", metadata={"_source": "b.md"}),
        Document(page_content="legacy source path content", metadata={"source_path": "a.md"}),
        Document(page_content="legacy source content", metadata={"source": "a.md"}),
    ]

    ids = provider.add_documents(documents)

    assert isinstance(provider, VectorStoreProvider)
    assert len(ids) == 4
    assert [doc.page_content for doc in provider.similarity_search("rag", k=3)] == [
        "alpha rag content"
    ]
    assert provider.delete_by_source("a.md") == 3
    assert provider.similarity_search("rag", k=3) == []


def test_fake_vector_store_provider_deletes_by_document_id():
    provider = FakeVectorStoreProvider()
    provider.add_documents(
        [
            Document(page_content="old chunk 1", metadata={"document_id": "doc-a"}),
            Document(page_content="old chunk 2", metadata={"document_id": "doc-a"}),
            Document(page_content="other chunk", metadata={"document_id": "doc-b"}),
        ]
    )

    assert provider.delete_by_document_id("doc-a") == 2
    assert [doc.page_content for doc in provider.documents] == ["other chunk"]


def test_milvus_vector_store_provider_is_lazy_and_factory_backed():
    class FakeMilvusManager:
        def __init__(self):
            self.connect_count = 0

        def connect(self):
            self.connect_count += 1
            return object()

    class FakeMilvusStore:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def similarity_search(self, query, k=3, **kwargs):
            return [Document(page_content=query, metadata=kwargs)]

    created = []

    def factory(**kwargs):
        store = FakeMilvusStore(**kwargs)
        created.append(store)
        return store

    manager = FakeMilvusManager()
    provider = MilvusVectorStoreProvider(
        embedding_provider=FakeEmbeddingProvider(),
        milvus_manager=manager,
        collection_name="unit_test_collection",
        host="127.0.0.1",
        port=19530,
        vector_store_factory=factory,
    )

    assert isinstance(provider, VectorStoreProvider)
    assert manager.connect_count == 0
    assert provider.similarity_search("hello", k=1)[0].page_content == "hello"
    assert manager.connect_count == 1
    assert created[0].kwargs["collection_name"] == "unit_test_collection"
    assert created[0].kwargs["connection_args"] == {"host": "127.0.0.1", "port": 19530}


def test_milvus_client_manager_prefers_grouped_settings():
    from app.core.milvus_client import MilvusClientManager

    settings = SimpleNamespace(
        milvus=SimpleNamespace(host="milvus.internal", port=19630, timeout=12345),
        milvus_host="flat-host",
        milvus_port=19530,
        milvus_timeout=10000,
    )

    manager = MilvusClientManager(settings=settings)

    assert manager.host == "milvus.internal"
    assert manager.port == 19630
    assert manager.timeout_ms == 12345
    assert manager.uri == "http://milvus.internal:19630"


def test_vector_store_manager_prefers_grouped_milvus_settings():
    from app.services.vector_store_manager import VectorStoreManager

    created = []

    def provider_factory(**kwargs):
        created.append(kwargs)
        return FakeVectorStoreProvider()

    settings = SimpleNamespace(
        milvus=SimpleNamespace(host="milvus.grouped", port=19631),
        milvus_host="flat-host",
        milvus_port=19530,
    )

    manager = VectorStoreManager(
        settings=settings,
        embedding_provider=FakeEmbeddingProvider(),
        milvus_manager=object(),
        provider_factory=provider_factory,
    )

    assert manager.provider is not None
    assert created[0]["host"] == "milvus.grouped"
    assert created[0]["port"] == 19631
    assert created[0]["collection_name"] == "biz"


def test_milvus_vector_store_provider_deletes_by_document_id_expression():
    class DeleteResult:
        delete_count = 4

    class FakeCollection:
        def __init__(self):
            self.expressions = []

        def delete(self, expr):
            self.expressions.append(expr)
            return DeleteResult()

    class FakeMilvusManager:
        def __init__(self):
            self.connect_count = 0
            self.collection = FakeCollection()

        def connect(self):
            self.connect_count += 1

        def get_collection(self):
            return self.collection

    manager = FakeMilvusManager()
    provider = MilvusVectorStoreProvider(
        embedding_provider=FakeEmbeddingProvider(),
        milvus_manager=manager,
        collection_name="unit_test_collection",
        host="127.0.0.1",
        port=19530,
    )

    deleted_count = provider.delete_by_document_id("doc-a")

    assert deleted_count == 4
    assert manager.connect_count == 1
    assert manager.collection.expressions == ['metadata["document_id"] == "doc-a"']


def test_milvus_vector_store_provider_deletes_legacy_source_metadata_fields():
    class DeleteResult:
        delete_count = 1

    class FakeCollection:
        def __init__(self):
            self.expressions = []

        def delete(self, expr):
            self.expressions.append(expr)
            return DeleteResult()

    class FakeMilvusManager:
        def __init__(self):
            self.connect_count = 0
            self.collection = FakeCollection()

        def connect(self):
            self.connect_count += 1

        def get_collection(self):
            return self.collection

    manager = FakeMilvusManager()
    provider = MilvusVectorStoreProvider(
        embedding_provider=FakeEmbeddingProvider(),
        milvus_manager=manager,
        collection_name="unit_test_collection",
        host="127.0.0.1",
        port=19530,
    )

    deleted_count = provider.delete_by_source('C:/docs/"legacy".md')

    assert deleted_count == 3
    assert manager.connect_count == 3
    assert manager.collection.expressions == [
        'metadata["_source"] == "C:/docs/\\"legacy\\".md"',
        'metadata["source_path"] == "C:/docs/\\"legacy\\".md"',
        'metadata["source"] == "C:/docs/\\"legacy\\".md"',
    ]


def test_fake_retriever_provider_returns_structured_result():
    vector_store = FakeVectorStoreProvider()
    vector_store.add_documents(
        [
            Document(page_content="langgraph state graph", metadata={"_source": "graph.md"}),
            Document(page_content="milvus vector search", metadata={"_source": "vector.md"}),
        ]
    )
    retriever = FakeRetrieverProvider(vector_store)

    result = retriever.retrieve(RetrievalRequest(query="state", top_k=2))

    assert isinstance(retriever, RetrieverProvider)
    assert result.query == "state"
    assert result.documents[0].page_content == "langgraph state graph"
    assert result.debug["provider"] == "fake"


@pytest.mark.asyncio
async def test_fake_chat_model_provider_returns_ai_messages():
    provider = FakeChatModelProvider(response="foundation answer")
    model = provider.create_chat_model(streaming=False)

    response = await model.ainvoke([HumanMessage(content="question")])

    assert isinstance(provider, ChatModelProvider)
    assert isinstance(response, AIMessage)
    assert response.content == "foundation answer"


def test_in_memory_session_store_provider_tracks_messages_by_session():
    provider = InMemorySessionStoreProvider()

    provider.append_message("s1", "user", "hello")
    provider.append_message("s1", "assistant", "hi")
    provider.append_message("s2", "user", "other")

    assert isinstance(provider, SessionStoreProvider)
    assert [message.content for message in provider.get_messages("s1")] == ["hello", "hi"]
    assert provider.clear("s1") is True
    assert provider.get_messages("s1") == []
    assert [message.content for message in provider.get_messages("s2")] == ["other"]


def test_fake_ingestion_provider_records_indexing_work():
    provider = FakeIngestionProvider()

    file_result = provider.index_file("docs/example.md")
    directory_result = provider.index_directory("docs")

    assert isinstance(provider, IngestionProvider)
    assert file_result.success is True
    assert file_result.document_count == 1
    assert directory_result.success is True
    assert provider.indexed_paths == ["docs/example.md", "docs"]
