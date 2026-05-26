from types import SimpleNamespace

import pytest

from app.api import chat as chat_api
from app.models.request import ChatRequest
from app.providers import RetrievalRequest, RetrievalResult, RetrievalSource
from app.services import rag_agent_service as rag_service_module
from app.services.rag_agent_service import RagAgentService


class StaticRetriever:
    def __init__(self):
        self.requests = []

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        self.requests.append(request)
        return RetrievalResult(
            query=request.query,
            rewritten_query=f"{request.query} rewritten",
            documents=[],
            sources=[
                RetrievalSource(
                    index=1,
                    source_path="/docs/guide.md",
                    file_name="guide.md",
                    heading_path="Intro",
                    chunk_id="chunk-1",
                    document_id="doc-1",
                    score=0.91,
                )
            ],
            debug={"stages": ["rewrite", "retrieve", "sources"], "timings_ms": {"total": 1.2}},
        )


class FakeAgent:
    async def ainvoke(self, input, config):
        return {"messages": [SimpleNamespace(content="answer with sources")]}

    async def astream(self, input, config, stream_mode):
        class AIMessageChunk:
            content_blocks = [{"type": "text", "text": "streamed answer"}]

        yield AIMessageChunk(), {"langgraph_node": "agent"}


class ConfigCapturingAgent:
    def __init__(self):
        self.configs = []

    async def ainvoke(self, input, config):
        self.configs.append(config)
        return {"messages": [SimpleNamespace(content="answer with sources")]}


def fake_agent_factory(model, tools, checkpointer):
    return FakeAgent()


@pytest.mark.asyncio
async def test_rag_agent_service_query_with_trace_returns_answer_sources_and_debug():
    retriever = StaticRetriever()
    service = RagAgentService(
        streaming=False,
        chat_model_provider=SimpleNamespace(create_chat_model=lambda streaming: object()),
        agent_factory=fake_agent_factory,
        tools=[],
        retriever_provider=retriever,
        retrieval_top_k=4,
        agent_runtime="legacy",
    )

    result = await service.query_with_trace("what is rag", session_id="s1")

    assert result["answer"] == "answer with sources"
    assert retriever.requests[0].query == "what is rag"
    assert retriever.requests[0].top_k == 4
    assert result["retrieval"]["query"] == "what is rag"
    assert result["retrieval"]["rewrittenQuery"] == "what is rag rewritten"
    assert result["sources"] == [
        {
            "index": 1,
            "sourcePath": "/docs/guide.md",
            "fileName": "guide.md",
            "headingPath": "Intro",
            "chunkId": "chunk-1",
            "documentId": "doc-1",
            "score": 0.91,
        }
    ]
    assert result["retrieval"]["debug"]["stages"] == ["rewrite", "retrieve", "sources"]


@pytest.mark.asyncio
async def test_rag_agent_service_attaches_trace_metadata_to_legacy_agent_config(monkeypatch):
    monkeypatch.setattr(rag_service_module, "get_current_trace_id", lambda: "trace-legacy")
    agent = ConfigCapturingAgent()

    def agent_factory(model, tools, checkpointer):
        return agent

    service = RagAgentService(
        streaming=False,
        chat_model_provider=SimpleNamespace(create_chat_model=lambda streaming: object()),
        agent_factory=agent_factory,
        tools=[],
        retriever_provider=StaticRetriever(),
        retrieval_top_k=4,
        agent_runtime="legacy",
        prompt_profile="concise",
    )

    await service.query_with_trace("what is rag", session_id="s1", space_id="finance")

    assert agent.configs[0]["configurable"] == {"thread_id": "s1"}
    assert agent.configs[0]["metadata"] == {
        "traceId": "trace-legacy",
        "sessionId": "s1",
        "spaceId": "finance",
        "agentRuntime": "legacy",
        "promptProfile": "concise",
    }
    assert "ragqs" in agent.configs[0]["tags"]
    assert "runtime:legacy" in agent.configs[0]["tags"]
    assert "space:finance" in agent.configs[0]["tags"]


@pytest.mark.asyncio
async def test_rag_agent_service_query_stream_with_trace_emits_retrieval_first():
    service = RagAgentService(
        streaming=True,
        chat_model_provider=SimpleNamespace(create_chat_model=lambda streaming: object()),
        agent_factory=fake_agent_factory,
        tools=[],
        retriever_provider=StaticRetriever(),
        agent_runtime="legacy",
    )

    chunks = [chunk async for chunk in service.query_stream_with_trace("what is rag", session_id="s1")]

    assert chunks[0]["type"] == "retrieval"
    assert chunks[0]["data"]["sources"][0]["fileName"] == "guide.md"
    assert chunks[1] == {"type": "content", "data": "streamed answer", "node": "agent"}
    assert chunks[-1] == {"type": "complete"}


