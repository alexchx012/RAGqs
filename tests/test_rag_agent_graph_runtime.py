from types import SimpleNamespace

import pytest
from langchain_core.documents import Document
from langchain_core.messages import AIMessage

from app.extensions.tools import ToolRegistry
from app.providers import InMemorySessionStoreProvider, RetrievalResult, RetrievalSource
from app.providers.checkpoints import SQLiteCheckpointProvider
from app.services import rag_agent_service as rag_service_module
from app.services.rag_agent_service import RagAgentService


class FakeCompiledGraph:
    def __init__(self, state):
        self.state = state
        self.calls = []

    def invoke(self, input, config=None):
        self.calls.append((input, config))
        return self.state


class RecordingMetrics:
    def __init__(self):
        self.rag_queries = []

    def record_rag_query(self, **kwargs):
        self.rag_queries.append(kwargs)


def failing_agent_factory(model, tools, checkpointer):
    raise AssertionError("legacy create_agent path should not be used")


class RecordingRetriever:
    def __init__(self):
        self.requests = []

    def retrieve(self, request):
        self.requests.append(request)
        return RetrievalResult(
            query=request.query,
            documents=[Document(page_content="Finance policy content.", metadata={})],
            sources=[
                RetrievalSource(
                    index=1,
                    source_path="/docs/finance.md",
                    file_name="finance.md",
                    document_id="doc-finance",
                )
            ],
            debug={"provider": "recording"},
        )


class EmptyRetriever:
    def retrieve(self, request):
        return RetrievalResult(
            query=request.query,
            documents=[],
            sources=[],
            debug={"provider": "recording", "returned": 0},
        )


class RecordingToolExecutor:
    def __init__(self):
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        return {"name": request["name"], "output": "tool answer", "metadata": {}}


class SyncChatModel:
    def __init__(self, response: str):
        self.response = response
        self.messages = []

    def invoke(self, messages):
        self.messages.append(messages)
        return type("Message", (), {"content": self.response})()


class StreamingChatModel:
    def __init__(self):
        self.streamed_messages = []

    def invoke(self, messages):
        raise AssertionError("streaming explicit graph should use model.stream")

    def stream(self, messages):
        self.streamed_messages.append(messages)
        yield type("Chunk", (), {"content": "stream "})()
        yield type("Chunk", (), {"content": "answer"})()


class SyncChatProvider:
    def __init__(self, response: str = "graph generated answer"):
        self.model = SyncChatModel(response)

    def create_chat_model(self, streaming: bool = True):
        return self.model


class StreamingChatProvider:
    def __init__(self):
        self.model = StreamingChatModel()

    def create_chat_model(self, streaming: bool = True):
        return self.model


