import pytest
from langchain_core.documents import Document
from langchain_core.messages import AIMessage, ToolMessage

from app.agents import (
    AnswerGenerator,
    ChatModelAnswerGenerator,
    LangChainToolExecutor,
    ToolExecutor,
    build_rag_state_graph,
)
from app.config import Settings
from app.providers import RetrievalRequest, RetrievalResult, RetrievalSource


class RecordingRetriever:
    def __init__(self):
        self.requests = []

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        self.requests.append(request)
        return RetrievalResult(
            query=request.query,
            documents=[
                Document(
                    page_content="RAG combines retrieval and generation.",
                    metadata={"chunk_id": "chunk-1"},
                )
            ],
            sources=[
                RetrievalSource(
                    index=1,
                    source_path="/docs/rag.md",
                    file_name="rag.md",
                    heading_path="Basics",
                    chunk_id="chunk-1",
                    document_id="doc-1",
                    score=0.8,
                )
            ],
            debug={"stages": ["retrieve"], "timings_ms": {"total": 1.0}},
        )


class EmptyRetriever:
    def __init__(self):
        self.requests = []

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        self.requests.append(request)
        return RetrievalResult(
            query=request.query,
            documents=[],
            sources=[],
            debug={"stages": ["retrieve"], "returned": 0},
        )


class RecordingAnswerGenerator:
    def __init__(self):
        self.states = []

    def generate(self, state):
        self.states.append(state)
        return f"Answered: {state['normalized_question']} using {state['sources'][0]['fileName']}"


class StreamingAnswerGenerator:
    def __init__(self):
        self.stream_states = []

    def generate(self, state):
        raise AssertionError("graph.stream should use token streaming")

    def stream(self, state):
        self.stream_states.append(state)
        yield "stream "
        yield "answer"


class FailingRetriever:
    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        raise RuntimeError("vector store unavailable")


class FailingAnswerGenerator:
    def generate(self, state):
        raise RuntimeError("model provider timeout")


class NoCallAnswerGenerator:
    def generate(self, state):
        raise AssertionError("answer generator should not run without retrieved context")


class NoCallRetriever:
    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        raise AssertionError("retriever should not run for explicit tool requests")


class RecordingToolExecutor:
    def __init__(self):
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        return {
            "name": request["name"],
            "output": f"lookup:{request['args']['customer_id']}",
            "metadata": {"provider": "recording"},
        }


def test_explicit_rag_state_graph_retrieves_answers_and_records_events():
    retriever = RecordingRetriever()
    answer_generator = RecordingAnswerGenerator()
    graph = build_rag_state_graph(
        retriever_provider=retriever,
        answer_generator=answer_generator,
    )

    state = graph.invoke({"question": "  What is RAG?  ", "session_id": "s1"})

    assert isinstance(answer_generator, AnswerGenerator)
    assert retriever.requests[0].query == "What is RAG?"
    assert state["normalized_question"] == "What is RAG?"
    assert state["answer"] == "Answered: What is RAG? using rag.md"
    assert state["sources"][0]["fileName"] == "rag.md"
    assert state["retrieval_debug"]["stages"] == ["retrieve"]
    assert [event["type"] for event in state["events"]] == [
        "input",
        "retrieval_decision",
        "retrieval",
        "answer",
        "done",
    ]
    assert state["events"][1]["data"] == {
        "action": "retrieve",
        "reason": "question_available",
    }
    assert state["events"][2]["data"]["sources"][0]["fileName"] == "rag.md"
    assert state["final_response"] == {
        "answer": "Answered: What is RAG? using rag.md",
        "success": True,
        "errors": [],
    }
    assert state["events"][-1] == {
        "type": "done",
        "node": "final_response",
        "data": state["final_response"],
    }


