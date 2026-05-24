from types import SimpleNamespace

import pytest

from app.providers import InMemorySessionStoreProvider, RetrievalRequest, RetrievalResult, RetrievalSource
from app.models.response import SessionInfoResponse
from app.services.rag_agent_service import RagAgentService


class StaticRetriever:
    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        return RetrievalResult(
            query=request.query,
            documents=[],
            sources=[
                RetrievalSource(
                    index=1,
                    source_path="/docs/session.md",
                    file_name="session.md",
                    chunk_id="chunk-session",
                    document_id="doc-session",
                    score=0.88,
                )
            ],
            debug={"stages": ["retrieve"]},
        )


class FakeAgent:
    async def ainvoke(self, input, config):
        return {"messages": [SimpleNamespace(content="stored answer")]}

    async def astream(self, input, config, stream_mode):
        class AIMessageChunk:
            content_blocks = [{"type": "text", "text": "streamed answer"}]

        yield AIMessageChunk(), {"langgraph_node": "agent"}


def fake_agent_factory(model, tools, checkpointer):
    return FakeAgent()


@pytest.mark.asyncio
async def test_traced_query_records_exchange_in_session_store():
    session_store = InMemorySessionStoreProvider()
    service = RagAgentService(
        streaming=False,
        chat_model_provider=SimpleNamespace(create_chat_model=lambda streaming: object()),
        agent_factory=fake_agent_factory,
        tools=[],
        retriever_provider=StaticRetriever(),
        session_store_provider=session_store,
        agent_runtime="legacy",
    )

    await service.query_with_trace("What is persisted?", session_id="s1")

    history = service.get_session_history("s1")
    assert [message["role"] for message in history] == ["user", "assistant"]
    assert [message["content"] for message in history] == [
        "What is persisted?",
        "stored answer",
    ]
    assert history[1]["metadata"]["sources"][0]["fileName"] == "session.md"


@pytest.mark.asyncio
async def test_streaming_query_records_completed_answer_in_session_store():
    session_store = InMemorySessionStoreProvider()
    service = RagAgentService(
        streaming=True,
        chat_model_provider=SimpleNamespace(create_chat_model=lambda streaming: object()),
        agent_factory=fake_agent_factory,
        tools=[],
        retriever_provider=StaticRetriever(),
        session_store_provider=session_store,
        agent_runtime="legacy",
    )

    chunks = [chunk async for chunk in service.query_stream_with_trace("Stream this", session_id="s1")]

    history = service.get_session_history("s1")
    assert chunks[-1] == {"type": "complete"}
    assert [message["role"] for message in history] == ["user", "assistant"]
    assert history[1]["content"] == "streamed answer"


def test_clear_session_deletes_session_store_history():
    session_store = InMemorySessionStoreProvider()
    service = RagAgentService(
        streaming=False,
        chat_model_provider=SimpleNamespace(create_chat_model=lambda streaming: object()),
        agent_factory=fake_agent_factory,
        tools=[],
        session_store_provider=session_store,
        agent_runtime="legacy",
    )
    session_store.append_message("s1", "user", "hello")

    assert service.clear_session("s1") is True
    assert service.get_session_history("s1") == []


def test_session_store_lists_summaries_newest_first_and_searches_messages():
    session_store = InMemorySessionStoreProvider()
    session_store.append_message("s1", "user", "Alpha question")
    session_store.append_message("s1", "assistant", "Alpha answer")
    session_store.append_message("s2", "user", "Beta search target")

    summaries = session_store.list_sessions()

    assert [summary.session_id for summary in summaries] == ["s2", "s1"]
    assert summaries[0].title == "Beta search target"
    assert summaries[0].message_count == 1
    assert summaries[0].last_message == "Beta search target"
    assert [summary.session_id for summary in session_store.list_sessions(query="alpha")] == ["s1"]
    assert [summary.session_id for summary in session_store.list_sessions(query="answer")] == ["s1"]
    assert session_store.list_sessions(query="missing") == []


def test_rag_agent_service_lists_session_summaries_from_store():
    session_store = InMemorySessionStoreProvider()
    service = RagAgentService(
        streaming=False,
        chat_model_provider=SimpleNamespace(create_chat_model=lambda streaming: object()),
        agent_factory=fake_agent_factory,
        tools=[],
        session_store_provider=session_store,
        agent_runtime="legacy",
    )
    session_store.append_message("s1", "user", "What is a backend session?")
    session_store.append_message("s1", "assistant", "A stored server-side conversation.")

    summaries = service.list_sessions(query="server-side")

    assert len(summaries) == 1
    assert summaries[0].session_id == "s1"
    assert summaries[0].title == "What is a backend session?"


def test_session_info_response_allows_structured_message_metadata():
    response = SessionInfoResponse(
        session_id="s1",
        message_count=1,
        history=[
            {
                "role": "assistant",
                "content": "answer",
                "metadata": {"sources": [{"fileName": "session.md"}]},
            }
        ],
    )

    assert response.history[0]["metadata"]["sources"][0]["fileName"] == "session.md"
