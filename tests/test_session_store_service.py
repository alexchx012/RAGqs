from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from app.models.response import SessionInfoResponse
from app.providers import (
    InMemorySessionStoreProvider,
    RetrievalRequest,
    RetrievalResult,
    RetrievalSource,
)
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


@pytest.mark.asyncio
async def test_clear_session_removes_public_history_and_private_checkpoint():
    from langgraph.checkpoint.memory import MemorySaver

    session_store = InMemorySessionStoreProvider()
    checkpointer = MemorySaver()
    service = RagAgentService(
        streaming=False,
        chat_model_provider=SessionThinkingToolProvider(),
        agent_factory=failing_session_agent_factory,
        tools=[],
        retriever_provider=SessionEmptyRetriever(),
        session_store_provider=session_store,
        checkpointer=checkpointer,
        agent_runtime="explicit_graph",
    )

    await service.query_with_trace("question", session_id="s1")

    checkpoint_cfg = service._build_runtime_config(session_id="s1", space_id="default")
    thread_id = checkpoint_cfg["configurable"]["thread_id"]
    assert thread_id == "s1"
    assert session_store.get_messages("s1")
    assert checkpointer.get(checkpoint_cfg) is not None
    assert thread_id in checkpointer.storage

    assert service.clear_session("s1") is True

    # MemorySaver.get() may re-touch defaultdict keys, so check deletion first.
    assert checkpointer.get(checkpoint_cfg) is None
    assert session_store.get_messages("s1") == []
    assert service.get_session_history("s1") == []


def test_clear_session_warns_when_checkpointer_lacks_delete_thread(monkeypatch):
    from app.services import rag_agent_service as rag_service_module

    warnings = []

    def capture_warning(message, *args, **kwargs):
        warnings.append(message.format(*args) if args else str(message))

    monkeypatch.setattr(rag_service_module.logger, "warning", capture_warning)

    session_store = InMemorySessionStoreProvider()
    session_store.append_message("s1", "user", "hello")
    service = RagAgentService(
        streaming=False,
        chat_model_provider=SimpleNamespace(create_chat_model=lambda streaming: object()),
        agent_factory=fake_agent_factory,
        tools=[],
        session_store_provider=session_store,
        checkpointer=object(),
        agent_runtime="legacy",
    )

    assert service.clear_session("s1") is True
    assert session_store.get_messages("s1") == []
    assert any("delete_thread" in message for message in warnings)


def test_session_store_lists_summaries_newest_first_and_searches_titles():
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
    # Message body is not a search target for sidebar title search
    assert session_store.list_sessions(query="answer") == []
    assert session_store.list_sessions(query="missing") == []


def test_session_store_title_search_matches_你好_and_policy_scenarios():
    session_store = InMemorySessionStoreProvider()
    for sid in ("h1", "h2", "h3"):
        session_store.append_message(sid, "user", "你好")
        session_store.append_message(sid, "assistant", "回复里提到 policy 和字母 p")
    session_store.append_message("p1", "user", "policy")
    session_store.append_message("p1", "assistant", "policy 详情")

    assert [s.session_id for s in session_store.list_sessions(query="policy")] == ["p1"]
    assert [s.session_id for s in session_store.list_sessions(query="p")] == ["p1"]
    assert sorted(s.session_id for s in session_store.list_sessions(query="你")) == [
        "h1",
        "h2",
        "h3",
    ]
    assert sorted(s.session_id for s in session_store.list_sessions(query="你好")) == [
        "h1",
        "h2",
        "h3",
    ]


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

    summaries = service.list_sessions(query="backend")

    assert len(summaries) == 1
    assert summaries[0].session_id == "s1"
    assert summaries[0].title == "What is a backend session?"


def test_rag_agent_service_filters_session_summaries_by_allowed_spaces():
    session_store = InMemorySessionStoreProvider()
    service = RagAgentService(
        streaming=False,
        chat_model_provider=SimpleNamespace(create_chat_model=lambda streaming: object()),
        agent_factory=fake_agent_factory,
        tools=[],
        session_store_provider=session_store,
        agent_runtime="legacy",
    )
    session_store.append_message("finance-session", "user", "Finance question", {"spaceId": "finance"})
    session_store.append_message("finance-session", "assistant", "Finance answer", {"spaceId": "finance"})
    session_store.append_message("hr-session", "user", "HR question", {"spaceId": "hr"})
    session_store.append_message("hr-session", "assistant", "HR answer", {"spaceId": "hr"})

    summaries = service.list_sessions(allowed_space_ids={"finance"})

    assert [summary.session_id for summary in summaries] == ["finance-session"]
    assert service.session_space_ids("finance-session") == {"finance"}
    assert service.session_space_ids("missing-session") == {"default"}


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


class SessionThinkingToolModel:
    def __init__(self):
        self.calls = []
        self.bound_tools = []

    def bind_tools(self, tools):
        self.bound_tools = tools
        return self

    def invoke(self, messages):
        self.calls.append(messages)
        if len(self.calls) == 1:
            return AIMessage(
                content="",
                additional_kwargs={
                    "reasoning_content": "private",
                    "deepseek_tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "demo_tool",
                                "arguments": "{}",
                            },
                        }
                    ],
                },
                tool_calls=[
                    {
                        "name": "demo_tool",
                        "args": {},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content="customer found")


class SessionThinkingToolProvider:
    def __init__(self):
        self.model = SessionThinkingToolModel()

    def create_chat_model(self, streaming: bool = True):
        return self.model


class SessionRecordingToolExecutor:
    def __init__(self):
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        return {"name": request["name"], "output": "tool answer", "metadata": {}}


class SessionEmptyRetriever:
    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        return RetrievalResult(
            query=request.query,
            documents=[],
            sources=[],
            debug={"stages": ["retrieve"], "returned": 0},
        )


def failing_session_agent_factory(model, tools, checkpointer):
    raise AssertionError("legacy create_agent path should not be used")


@pytest.mark.asyncio
async def test_session_store_does_not_persist_private_reasoning_metadata():
    from langchain_core.tools import tool

    @tool("demo_tool")
    def demo_tool() -> str:
        """Return a demo tool response."""
        return "demo"

    session_store = InMemorySessionStoreProvider()
    service = RagAgentService(
        streaming=False,
        chat_model_provider=SessionThinkingToolProvider(),
        agent_factory=failing_session_agent_factory,
        tools=[demo_tool],
        retriever_provider=SessionEmptyRetriever(),
        session_store_provider=session_store,
        tool_executor=SessionRecordingToolExecutor(),
        agent_runtime="explicit_graph",
    )

    await service.query_with_trace("question", session_id="s1")

    history = service.get_session_history("s1")
    stored = session_store.get_messages("s1")

    assert "reasoning_content" not in repr(history)
    assert "private" not in repr(history)
    assert "deepseek_tool_calls" not in repr(history)
    assert "reasoning_content" not in repr(stored)
    assert "private" not in repr(stored)
    assert "deepseek_tool_calls" not in repr(stored)


@pytest.mark.asyncio
async def test_session_store_public_history_contract_on_fake_path():
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

    await service.query_with_trace("persist public only", session_id="s-public")

    history = service.get_session_history("s-public")
    assert [message["content"] for message in history] == [
        "persist public only",
        "stored answer",
    ]
    assert "reasoning_content" not in repr(history)
    assert "reasoning_content" not in repr(session_store.get_messages("s-public"))