def test_explicit_rag_state_graph_executes_explicit_tool_requests_without_retrieval():
    tool_executor = RecordingToolExecutor()
    graph = build_rag_state_graph(
        retriever_provider=NoCallRetriever(),
        answer_generator=NoCallAnswerGenerator(),
        tool_executor=tool_executor,
    )

    state = graph.invoke(
        {
            "question": "lookup customer",
            "session_id": "s1",
            "tool_request": {"name": "crm_lookup", "args": {"customer_id": "c-123"}},
        }
    )

    assert isinstance(tool_executor, ToolExecutor)
    assert tool_executor.requests == [
        {"name": "crm_lookup", "args": {"customer_id": "c-123"}}
    ]
    assert state["tool_result"] == {
        "name": "crm_lookup",
        "output": "lookup:c-123",
        "metadata": {"provider": "recording"},
    }
    assert state["answer"] == "lookup:c-123"
    assert [event["type"] for event in state["events"]] == [
        "input",
        "retrieval_decision",
        "tool_call",
        "tool_result",
        "done",
    ]
    assert state["events"][1]["data"] == {
        "action": "tool",
        "reason": "explicit_tool_request",
        "toolName": "crm_lookup",
    }
    assert state["events"][2] == {
        "type": "tool_call",
        "node": "tool",
        "data": {"name": "crm_lookup", "args": {"customer_id": "c-123"}},
    }
    assert state["final_response"] == {
        "answer": "lookup:c-123",
        "success": True,
        "errors": [],
    }


def test_langchain_tool_executor_invokes_registered_tools_by_name():
    from langchain_core.tools import tool

    @tool("crm_lookup")
    def crm_lookup(customer_id: str) -> str:
        """Look up a customer record."""
        return f"customer:{customer_id}"

    executor = LangChainToolExecutor([crm_lookup])

    result = executor.execute({"name": "crm_lookup", "args": {"customer_id": "c-123"}})

    assert result == {
        "name": "crm_lookup",
        "output": "customer:c-123",
        "metadata": {},
    }


def test_lang_chain_tool_planner_removed():
    import app.agents.rag_graph as rag_graph_module

    assert not hasattr(rag_graph_module, "LangChainToolPlanner")


def test_settings_no_longer_exposes_tool_planning_fields():
    assert "tool_planning_enabled" not in Settings.model_fields
    assert "tool_planning_excluded_tools" not in Settings.model_fields


def test_chat_model_answer_generator_streams_model_chunks():
    class FakeStreamingModel:
        def stream(self, messages):
            yield type("Chunk", (), {"content": "hello "})()
            yield type("Chunk", (), {"content": "world"})()

    class FakeChatProvider:
        def __init__(self):
            self.model = FakeStreamingModel()

        def create_chat_model(self, streaming: bool = True):
            return self.model

    generator = ChatModelAnswerGenerator(
        chat_model_provider=FakeChatProvider(),
        system_prompt="Answer with context.",
    )

    tokens = list(generator.stream({"question": "hello", "retrieval_result": None}))

    assert tokens == ["hello ", "world"]


def test_explicit_rag_state_graph_streams_answer_tokens_as_custom_events():
    answer_generator = StreamingAnswerGenerator()
    graph = build_rag_state_graph(
        retriever_provider=RecordingRetriever(),
        answer_generator=answer_generator,
    )

    chunks = list(
        graph.stream(
            {"question": "What is RAG?", "session_id": "s1"},
            stream_mode=["custom", "updates"],
        )
    )

    custom_chunks = [payload for mode, payload in chunks if mode == "custom"]
    answer_updates = [
        payload["answer"]
        for mode, payload in chunks
        if mode == "updates" and "answer" in payload
    ]
    final_updates = [
        payload["final_response"]
        for mode, payload in chunks
        if mode == "updates" and "final_response" in payload
    ]

    assert custom_chunks == [
        {"type": "token", "node": "answer", "data": "stream "},
        {"type": "token", "node": "answer", "data": "answer"},
    ]
    assert answer_updates[0]["answer"] == "stream answer"
    assert final_updates[0]["final_response"]["answer"] == "stream answer"
    assert answer_generator.stream_states[0]["sources"][0]["fileName"] == "rag.md"


