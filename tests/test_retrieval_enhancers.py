from types import SimpleNamespace

from langchain_core.documents import Document

import app.retrieval as retrieval
from app.providers import RetrievalRequest, RetrievalResult
from app.providers.factory import create_default_provider_container


def test_llm_query_rewriter_and_context_compressor_use_chat_provider_outputs():
    provider = RecordingChatModelProvider(["rewritten agent query", "compressed evidence"])
    rewriter = _class("LLMQueryRewriter")(provider)
    compressor = _class("LLMContextCompressor")(provider, max_characters=40)

    rewritten = rewriter.rewrite("agent?")
    compressed = compressor.compress(
        "agent?",
        [
            Document(
                page_content="very long document evidence",
                metadata={"chunk_id": "chunk-1"},
            )
        ],
    )

    assert rewritten == "rewritten agent query"
    assert compressed == [
        Document(page_content="compressed evidence", metadata={"chunk_id": "chunk-1"})
    ]
    assert len(provider.model.messages) == 2
    assert "Rewrite the question" in provider.model.messages[0][0][1]
    assert "Compress the document chunk" in provider.model.messages[1][0][1]


def test_llm_retrieval_enhancers_fallback_to_original_content_on_empty_model_output():
    provider = RecordingChatModelProvider([" ", ""])
    rewriter = _class("LLMQueryRewriter")(provider)
    compressor = _class("LLMContextCompressor")(provider, max_characters=12)
    original = Document(page_content="original document content", metadata={"source": "guide.md"})

    assert rewriter.rewrite("agent?") == "agent?"
    assert compressor.compress("agent?", [original]) == [
        Document(page_content="original doc", metadata={"source": "guide.md"})
    ]


def test_provider_factory_wires_configured_llm_rewrite_and_compression():
    chat_provider = RecordingChatModelProvider(["rewritten policy", "compressed policy"])
    vector_store = RecordingVectorStore(
        [Document(page_content="full policy document", metadata={"chunk_id": "policy-1"})]
    )
    settings = _settings(
        query_rewriter_provider="llm",
        context_compressor_provider="llm",
        context_compressor_max_characters=20,
    )

    container = create_default_provider_container(
        settings=settings,
        milvus_manager=object(),
        chat_model_provider=chat_provider,
        vector_store_provider=vector_store,
    )
    result = container.retriever_provider.retrieve(RetrievalRequest(query="policy"))

    assert isinstance(container.retriever_provider.query_rewriter, _class("LLMQueryRewriter"))
    assert isinstance(container.retriever_provider.compressor, _class("LLMContextCompressor"))
    assert vector_store.queries == ["rewritten policy"]
    assert result.rewritten_query == "rewritten policy"
    assert result.documents[0].page_content == "compressed policy"
    assert result.debug["stages"] == ["rewrite", "retrieve", "deduplicate", "compress", "sources"]


def test_fake_chat_model_supports_configured_llm_retrieval_enhancers():
    vector_store = RecordingVectorStore(
        [Document(page_content="fake answer evidence", metadata={"chunk_id": "fake-1"})]
    )
    settings = _settings(
        chat_provider="fake",
        query_rewriter_provider="llm",
        context_compressor_provider="llm",
        context_compressor_max_characters=20,
    )

    container = create_default_provider_container(
        settings=settings,
        milvus_manager=object(),
        vector_store_provider=vector_store,
    )
    result = container.retriever_provider.retrieve(RetrievalRequest(query="evidence"))

    assert vector_store.queries == ["fake answer"]
    assert result.rewritten_query == "fake answer"
    assert result.documents == [Document(page_content="fake answer", metadata={"chunk_id": "fake-1"})]


def _class(name: str):
    assert hasattr(retrieval, name)
    return getattr(retrieval, name)


class RecordingChatModelProvider:
    def __init__(self, responses: list[str]):
        self.model = RecordingChatModel(responses)

    def create_chat_model(self, streaming: bool = True):
        return self.model


class RecordingChatModel:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.messages = []

    def invoke(self, messages):
        self.messages.append(messages)
        content = self.responses.pop(0) if self.responses else ""
        return SimpleNamespace(content=content)


class RecordingVectorStore:
    def __init__(self, documents: list[Document]):
        self.documents = documents
        self.queries = []

    def similarity_search(self, query, k=3, filters=None):
        self.queries.append(query)
        return self.documents[:k]

    def add_documents(self, documents):
        return []

    def delete_by_source(self, source):
        return 0

    def delete_by_document_id(self, document_id):
        return 0


def _settings(**overrides):
    values = {
        "dashscope_api_key": "sk-valid",
        "rag_model": "qwen-max",
        "rag_top_k": 3,
        "milvus_host": "127.0.0.1",
        "milvus_port": 19530,
        "chat_provider": "fake",
        "embedding_provider": "fake",
        "vector_store_provider": "fake",
        "session_store_provider": "memory",
        "ingestion_provider": "fake",
        "checkpoint_provider": "memory",
        "query_rewriter_provider": "none",
        "context_compressor_provider": "none",
        "context_compressor_max_characters": 1200,
    }
    values.update(overrides)
    return SimpleNamespace(**values)