@pytest.mark.asyncio
async def test_chat_api_returns_sources_and_retrieval_debug(monkeypatch):
    class FakeRagService:
        async def query_with_trace(self, question: str, session_id: str):
            return {
                "answer": "api answer",
                "sources": [{"index": 1, "fileName": "guide.md"}],
                "retrieval": {"debug": {"stages": ["retrieve"]}},
            }

    monkeypatch.setattr(chat_api, "rag_agent_service", FakeRagService())

    response = await chat_api.chat(ChatRequest(Id="s1", Question="question"))

    assert response["data"]["success"] is True
    assert response["data"]["answer"] == "api answer"
    assert response["data"]["sources"] == [{"index": 1, "fileName": "guide.md"}]
    assert response["data"]["retrievalDebug"] == {"stages": ["retrieve"]}


def test_chat_stream_chunk_formatter_preserves_structured_error_data():
    payload = chat_api.format_stream_chunk(
        {
            "type": "error",
            "node": "retrieve",
            "data": {"message": "vector store unavailable", "recoverable": False},
        }
    )

    assert payload == {
        "type": "error",
        "data": {"message": "vector store unavailable", "recoverable": False},
        "node": "retrieve",
    }


def test_chat_stream_chunk_formatter_maps_token_to_content():
    payload = chat_api.format_stream_chunk(
        {"type": "token", "node": "answer", "data": "partial answer"}
    )

    assert payload == {"type": "content", "data": "partial answer", "node": "answer"}


def test_chat_stream_chunk_formatter_maps_graph_routing_events():
    assert chat_api.format_stream_chunk(
        {
            "type": "retrieval_decision",
            "data": {"action": "retrieve", "reason": "question_available"},
            "node": "decide_retrieval",
        }
    ) == {
        "type": "retrieval_decision",
        "data": {"action": "retrieve", "reason": "question_available"},
        "node": "decide_retrieval",
    }
    assert chat_api.format_stream_chunk(
        {
            "type": "handoff",
            "data": {"reason": "no_retrieved_context", "action": "refuse"},
            "node": "handoff",
        }
    ) == {
        "type": "handoff",
        "data": {"reason": "no_retrieved_context", "action": "refuse"},
        "node": "handoff",
    }


def test_chat_stream_chunk_formatter_maps_error_policy_events():
    assert chat_api.format_stream_chunk(
        {
            "type": "error_policy",
            "data": {
                "action": "fail",
                "recoverable": False,
                "errors": ["retrieve: vector store unavailable"],
            },
            "node": "error_policy",
        }
    ) == {
        "type": "error_policy",
        "data": {
            "action": "fail",
            "recoverable": False,
            "errors": ["retrieve: vector store unavailable"],
        },
        "node": "error_policy",
    }


def test_chat_stream_chunk_formatter_maps_tool_result_events():
    assert chat_api.format_stream_chunk(
        {
            "type": "tool_result",
            "data": {
                "name": "crm_lookup",
                "output": "customer:c-123",
                "metadata": {"provider": "crm"},
            },
            "node": "tool",
        }
    ) == {
        "type": "tool_result",
        "data": {
            "name": "crm_lookup",
            "output": "customer:c-123",
            "metadata": {"provider": "crm"},
        },
        "node": "tool",
    }


@pytest.mark.asyncio
async def test_chat_sessions_api_returns_searchable_session_summaries(monkeypatch):
    class FakeRagService:
        def list_sessions(self, query: str | None = None):
            assert query == "rag"
            return [
                SimpleNamespace(
                    session_id="s1",
                    title="RAG architecture",
                    message_count=2,
                    updated_at="2026-05-24T12:00:00+00:00",
                    last_message="Use a retriever provider.",
                )
            ]

    monkeypatch.setattr(chat_api, "rag_agent_service", FakeRagService())

    response = await chat_api.list_sessions(query="rag")

    assert response["code"] == 200
    assert response["data"]["count"] == 1
    assert response["data"]["sessions"] == [
        {
            "id": "s1",
            "title": "RAG architecture",
            "messageCount": 2,
            "updatedAt": "2026-05-24T12:00:00+00:00",
            "lastMessage": "Use a retriever provider.",
        }
    ]
