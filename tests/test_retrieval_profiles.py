from types import SimpleNamespace

from langchain_core.documents import Document

import app.retrieval as retrieval
from app.operations.config_validation import validate_settings
from app.providers import RetrievalRequest, RetrievalResult
from app.providers.factory import create_default_provider_container


class RecordingRetriever:
    def __init__(self, documents: list[Document]):
        self.documents = documents
        self.requests: list[RetrievalRequest] = []

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        self.requests.append(request)
        return RetrievalResult(
            query=request.query,
            documents=self.documents,
            debug={"provider": "recording", "filters": request.filters, "top_k": request.top_k},
        )


class RecordingVectorStore:
    def __init__(self, documents: list[Document]):
        self.documents = documents
        self.calls = []

    def similarity_search(self, query, k=3, filters=None):
        self.calls.append({"query": query, "k": k, "filters": dict(filters or {})})
        return self.documents[:k]

    def add_documents(self, documents):
        return []

    def delete_by_source(self, source):
        return 0

    def delete_by_document_id(self, document_id):
        return 0


def test_high_recall_profile_adds_widened_and_relaxed_retrievers():
    profile = retrieval.build_default_retrieval_profile_registry(
        high_recall_top_k_multiplier=2,
        relaxed_filter_preserve_keys=("space_id", "tenant_id"),
    ).get("high_recall")
    base = RecordingRetriever(
        [
            Document(page_content="strict chunk", metadata={"chunk_id": "strict"}),
            Document(page_content="relaxed chunk", metadata={"chunk_id": "relaxed"}),
        ]
    )

    primary, additional = retrieval.build_retrievers_for_profile(base, profile)
    pipeline = retrieval.RetrievalPipeline(
        primary_retriever=primary,
        additional_retrievers=additional,
        default_top_k=3,
    )
    result = pipeline.retrieve(
        RetrievalRequest(
            query="retention policy",
            top_k=3,
            filters={"space_id": "hr", "document_type": "policy", "tenant_id": "acme"},
        )
    )

    assert len(additional) == 1
    assert [request.top_k for request in base.requests] == [6, 6]
    assert base.requests[0].filters == {
        "space_id": "hr",
        "document_type": "policy",
        "tenant_id": "acme",
    }
    assert base.requests[1].filters == {"space_id": "hr", "tenant_id": "acme"}
    assert result.debug["retriever_count"] == 2
    assert result.debug["profile"] == "high_recall"
    assert result.debug["retrievers"][1]["dropped_filters"] == ["document_type"]


def test_provider_factory_wires_configured_high_recall_profile():
    vector_store = RecordingVectorStore(
        [
            Document(page_content="strict match", metadata={"chunk_id": "strict"}),
            Document(page_content="fallback match", metadata={"chunk_id": "fallback"}),
        ]
    )
    settings = _settings(
        retrieval_profile="high_recall",
        retrieval_high_recall_top_k_multiplier=2,
        retrieval_relaxed_filter_preserve_keys="space_id,tenant_id",
    )

    container = create_default_provider_container(
        settings=settings,
        milvus_manager=object(),
        vector_store_provider=vector_store,
    )
    result = container.retriever_provider.retrieve(
        RetrievalRequest(
            query="policy",
            top_k=2,
            filters={"space_id": "hr", "document_type": "policy", "tenant_id": "acme"},
        )
    )

    assert len(vector_store.calls) == 2
    assert vector_store.calls[0] == {
        "query": "policy",
        "k": 4,
        "filters": {"space_id": "hr", "document_type": "policy", "tenant_id": "acme"},
    }
    assert vector_store.calls[1] == {
        "query": "policy",
        "k": 4,
        "filters": {"space_id": "hr", "tenant_id": "acme"},
    }
    assert result.debug["profile"] == "high_recall"
    assert result.debug["retriever_count"] == 2


def test_config_validation_rejects_unknown_retrieval_profile_settings():
    report = validate_settings(
        _settings(
            retrieval_profile="missing",
            retrieval_high_recall_top_k_multiplier=0,
            retrieval_relaxed_filter_preserve_keys=" ",
        )
    )

    issues = {(issue.field, issue.message) for issue in report.errors}
    assert ("RETRIEVAL_PROFILE", "unsupported profile: missing") in issues
    assert (
        "RETRIEVAL_HIGH_RECALL_TOP_K_MULTIPLIER",
        "must be greater than or equal to 1",
    ) in issues
    assert (
        "RETRIEVAL_RELAXED_FILTER_PRESERVE_KEYS",
        "must contain at least one filter key",
    ) in issues


def test_retrieval_exports_profile_registry_api():
    assert hasattr(retrieval, "RetrievalProfile")
    assert hasattr(retrieval, "RetrievalProfileRegistry")
    assert hasattr(retrieval, "build_default_retrieval_profile_registry")
    assert hasattr(retrieval, "build_retrievers_for_profile")


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
        "retrieval_profile": "default",
        "retrieval_high_recall_top_k_multiplier": 2,
        "retrieval_relaxed_filter_preserve_keys": "space_id,spaceId,tenant_id,tenantId",
        "query_rewriter_provider": "none",
        "reranker_provider": "none",
        "context_compressor_provider": "none",
        "context_compressor_max_characters": 1200,
        "prompt_profile": "default",
        "enabled_tools": "retrieve_knowledge,get_current_time",
        "cors_allow_origins": "http://127.0.0.1:9900",
        "cors_allow_credentials": True,
        "chunk_max_size": 800,
        "chunk_overlap": 100,
        "host": "0.0.0.0",
        "port": 9900,
        "milvus_timeout": 10000,
    }
    values.update(overrides)
    return SimpleNamespace(**values)
