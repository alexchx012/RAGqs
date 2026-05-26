from types import SimpleNamespace

from langchain_core.documents import Document

from app.providers import FakeRetrieverProvider, FakeVectorStoreProvider, RetrievalResult
from app.tools.knowledge_tool import (
    enforce_knowledge_space,
    retrieve_knowledge_with_provider,
    retrieve_knowledge_with_settings,
)


def test_retrieve_knowledge_with_provider_uses_structured_retriever_result():
    vector_store = FakeVectorStoreProvider()
    vector_store.add_documents(
        [
            Document(
                page_content="LangGraph supports stateful agent graphs.",
                metadata={"_source": "docs/graph.md", "_file_name": "graph.md", "h1": "Graph"},
            )
        ]
    )
    retriever = FakeRetrieverProvider(vector_store)

    context, docs = retrieve_knowledge_with_provider(
        query="stateful",
        retriever_provider=retriever,
        top_k=4,
    )

    assert len(docs) == 1
    assert "Graph" in context
    assert "graph.md" in context
    assert "LangGraph supports stateful agent graphs." in context


def test_retrieve_knowledge_enforces_request_scoped_space_over_tool_argument():
    class RecordingRetriever:
        def __init__(self):
            self.requests = []

        def retrieve(self, request):
            self.requests.append(request)
            return RetrievalResult(query=request.query, documents=[])

    retriever = RecordingRetriever()

    retrieve_knowledge_with_provider(
        query="policy",
        retriever_provider=retriever,
        top_k=3,
        space_id="sales",
    )
    with enforce_knowledge_space("finance"):
        retrieve_knowledge_with_provider(
            query="policy",
            retriever_provider=retriever,
            top_k=3,
            space_id="sales",
        )
    retrieve_knowledge_with_provider(
        query="policy",
        retriever_provider=retriever,
        top_k=3,
        space_id="sales",
    )

    assert [request.filters for request in retriever.requests] == [
        {"space_id": "sales"},
        {"space_id": "finance"},
        {"space_id": "sales"},
    ]


def test_retrieve_knowledge_with_settings_prefers_grouped_rag_top_k():
    class RecordingRetriever:
        def __init__(self):
            self.requests = []

        def retrieve(self, request):
            self.requests.append(request)
            return RetrievalResult(query=request.query, documents=[])

    settings = SimpleNamespace(rag=SimpleNamespace(top_k=9), rag_top_k=3)
    retriever = RecordingRetriever()

    retrieve_knowledge_with_settings(
        query="policy",
        retriever_provider=retriever,
        settings=settings,
    )

    assert retriever.requests[0].top_k == 9