def test_explicit_rag_state_graph_handoffs_when_retrieval_returns_no_context():
    retriever = EmptyRetriever()
    graph = build_rag_state_graph(
        retriever_provider=retriever,
        answer_generator=NoCallAnswerGenerator(),
    )

    state = graph.invoke({"question": "What is the private launch code?", "session_id": "s1"})

    assert state["answer"] == "知识库中没有足够依据回答这个问题。"
    assert state["sources"] == []
    assert state["retrieval_debug"] == {"stages": ["retrieve"], "returned": 0}
    assert [event["type"] for event in state["events"]] == [
        "input",
        "retrieval_decision",
        "retrieval",
        "handoff",
        "done",
    ]
    assert state["events"][-2] == {
        "type": "handoff",
        "node": "handoff",
        "data": {
            "reason": "no_retrieved_context",
            "action": "refuse",
        },
    }
    assert state["events"][-1]["type"] == "done"
    assert state["events"][-1]["data"]["success"] is True


def test_explicit_rag_state_graph_passes_knowledge_space_filter_to_retriever():
    retriever = RecordingRetriever()
    graph = build_rag_state_graph(
        retriever_provider=retriever,
        answer_generator=RecordingAnswerGenerator(),
    )

    graph.invoke({"question": "What is RAG?", "session_id": "s1", "space_id": "finance"})

    assert retriever.requests[0].filters == {"space_id": "finance"}


def test_explicit_rag_state_graph_records_retrieval_errors_and_stops():
    graph = build_rag_state_graph(
        retriever_provider=FailingRetriever(),
        answer_generator=RecordingAnswerGenerator(),
    )

    state = graph.invoke({"question": "What is RAG?", "session_id": "s1"})

    assert state["answer"] == ""
    assert state["errors"] == ["retrieve: vector store unavailable"]
    assert state["error_policy"] == {
        "action": "fail",
        "recoverable": False,
        "errors": ["retrieve: vector store unavailable"],
    }
    assert [event["type"] for event in state["events"]][-3:] == [
        "error",
        "error_policy",
        "done",
    ]
    assert state["events"][-3] == {
        "type": "error",
        "node": "retrieve",
        "data": {
            "message": "vector store unavailable",
            "recoverable": False,
        },
    }
    assert state["events"][-2] == {
        "type": "error_policy",
        "node": "error_policy",
        "data": state["error_policy"],
    }
    assert state["events"][-1] == {
        "type": "done",
        "node": "final_response",
        "data": {
            "answer": "",
            "success": False,
            "errors": ["retrieve: vector store unavailable"],
        },
    }


def test_explicit_rag_state_graph_records_answer_generation_errors():
    graph = build_rag_state_graph(
        retriever_provider=RecordingRetriever(),
        answer_generator=FailingAnswerGenerator(),
    )

    state = graph.invoke({"question": "What is RAG?", "session_id": "s1"})

    assert state["answer"] == ""
    assert state["sources"][0]["fileName"] == "rag.md"
    assert state["errors"] == ["answer: model provider timeout"]
    assert state["error_policy"] == {
        "action": "fail",
        "recoverable": False,
        "errors": ["answer: model provider timeout"],
    }
    assert [event["type"] for event in state["events"]][-3:] == [
        "error",
        "error_policy",
        "done",
    ]
    assert state["events"][-3] == {
        "type": "error",
        "node": "answer",
        "data": {
            "message": "model provider timeout",
            "recoverable": False,
        },
    }
    assert state["events"][-2] == {
        "type": "error_policy",
        "node": "error_policy",
        "data": state["error_policy"],
    }
    assert state["events"][-1] == {
        "type": "done",
        "node": "final_response",
        "data": {
            "answer": "",
            "success": False,
            "errors": ["answer: model provider timeout"],
        },
    }


def test_explicit_rag_state_graph_can_compile_with_checkpointer():
    from langgraph.checkpoint.memory import MemorySaver

    graph = build_rag_state_graph(
        retriever_provider=RecordingRetriever(),
        answer_generator=RecordingAnswerGenerator(),
        checkpointer=MemorySaver(),
    )

    state = graph.invoke(
        {"question": "checkpointed", "session_id": "s1"},
        config={"configurable": {"thread_id": "s1"}},
    )

    assert state["answer"] == "Answered: checkpointed using rag.md"


