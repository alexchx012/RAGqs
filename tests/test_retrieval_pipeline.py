from langchain_core.documents import Document

from app.providers import RetrievalRequest, RetrievalResult, RetrieverProvider
from app.retrieval import (
    RetrievalPipeline,
    StaticContextCompressor,
    StaticQueryRewriter,
    StaticReranker,
)


class RecordingRetriever:
    def __init__(self, name: str, documents: list[Document]):
        self.name = name
        self.documents = documents
        self.requests = []

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        self.requests.append(request)
        return RetrievalResult(
            query=request.query,
            documents=self.documents,
            debug={"provider": self.name, "top_k": request.top_k, "filters": request.filters},
        )


def test_retrieval_pipeline_rewrites_queries_combines_and_deduplicates_sources():
    primary = RecordingRetriever(
        "primary",
        [
            Document(
                page_content="original chunk",
                metadata={
                    "chunk_id": "chunk-1",
                    "document_id": "doc-1",
                    "source_path": "/docs/a.md",
                    "file_name": "a.md",
                    "heading_path": "Intro",
                    "score": 0.4,
                },
            )
        ],
    )
    secondary = RecordingRetriever(
        "secondary",
        [
            Document(page_content="duplicate chunk", metadata={"chunk_id": "chunk-1"}),
            Document(
                page_content="reranked chunk",
                metadata={
                    "chunk_id": "chunk-2",
                    "_source": "/docs/b.md",
                    "_file_name": "b.md",
                    "h1": "Graph",
                    "h2": "State",
                    "score": 0.9,
                },
            ),
        ],
    )
    pipeline = RetrievalPipeline(
        primary_retriever=primary,
        additional_retrievers=[secondary],
        query_rewriter=StaticQueryRewriter({"agent": "agent rewritten"}),
        reranker=StaticReranker(key="score", reverse=True),
        compressor=StaticContextCompressor(max_documents=2, max_characters=20),
        default_top_k=5,
    )

    result = pipeline.retrieve(
        RetrievalRequest(query="agent", filters={"space": "base"}, top_k=None)
    )

    assert isinstance(pipeline, RetrieverProvider)
    assert [request.query for request in primary.requests + secondary.requests] == [
        "agent rewritten",
        "agent rewritten",
    ]
    assert primary.requests[0].filters == {"space": "base"}
    assert result.query == "agent"
    assert result.rewritten_query == "agent rewritten"
    assert [doc.metadata["chunk_id"] for doc in result.documents] == ["chunk-2", "chunk-1"]
    assert result.documents[0].page_content == "reranked chunk"
    assert result.sources[0].source_path == "/docs/b.md"
    assert result.sources[0].file_name == "b.md"
    assert result.sources[0].heading_path == "Graph > State"
    assert result.sources[1].source_path == "/docs/a.md"
    assert result.debug["retriever_count"] == 2
    assert result.debug["deduplicated"] == 1
    assert result.debug["stages"] == ["rewrite", "retrieve", "deduplicate", "rerank", "compress", "sources"]
    assert set(result.debug["timings_ms"]) == {
        "rewrite",
        "retrieve",
        "deduplicate",
        "rerank",
        "compress",
        "sources",
        "total",
    }
    assert all(elapsed_ms >= 0 for elapsed_ms in result.debug["timings_ms"].values())


def test_retrieval_pipeline_applies_request_top_k_after_multi_retrieval():
    primary = RecordingRetriever(
        "primary",
        [
            Document(page_content="first", metadata={"chunk_id": "1", "score": 0.1}),
            Document(page_content="second", metadata={"chunk_id": "2", "score": 0.8}),
        ],
    )
    pipeline = RetrievalPipeline(
        primary_retriever=primary,
        reranker=StaticReranker(key="score", reverse=True),
        default_top_k=10,
    )

    result = pipeline.retrieve(RetrievalRequest(query="q", top_k=1))

    assert [doc.page_content for doc in result.documents] == ["second"]
    assert primary.requests[0].top_k == 1
    assert result.debug["top_k"] == 1