class ToolPlanningChatModel:
    def __init__(self):
        self.bound_tools = []

    def bind_tools(self, tools):
        self.bound_tools = tools
        return self

    def invoke(self, messages):
        return type(
            "Message",
            (),
            {
                "tool_calls": [
                    {
                        "name": "demo_tool",
                        "args": {},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ]
            },
        )()


class ToolPlanningChatProvider:
    def __init__(self):
        self.model = ToolPlanningChatModel()

    def create_chat_model(self, streaming: bool = True):
        return self.model


class ExplodingChatProvider:
    def create_chat_model(self, streaming: bool = True):
        raise AssertionError("chat model should not be called without retrieved context")


@pytest.mark.asyncio
async def test_rag_agent_service_query_with_trace_can_use_explicit_graph_runtime():
    graph = FakeCompiledGraph(
        {
            "answer": "graph answer",
            "sources": [{"index": 1, "fileName": "graph.md"}],
            "retrieval_debug": {"stages": ["retrieve"]},
            "events": [{"type": "retrieval", "data": {"sources": [{"fileName": "graph.md"}]}}],
        }
    )
    service = RagAgentService(
        streaming=False,
        agent_factory=failing_agent_factory,
        tools=[],
        use_explicit_graph=True,
        explicit_graph=graph,
    )

    result = await service.query_with_trace("What is graph RAG?", session_id="s1")

    assert graph.calls[0][0] == {
        "question": "What is graph RAG?",
        "session_id": "s1",
        "space_id": "default",
    }
    assert graph.calls[0][1]["configurable"] == {"thread_id": "s1"}
    assert result == {
        "answer": "graph answer",
        "sources": [{"index": 1, "fileName": "graph.md"}],
        "retrieval": {
            "query": "What is graph RAG?",
            "rewrittenQuery": None,
            "sources": [{"index": 1, "fileName": "graph.md"}],
            "debug": {"stages": ["retrieve"]},
        },
    }


@pytest.mark.asyncio
async def test_rag_agent_service_query_with_trace_records_runtime_metrics():
    graph = FakeCompiledGraph(
        {
            "answer": "graph answer",
            "sources": [{"index": 1, "fileName": "graph.md"}],
            "retrieval_debug": {"stages": ["retrieve"]},
            "tokenUsage": {"promptTokens": 7, "completionTokens": 5, "totalTokens": 12},
            "events": [],
        }
    )
    metrics = RecordingMetrics()
    ticks = iter([10.0, 10.25])
    service = RagAgentService(
        streaming=False,
        agent_factory=failing_agent_factory,
        tools=[],
        use_explicit_graph=True,
        explicit_graph=graph,
        metrics_collector=metrics,
        metrics_clock=lambda: next(ticks),
    )

    await service.query_with_trace("What is graph RAG?", session_id="s1", space_id="finance")

    assert metrics.rag_queries == [
        {
            "session_id": "s1",
            "space_id": "finance",
            "success": True,
            "latency_ms": 250.0,
            "token_usage": {"promptTokens": 7, "completionTokens": 5, "totalTokens": 12},
        }
    ]


@pytest.mark.asyncio
async def test_rag_agent_service_query_with_trace_preserves_explicit_graph_failures():
    graph = FakeCompiledGraph(
        {
            "answer": "",
            "sources": [],
            "retrieval_debug": {},
            "final_response": {
                "answer": "",
                "success": False,
                "errors": ["retrieve: vector store unavailable"],
            },
            "events": [],
        }
    )
    metrics = RecordingMetrics()
    ticks = iter([10.0, 10.1])
    service = RagAgentService(
        streaming=False,
        agent_factory=failing_agent_factory,
        tools=[],
        use_explicit_graph=True,
        explicit_graph=graph,
        metrics_collector=metrics,
        metrics_clock=lambda: next(ticks),
    )

    result = await service.query_with_trace("What is graph RAG?", session_id="s1")

    assert result["success"] is False
    assert result["errors"] == ["retrieve: vector store unavailable"]
    assert metrics.rag_queries[0]["success"] is False


@pytest.mark.asyncio
async def test_rag_agent_service_attaches_trace_metadata_to_explicit_graph_config(monkeypatch):
    monkeypatch.setattr(rag_service_module, "get_current_trace_id", lambda: "trace-graph")
    graph = FakeCompiledGraph(
        {
            "answer": "graph answer",
            "sources": [],
            "retrieval_debug": {},
            "events": [],
        }
    )
    service = RagAgentService(
        streaming=False,
        agent_factory=failing_agent_factory,
        tools=[],
        use_explicit_graph=True,
        explicit_graph=graph,
        prompt_profile="strict",
    )

    await service.query_with_trace("What is graph RAG?", session_id="s1", space_id="finance")

    config = graph.calls[0][1]
    assert config["configurable"] == {"thread_id": "s1"}
    assert config["metadata"] == {
        "traceId": "trace-graph",
        "sessionId": "s1",
        "spaceId": "finance",
        "agentRuntime": "explicit_graph",
        "promptProfile": "strict",
    }
    assert "ragqs" in config["tags"]
    assert "runtime:explicit_graph" in config["tags"]
    assert "space:finance" in config["tags"]


@pytest.mark.asyncio
async def test_rag_agent_service_builds_default_explicit_graph_runtime():
    retriever = RecordingRetriever()
    chat_provider = SyncChatProvider()
    session_store = InMemorySessionStoreProvider()
    service = RagAgentService(
        streaming=False,
        chat_model_provider=chat_provider,
        agent_factory=failing_agent_factory,
        tools=[],
        retriever_provider=retriever,
        session_store_provider=session_store,
        agent_runtime="explicit_graph",
    )

    result = await service.query_with_trace("What is policy?", session_id="s1", space_id="finance")

    assert result["answer"] == "graph generated answer"
    assert retriever.requests[0].filters == {"space_id": "finance"}
    assert "Finance policy content." in chat_provider.model.messages[0][-1].content
    assert [message.role for message in session_store.get_messages("s1")] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_rag_agent_service_can_run_explicit_graph_with_sqlite_checkpoints(tmp_path):
    retriever = RecordingRetriever()
    chat_provider = SyncChatProvider()
    checkpoint_provider = SQLiteCheckpointProvider(str(tmp_path / "graph-checkpoints.db"))
    service = RagAgentService(
        streaming=False,
        chat_model_provider=chat_provider,
        agent_factory=failing_agent_factory,
        tools=[],
        retriever_provider=retriever,
        session_store_provider=InMemorySessionStoreProvider(),
        checkpoint_provider=checkpoint_provider,
        agent_runtime="explicit_graph",
    )

    result = await service.query_with_trace("What is policy?", session_id="s-sqlite")

    assert result["answer"] == "graph generated answer"
    checkpoint = service.checkpointer.get({"configurable": {"thread_id": "s-sqlite"}})
    assert checkpoint is not None
    checkpoint_provider.close()


@pytest.mark.asyncio
async def test_rag_agent_service_refuses_without_calling_model_when_graph_retrieval_is_empty():
    service = RagAgentService(
        streaming=False,
        chat_model_provider=ExplodingChatProvider(),
        agent_factory=failing_agent_factory,
        tools=[],
        retriever_provider=EmptyRetriever(),
        session_store_provider=InMemorySessionStoreProvider(),
        agent_runtime="explicit_graph",
    )

    result = await service.query_with_trace("What is the private launch code?", session_id="s1")

    assert result["answer"] == "知识库中没有足够依据回答这个问题。"
    assert result["sources"] == []
    assert result["retrieval"]["debug"]["returned"] == 0


@pytest.mark.asyncio
async def test_rag_agent_service_query_stream_with_trace_maps_graph_events():
    graph = FakeCompiledGraph(
        {
            "answer": "final graph answer",
            "sources": [{"index": 1, "fileName": "graph.md"}],
            "retrieval_debug": {"stages": ["retrieve"]},
            "events": [
                {"type": "retrieval", "data": {"sources": [{"fileName": "graph.md"}]}},
                {"type": "token", "data": "stream token", "node": "answer"},
                {"type": "source", "data": {"fileName": "graph.md"}},
            ],
        }
    )
    service = RagAgentService(
        streaming=True,
        agent_factory=failing_agent_factory,
        tools=[],
        use_explicit_graph=True,
        explicit_graph=graph,
    )

    chunks = [chunk async for chunk in service.query_stream_with_trace("question", session_id="s1")]

    assert chunks == [
        {"type": "retrieval", "data": {"sources": [{"fileName": "graph.md"}]}},
        {"type": "token", "data": "stream token", "node": "answer"},
        {"type": "source", "data": {"fileName": "graph.md"}},
        {"type": "done", "data": {"answer": "final graph answer"}},
    ]


@pytest.mark.asyncio
async def test_rag_agent_service_streams_graph_owned_done_event_once():
    service = RagAgentService(
        streaming=True,
        chat_model_provider=SyncChatProvider("stream graph answer"),
        agent_factory=failing_agent_factory,
        tools=[],
        retriever_provider=RecordingRetriever(),
        session_store_provider=InMemorySessionStoreProvider(),
        agent_runtime="explicit_graph",
    )

    chunks = [
        chunk async for chunk in service.query_stream_with_trace("What is policy?", session_id="s1")
    ]
    done_chunks = [chunk for chunk in chunks if chunk["type"] == "done"]

    assert done_chunks == [
        {
            "type": "done",
            "data": {
                "answer": "stream graph answer",
                "success": True,
                "errors": [],
            },
            "node": "final_response",
        }
    ]


@pytest.mark.asyncio
async def test_rag_agent_service_streams_explicit_graph_tokens_before_done():
    chat_provider = StreamingChatProvider()
    service = RagAgentService(
        streaming=True,
        chat_model_provider=chat_provider,
        agent_factory=failing_agent_factory,
        tools=[],
        retriever_provider=RecordingRetriever(),
        session_store_provider=InMemorySessionStoreProvider(),
        agent_runtime="explicit_graph",
    )

    chunks = [
        chunk async for chunk in service.query_stream_with_trace("What is policy?", session_id="s1")
    ]

    assert {"type": "token", "data": "stream ", "node": "answer"} in chunks
    assert {"type": "token", "data": "answer", "node": "answer"} in chunks
    assert chunks[-1] == {
        "type": "done",
        "data": {
            "answer": "stream answer",
            "success": True,
            "errors": [],
        },
        "node": "final_response",
    }
    assert chat_provider.model.streamed_messages


def test_rag_agent_service_builds_explicit_graph_with_injected_tool_executor():
    tool_executor = RecordingToolExecutor()
    service = RagAgentService(
        streaming=False,
        chat_model_provider=SyncChatProvider(),
        agent_factory=failing_agent_factory,
        tools=[],
        retriever_provider=RecordingRetriever(),
        session_store_provider=InMemorySessionStoreProvider(),
        tool_executor=tool_executor,
        agent_runtime="explicit_graph",
    )
    graph = service._build_default_explicit_graph()

    state = graph.invoke(
        {
            "question": "run tool",
            "session_id": "s-tool",
            "tool_request": {"name": "demo_tool", "args": {}},
        },
        {"configurable": {"thread_id": "s-tool"}},
    )

    assert tool_executor.requests == [{"name": "demo_tool", "args": {}}]
    assert state["answer"] == "tool answer"


def test_rag_agent_service_prefers_grouped_agent_and_rag_settings():
    from langchain_core.tools import tool

    @tool("demo_tool")
    def demo_tool() -> str:
        """Return a demo tool response."""
        return "demo"

    @tool("excluded_tool")
    def excluded_tool() -> str:
        """Return an excluded tool response."""
        return "excluded"

    registry = ToolRegistry()
    registry.register(demo_tool)
    registry.register(excluded_tool)
    settings = SimpleNamespace(
        rag=SimpleNamespace(top_k=7, model="group-model"),
        agent=SimpleNamespace(
            runtime="explicit_graph",
            enabled_tools="demo_tool,excluded_tool",
            prompt_profile="strict",
        ),
        chat_model="group-model",
    )

    service = RagAgentService(
        settings=settings,
        streaming=False,
        chat_model_provider=SyncChatProvider(),
        agent_factory=failing_agent_factory,
        checkpointer=object(),
        tool_registry=registry,
    )

    assert service.model_name == "group-model"
    assert service.prompt_profile == "strict"
    assert service.retrieval_top_k == 7
    assert service.agent_runtime == "explicit_graph"
    assert service.use_explicit_graph is True
    assert [tool.name for tool in service.tools] == ["demo_tool", "excluded_tool"]
    assert not hasattr(service, "tool_planner")
    assert not hasattr(service, "tool_planning_enabled")


@pytest.mark.asyncio
async def test_rag_agent_service_query_uses_explicit_graph_answer_when_enabled():
    graph = FakeCompiledGraph({"answer": "plain graph answer", "events": []})
    service = RagAgentService(
        streaming=False,
        agent_factory=failing_agent_factory,
        tools=[],
        use_explicit_graph=True,
        explicit_graph=graph,
    )

    answer = await service.query("question", session_id="s1")

    assert answer == "plain graph answer"


class ThinkingToolChatModel:
    """Emits private reasoning + a tool call on the first turn, then a public answer."""

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


class ThinkingToolChatProvider:
    def __init__(self):
        self.model = ThinkingToolChatModel()

    def create_chat_model(self, streaming: bool = True):
        return self.model


@pytest.mark.asyncio
async def test_public_trace_and_session_metadata_do_not_contain_reasoning():
    from langchain_core.tools import tool

    @tool("demo_tool")
    def demo_tool() -> str:
        """Return a demo tool response."""
        return "demo"

    chat_provider = ThinkingToolChatProvider()
    tool_executor = RecordingToolExecutor()
    session_store = InMemorySessionStoreProvider()
    service = RagAgentService(
        streaming=False,
        chat_model_provider=chat_provider,
        agent_factory=failing_agent_factory,
        tools=[demo_tool],
        retriever_provider=EmptyRetriever(),
        session_store_provider=session_store,
        tool_executor=tool_executor,
        agent_runtime="explicit_graph",
    )

    result = await service.query_with_trace("question", session_id="s1")

    assert "private" not in repr(result)
    assert "reasoning_content" not in repr(result)
    assert "reasoning_content" not in repr(service.get_session_history("s1"))
    assert "private" not in repr(service.get_session_history("s1"))
    assert "deepseek_tool_calls" not in repr(service.get_session_history("s1"))