def test_explicit_rag_state_graph_resets_run_events_between_checkpointed_invocations():
    from langgraph.checkpoint.memory import MemorySaver

    graph = build_rag_state_graph(
        retriever_provider=RecordingRetriever(),
        answer_generator=RecordingAnswerGenerator(),
        checkpointer=MemorySaver(),
    )
    config = {"configurable": {"thread_id": "same-thread"}}

    graph.invoke({"question": "first", "session_id": "same-thread"}, config=config)
    state = graph.invoke({"question": "second", "session_id": "same-thread"}, config=config)

    event_types = [event["type"] for event in state["events"]]
    assert event_types == [
        "input",
        "retrieval_decision",
        "retrieval",
        "answer",
        "done",
    ]
    assert state["answer"] == "Answered: second using rag.md"


def test_explicit_rag_state_graph_resets_tool_request_between_checkpointed_invocations():
    from langgraph.checkpoint.memory import MemorySaver

    retriever = RecordingRetriever()
    tool_executor = RecordingToolExecutor()
    graph = build_rag_state_graph(
        retriever_provider=retriever,
        answer_generator=RecordingAnswerGenerator(),
        tool_executor=tool_executor,
        checkpointer=MemorySaver(),
    )
    config = {"configurable": {"thread_id": "same-tool-thread"}}

    graph.invoke(
        {
            "question": "lookup customer",
            "session_id": "same-tool-thread",
            "tool_request": {"name": "crm_lookup", "args": {"customer_id": "c-123"}},
        },
        config=config,
    )
    state = graph.invoke(
        {"question": "What is RAG?", "session_id": "same-tool-thread"},
        config=config,
    )

    assert tool_executor.requests == [
        {"name": "crm_lookup", "args": {"customer_id": "c-123"}}
    ]
    assert retriever.requests[0].query == "What is RAG?"
    assert state["answer"] == "Answered: What is RAG? using rag.md"


class ContinuationModel:
    """Records every model invocation so tests can assert tool-continuation history."""

    def __init__(self):
        self.calls = []

    def bind_tools(self, tools):
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
                                "name": "crm_lookup",
                                "arguments": '{"customer_id":"c-7"}',
                            },
                        }
                    ],
                },
                tool_calls=[
                    {
                        "name": "crm_lookup",
                        "args": {"customer_id": "c-7"},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content="customer found")


class MultiToolContinuationModel:
    def __init__(self):
        self.calls = []

    def bind_tools(self, tools):
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
                            "function": {"name": "first", "arguments": "{}"},
                        },
                        {
                            "id": "call-2",
                            "type": "function",
                            "function": {"name": "second", "arguments": "{}"},
                        },
                    ],
                },
                tool_calls=[
                    {
                        "name": "first",
                        "args": {},
                        "id": "call-1",
                        "type": "tool_call",
                    },
                    {
                        "name": "second",
                        "args": {},
                        "id": "call-2",
                        "type": "tool_call",
                    },
                ],
            )
        return AIMessage(content="final answer")


class InvalidArgsModel:
    def __init__(self, arguments: str):
        self.arguments = arguments
        self.calls = []

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        import json

        self.calls.append(messages)
        # LangChain AIMessage requires tool_calls[].args to be a dict. The raw
        # invalid payload lives in deepseek_tool_calls; Task 10 validation must
        # prefer that raw string and reject before execution.
        coerced_args: dict = {}
        try:
            parsed = json.loads(self.arguments)
            if isinstance(parsed, dict):
                coerced_args = parsed
        except json.JSONDecodeError:
            coerced_args = {}
        return AIMessage(
            content="",
            additional_kwargs={
                "reasoning_content": "private",
                "deepseek_tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "crm_lookup",
                            "arguments": self.arguments,
                        },
                    }
                ],
            },
            tool_calls=[
                {
                    "name": "crm_lookup",
                    "args": coerced_args,
                    "id": "call-1",
                    "type": "tool_call",
                }
            ],
        )


class SequenceRecordingToolExecutor:
    def __init__(self):
        self.requests = []

    def execute(self, request):
        self.requests.append(
            {"name": request["name"], "args": dict(request.get("args") or {})}
        )
        return {
            "name": request["name"],
            "output": f"result:{request['name']}",
            "metadata": {},
        }


class ContinuationChatProvider:
    def __init__(self, model):
        self.model = model

    def create_chat_model(self, streaming: bool = True):
        return self.model


def test_graph_returns_to_model_after_thinking_tool_call():
    from langchain_core.tools import tool

    model = ContinuationModel()
    executor = RecordingToolExecutor()

    @tool("crm_lookup")
    def crm_lookup(customer_id: str) -> str:
        """Look up a customer record."""
        return f"customer:{customer_id}"

    graph = build_rag_state_graph(
        retriever_provider=RecordingRetriever(),
        answer_generator=ChatModelAnswerGenerator(
            chat_model_provider=ContinuationChatProvider(model),
            system_prompt="Answer with tools when needed.",
        ),
        tool_executor=executor,
        available_tools=[crm_lookup],
    )

    state = graph.invoke(
        {"question": "lookup c-7", "session_id": "s1"},
        {"configurable": {"thread_id": "s1"}},
    )

    assert state["answer"] == "customer found"
    assert len(model.calls) == 2
    assert model.calls[1][-2].additional_kwargs["reasoning_content"] == "private"
    assert isinstance(model.calls[1][-1], ToolMessage)
    assert model.calls[1][-1].tool_call_id == "call-1"
    assert executor.requests == [
        {"name": "crm_lookup", "args": {"customer_id": "c-7"}}
    ]


def test_graph_preserves_multiple_tool_call_order():
    from langchain_core.tools import tool

    model = MultiToolContinuationModel()
    executor = SequenceRecordingToolExecutor()

    @tool("first")
    def first_tool() -> str:
        """First tool."""
        return "first-result"

    @tool("second")
    def second_tool() -> str:
        """Second tool."""
        return "second-result"

    graph = build_rag_state_graph(
        retriever_provider=RecordingRetriever(),
        answer_generator=ChatModelAnswerGenerator(
            chat_model_provider=ContinuationChatProvider(model),
            system_prompt="Answer with tools when needed.",
        ),
        tool_executor=executor,
        available_tools=[first_tool, second_tool],
    )

    state = graph.invoke(
        {"question": "run both", "session_id": "s1"},
        {"configurable": {"thread_id": "s1"}},
    )

    assert [request["name"] for request in executor.requests] == ["first", "second"]
    tool_messages = [
        message for message in model.calls[1] if isinstance(message, ToolMessage)
    ]
    assert [message.tool_call_id for message in tool_messages] == ["call-1", "call-2"]
    assert state["answer"] == "final answer"


@pytest.mark.parametrize("arguments", ["{not-json", "[]", '{"unexpected": true}'])
def test_invalid_tool_arguments_do_not_execute_tool(arguments):
    from langchain_core.tools import tool

    model = InvalidArgsModel(arguments)
    executor = SequenceRecordingToolExecutor()

    @tool("crm_lookup")
    def crm_lookup(customer_id: str) -> str:
        """Look up a customer record."""
        return f"customer:{customer_id}"

    graph = build_rag_state_graph(
        retriever_provider=RecordingRetriever(),
        answer_generator=ChatModelAnswerGenerator(
            chat_model_provider=ContinuationChatProvider(model),
            system_prompt="Answer with tools when needed.",
        ),
        tool_executor=executor,
        available_tools=[crm_lookup],
    )

    state = graph.invoke(
        {"question": "unsafe", "session_id": "s1"},
        {"configurable": {"thread_id": "s1"}},
    )

    assert executor.requests == []
    assert state["answer"] == ""
    assert any("tool arguments" in error for error in state["errors"])
    assert not any(
        isinstance(message, ToolMessage) for message in state.get("messages", [])
    )
