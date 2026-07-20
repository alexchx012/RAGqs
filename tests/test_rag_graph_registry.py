"""Tests for the orchestration path registry, prompt_builder wiring, and agentic routing."""

from unittest.mock import Mock, patch

from langchain_core.documents import Document
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents.rag_graph import (
    NO_CONTEXT_ANSWER,
    RagGraphNodes,
    _build_answer_prompt,
    _build_bare_answer_prompt,
    build_agentic_graph,
    build_rag_graph_registry,
    route_after_agentic_tool,
    route_after_agentic_answer,
)
from app.providers.contracts import RetrievalResult, RetrievalSource


class _StubAnswerGenerator:
    def __init__(self, message):
        self._message = message
        self.received_messages = None

    def invoke_messages(self, messages, tools=None):
        self.received_messages = list(messages)
        return self._message


class _RecordingToolExecutor:
    """Fake tool executor returning pre-programmed results keyed by tool name."""

    def __init__(self, results: dict[str, dict]):
        self._results = results
        self.tools_by_name = {name: Mock() for name in results}

    def execute(self, request):
        name = request["name"]
        return self._results[name]


def _ai_message_with_tool_call(name: str, args: dict, call_id: str = "call-1"):
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id}],
    )


def test_answer_uses_build_answer_prompt_by_default():
    generator = _StubAnswerGenerator(AIMessage(content="ok"))
    nodes = RagGraphNodes(retriever_provider=Mock(), answer_generator=generator)
    state = {"question": "q", "normalized_question": "q", "retrieval_result": None, "messages": []}

    nodes.answer(state)

    human_messages = [m for m in generator.received_messages if isinstance(m, HumanMessage)]
    assert human_messages[-1].content == _build_answer_prompt(state)


def test_answer_uses_custom_prompt_builder_when_provided():
    generator = _StubAnswerGenerator(AIMessage(content="ok"))
    nodes = RagGraphNodes(retriever_provider=Mock(), answer_generator=generator)
    state = {"question": "q", "normalized_question": "q", "retrieval_result": None, "messages": []}

    def custom_prompt(_state):
        return "CUSTOM PROMPT"

    nodes.answer(state, prompt_builder=custom_prompt)

    human_messages = [m for m in generator.received_messages if isinstance(m, HumanMessage)]
    assert human_messages[-1].content == "CUSTOM PROMPT"


def test_replay_direct_answer_emits_chunked_token_events_without_live_streaming():
    generator = _StubAnswerGenerator(AIMessage(content="这是一段完整的回答内容用于测试切片回放"))
    nodes = RagGraphNodes(retriever_provider=Mock(), answer_generator=generator)
    state = {"question": "q", "normalized_question": "q", "retrieval_result": None, "messages": []}
    emitted_tokens = []

    with patch("app.agents.rag_graph._get_stream_writer_or_none") as mock_writer:
        mock_writer.return_value = lambda event: emitted_tokens.append(event)
        result = nodes.answer(state, replay_direct_answer=True)

    token_events = [e for e in emitted_tokens if e.get("type") == "token"]
    assert len(token_events) > 1, "answer content must be split into multiple replay chunks"
    assert "".join(e["data"] for e in token_events) == "这是一段完整的回答内容用于测试切片回放"
    assert result["answer"] == "这是一段完整的回答内容用于测试切片回放"


def test_replay_direct_answer_emits_nothing_when_model_calls_a_tool():
    generator = _StubAnswerGenerator(
        AIMessage(content="", tool_calls=[{"name": "search_knowledge_base", "args": {"query": "q"}, "id": "c1"}])
    )
    nodes = RagGraphNodes(retriever_provider=Mock(), answer_generator=generator)
    state = {"question": "q", "normalized_question": "q", "retrieval_result": None, "messages": []}
    emitted_tokens = []

    with patch("app.agents.rag_graph._get_stream_writer_or_none") as mock_writer:
        mock_writer.return_value = lambda event: emitted_tokens.append(event)
        nodes.answer(state, replay_direct_answer=True)

    token_events = [e for e in emitted_tokens if e.get("type") == "token"]
    assert token_events == [], (
        "a tool-calling turn must never emit any token event — the model's "
        "full message (including tool_calls) is known before any replay "
        "decision is made, so this must be zero by construction, not by luck"
    )


def test_replay_direct_answer_never_calls_stream_ai_message():
    """The whole point of buffer+replay is to never touch the live streaming
    path that has the leak window — assert invoke_messages is used instead."""
    generator = _StubAnswerGenerator(AIMessage(content="ok"))
    generator.stream_ai_message = Mock(side_effect=AssertionError("must not be called"))
    nodes = RagGraphNodes(retriever_provider=Mock(), answer_generator=generator)
    state = {"question": "q", "normalized_question": "q", "retrieval_result": None, "messages": []}

    with patch("app.agents.rag_graph._get_stream_writer_or_none") as mock_writer:
        mock_writer.return_value = lambda event: None
        nodes.answer(state, replay_direct_answer=True)

    generator.stream_ai_message.assert_not_called()


def test_search_knowledge_base_hit_writes_retrieval_result_and_round_signal():
    executor = _RecordingToolExecutor(
        {
            "search_knowledge_base": {
                "name": "search_knowledge_base",
                "output": {
                    "hit": True,
                    "documents": [{"content": "内容A", "metadata": {"_file_name": "a.md"}}],
                },
                "metadata": {},
            }
        }
    )
    nodes = RagGraphNodes(
        retriever_provider=Mock(),
        answer_generator=Mock(),
        tool_executor=executor,
        available_tools=list(executor.tools_by_name.values()),
    )
    state = {
        "messages": [_ai_message_with_tool_call("search_knowledge_base", {"query": "q"})],
        "tool_rounds": 0,
    }

    update = nodes.tool(state)

    assert update["agentic_tool_round"] == {
        "called_search_knowledge_base": True,
        "hit": True,
        "had_non_knowledge_base_tool": False,
    }
    assert update["retrieval_result"] is not None
    assert update["retrieval_result"].documents[0].page_content == "内容A"
    assert len(update["sources"]) == 1
    assert update["agentic_kb_session"] == {"called": True, "hit": True}


def test_search_knowledge_base_miss_writes_round_signal_without_forcing_stop():
    executor = _RecordingToolExecutor(
        {
            "search_knowledge_base": {
                "name": "search_knowledge_base",
                "output": {"hit": False, "documents": []},
                "metadata": {},
            }
        }
    )
    nodes = RagGraphNodes(
        retriever_provider=Mock(),
        answer_generator=Mock(),
        tool_executor=executor,
        available_tools=list(executor.tools_by_name.values()),
    )
    state = {
        "messages": [_ai_message_with_tool_call("search_knowledge_base", {"query": "q"})],
        "tool_rounds": 0,
    }

    update = nodes.tool(state)

    # Miss still means the tool was called this round; only hit is false.
    assert update["agentic_tool_round"] == {
        "called_search_knowledge_base": True,
        "hit": False,
        "had_non_knowledge_base_tool": False,
    }
    assert update["agentic_kb_session"] == {"called": True, "hit": False}


def test_search_knowledge_base_miss_alongside_successful_other_tool_does_not_block_that_result():
    """Delta spec Scenario: 'Retrieval miss alongside a successful
    non-knowledge-base tool call does not block the answer' — both tool
    results (the kb miss and the other tool's success) must survive in the
    update; the miss must not erase or shadow the other tool's ToolMessage."""
    executor = _RecordingToolExecutor(
        {
            "search_knowledge_base": {
                "name": "search_knowledge_base",
                "output": {"hit": False, "documents": []},
                "metadata": {},
            },
            "get_current_time": {
                "name": "get_current_time",
                "output": "2026-07-20T12:00:00",
                "metadata": {},
            },
        }
    )
    nodes = RagGraphNodes(
        retriever_provider=Mock(),
        answer_generator=Mock(),
        tool_executor=executor,
        available_tools=list(executor.tools_by_name.values()),
    )
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "search_knowledge_base", "args": {"query": "q"}, "id": "call-1"},
                    {"name": "get_current_time", "args": {}, "id": "call-2"},
                ],
            )
        ],
        "tool_rounds": 0,
    }

    update = nodes.tool(state)

    assert update["agentic_tool_round"] == {
        "called_search_knowledge_base": True,
        "hit": False,
        "had_non_knowledge_base_tool": True,
    }
    assert update["agentic_kb_session"] == {"called": True, "hit": False}
    tool_message_names = {message.name for message in update["messages"]}
    assert tool_message_names == {"search_knowledge_base", "get_current_time"}
    time_result_message = next(m for m in update["messages"] if m.name == "get_current_time")
    assert "2026-07-20T12:00:00" in time_result_message.content


def test_non_knowledge_base_tool_call_does_not_set_called_flag():
    executor = _RecordingToolExecutor(
        {"get_current_time": {"name": "get_current_time", "output": "12:00", "metadata": {}}}
    )
    nodes = RagGraphNodes(
        retriever_provider=Mock(),
        answer_generator=Mock(),
        tool_executor=executor,
        available_tools=list(executor.tools_by_name.values()),
    )
    state = {
        "messages": [_ai_message_with_tool_call("get_current_time", {})],
        "tool_rounds": 0,
    }

    update = nodes.tool(state)

    assert update["agentic_tool_round"] == {
        "called_search_knowledge_base": False,
        "hit": False,
        "had_non_knowledge_base_tool": True,
    }
    assert update["agentic_kb_session"] == {"called": False, "hit": False}


def test_round_signal_does_not_leak_across_rounds():
    """Round 1 misses; round 2 only calls a non-knowledge-base tool. Round 2's
    signal must not inherit round 1's miss. Durable session must still retain
    the earlier called flag for final answer_mode aggregation."""
    executor = _RecordingToolExecutor(
        {"get_current_time": {"name": "get_current_time", "output": "12:00", "metadata": {}}}
    )
    nodes = RagGraphNodes(
        retriever_provider=Mock(),
        answer_generator=Mock(),
        tool_executor=executor,
        available_tools=list(executor.tools_by_name.values()),
    )
    state_round_2 = {
        "messages": [_ai_message_with_tool_call("get_current_time", {})],
        "tool_rounds": 1,
        # Simulates leftover state from a prior round's miss, which must be overwritten
        # for routing, while the durable session carries the prior called flag.
        "agentic_tool_round": {
            "called_search_knowledge_base": True,
            "hit": False,
            "had_non_knowledge_base_tool": False,
        },
        "agentic_kb_session": {"called": True, "hit": False},
    }

    update = nodes.tool(state_round_2)

    assert update["agentic_tool_round"] == {
        "called_search_knowledge_base": False,
        "hit": False,
        "had_non_knowledge_base_tool": True,
    }
    assert update["agentic_kb_session"] == {"called": True, "hit": False}


def test_prepare_model_messages_injects_grounded_prompt_after_tool_round_with_retrieval():
    """answer_with_context runs with tool_rounds >= 1; grounded prompt must still inject."""
    generator = _StubAnswerGenerator(AIMessage(content="grounded"))
    nodes = RagGraphNodes(retriever_provider=Mock(), answer_generator=generator)
    retrieval_result = RetrievalResult(
        query="q",
        documents=[Document(page_content="内容A", metadata={"_file_name": "a.md"})],
        sources=[
            RetrievalSource(
                index=1,
                source_path="",
                file_name="a.md",
            )
        ],
    )
    state = {
        "question": "q",
        "normalized_question": "q",
        "retrieval_result": retrieval_result,
        "tool_rounds": 1,
        "messages": [
            AIMessage(
                content="",
                tool_calls=[{"name": "search_knowledge_base", "args": {"query": "q"}, "id": "call-1"}],
            ),
            ToolMessage(content="{}", tool_call_id="call-1", name="search_knowledge_base"),
        ],
    }

    nodes.answer(state, prompt_builder=_build_answer_prompt)

    human_messages = [m for m in generator.received_messages if isinstance(m, HumanMessage)]
    assert human_messages, "expected grounded HumanMessage after tool round"
    assert human_messages[-1].content == _build_answer_prompt(state)
    assert "内容A" in human_messages[-1].content


def test_build_bare_answer_prompt_does_not_mention_retrieved_context_only():
    state = {"question": "什么是报销比例", "normalized_question": "什么是报销比例"}
    prompt = _build_bare_answer_prompt(state)

    assert "search_knowledge_base" in prompt
    assert "仅使用检索上下文" not in prompt
    assert "什么是报销比例" in prompt


def test_route_after_agentic_tool_hit_goes_to_answer_with_context():
    state = {
        "agentic_tool_round": {
            "called_search_knowledge_base": True,
            "hit": True,
            "had_non_knowledge_base_tool": False,
        }
    }
    assert route_after_agentic_tool(state) == "answer_with_context"


def test_route_after_agentic_tool_pure_miss_goes_to_deterministic_no_context():
    """Pure KB miss (no other tools this round) must not re-enter model answer."""
    state = {
        "agentic_tool_round": {
            "called_search_knowledge_base": True,
            "hit": False,
            "had_non_knowledge_base_tool": False,
        }
    }
    assert route_after_agentic_tool(state) == "agentic_no_context_response"


def test_route_after_agentic_tool_miss_with_other_tool_returns_to_answer_no_context():
    """Miss + successful non-KB tool in the same round may continue the bare loop."""
    state = {
        "agentic_tool_round": {
            "called_search_knowledge_base": True,
            "hit": False,
            "had_non_knowledge_base_tool": True,
        }
    }
    assert route_after_agentic_tool(state) == "answer_no_context"


def test_route_after_agentic_tool_non_knowledge_base_call_returns_to_answer_no_context():
    state = {
        "agentic_tool_round": {
            "called_search_knowledge_base": False,
            "hit": False,
            "had_non_knowledge_base_tool": True,
        }
    }
    assert route_after_agentic_tool(state) == "answer_no_context"


def test_route_after_agentic_tool_error_goes_to_error_policy():
    state = {
        "errors": ["boom"],
        "agentic_tool_round": {
            "called_search_knowledge_base": True,
            "hit": True,
            "had_non_knowledge_base_tool": False,
        },
    }
    assert route_after_agentic_tool(state) == "error_policy"


def test_route_after_agentic_answer_tool_calls_go_to_tool():
    state = {
        "messages": [_ai_message_with_tool_call("search_knowledge_base", {"query": "q"})],
    }
    assert route_after_agentic_answer(state) == "tool"


def test_route_after_agentic_answer_plain_answer_goes_to_final_response():
    state = {"messages": [AIMessage(content="直接回答")]}
    assert route_after_agentic_answer(state) == "final_response"


class _ScriptedAnswerGenerator:
    """Returns AI messages from a fixed script, one per invoke_messages call."""

    def __init__(self, script: list):
        self._script = list(script)
        self.calls: list[list] = []

    def invoke_messages(self, messages, tools=None):
        self.calls.append(list(messages))
        return self._script.pop(0)


def test_agentic_graph_direct_answer_without_any_tool_call():
    generator = _ScriptedAnswerGenerator([AIMessage(content="直接回答")])
    graph = build_agentic_graph(
        retriever_provider=Mock(),
        answer_generator=generator,
        tool_executor=_RecordingToolExecutor({}),
        available_tools=[],
    )
    result = graph.invoke({"question": "今天天气如何", "session_id": "s1", "space_id": "default"})

    assert result["final_response"]["answer"] == "直接回答"
    assert result["final_response"]["answer_mode"] == "direct"
    assert result["final_response"]["used_tools_without_knowledge_base"] is False


def test_agentic_graph_hits_knowledge_base_and_grounds_answer():
    generator = _ScriptedAnswerGenerator(
        [
            _ai_message_with_tool_call("search_knowledge_base", {"query": "报销比例"}),
            AIMessage(content="报销比例是 80%"),
        ]
    )
    executor = _RecordingToolExecutor(
        {
            "search_knowledge_base": {
                "name": "search_knowledge_base",
                "output": {
                    "hit": True,
                    "documents": [{"content": "报销比例是 80%", "metadata": {"_file_name": "policy.md"}}],
                },
                "metadata": {},
            }
        }
    )
    graph = build_agentic_graph(
        retriever_provider=Mock(),
        answer_generator=generator,
        tool_executor=executor,
        available_tools=list(executor.tools_by_name.values()),
    )
    result = graph.invoke({"question": "报销比例是多少", "session_id": "s1", "space_id": "default"})

    assert result["final_response"]["answer"] == "报销比例是 80%"
    assert result["final_response"]["answer_mode"] == "grounded"
    assert len(result["sources"]) == 1


def test_agentic_graph_miss_with_other_tool_then_direct_answer_marks_used_tools_without_kb():
    """Same-round KB miss + non-KB tool may continue; recovered direct answer is flagged."""
    generator = _ScriptedAnswerGenerator(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "search_knowledge_base",
                        "args": {"query": "不存在的问题"},
                        "id": "call-1",
                    },
                    {"name": "get_current_time", "args": {}, "id": "call-2"},
                ],
            ),
            AIMessage(content="根据常识回答"),
        ]
    )
    executor = _RecordingToolExecutor(
        {
            "search_knowledge_base": {
                "name": "search_knowledge_base",
                "output": {"hit": False, "documents": []},
                "metadata": {},
            },
            "get_current_time": {
                "name": "get_current_time",
                "output": "2026-07-20T12:00:00",
                "metadata": {},
            },
        }
    )
    graph = build_agentic_graph(
        retriever_provider=Mock(),
        answer_generator=generator,
        tool_executor=executor,
        available_tools=list(executor.tools_by_name.values()),
    )
    result = graph.invoke({"question": "不存在的问题", "session_id": "s1", "space_id": "default"})

    assert result["final_response"]["answer"] == "根据常识回答"
    assert result["final_response"]["answer_mode"] == "direct"
    assert result["final_response"]["used_tools_without_knowledge_base"] is True
    assert len(generator.calls) == 2


def test_agentic_graph_pure_miss_ends_no_context_without_second_model_call():
    """Pure KB miss must take the deterministic no-context node: fixed text,
    answer_mode=no_context from structure (not text equality), and no second
    model answer invocation after the tool-calling turn."""
    generator = _ScriptedAnswerGenerator(
        [
            _ai_message_with_tool_call("search_knowledge_base", {"query": "不存在的问题"}),
            # Intentionally present: if the graph wrongly re-enters answer_no_context,
            # this would be consumed and the test would fail on call count / mode.
            AIMessage(content="模型不应再被调用"),
        ]
    )
    executor = _RecordingToolExecutor(
        {
            "search_knowledge_base": {
                "name": "search_knowledge_base",
                "output": {"hit": False, "documents": []},
                "metadata": {},
            }
        }
    )
    graph = build_agentic_graph(
        retriever_provider=Mock(),
        answer_generator=generator,
        tool_executor=executor,
        available_tools=list(executor.tools_by_name.values()),
    )
    result = graph.invoke({"question": "不存在的问题", "session_id": "s1", "space_id": "default"})

    assert result["final_response"]["answer"] == NO_CONTEXT_ANSWER
    assert result["final_response"]["answer_mode"] == "no_context"
    assert result["final_response"]["used_tools_without_knowledge_base"] is False
    assert len(generator.calls) == 1
    assert len(generator._script) == 1  # second scripted answer never consumed


def test_agentic_no_context_response_emits_visible_answer_content_events():
    """Pure miss stream must surface answer text via content/token events, not only done."""
    nodes = RagGraphNodes(retriever_provider=Mock(), answer_generator=Mock())
    update = nodes.agentic_no_context_response({})

    event_types = [event["type"] for event in update["events"]]
    assert "token" in event_types or "content" in event_types
    content_events = [
        event
        for event in update["events"]
        if event["type"] in {"token", "content"}
    ]
    assert any(event.get("data") == NO_CONTEXT_ANSWER for event in content_events)
    assert any(event["type"] == "done" for event in update["events"])
    assert any(
        event["type"] == "answer_mode" and event["data"]["mode"] == "no_context"
        for event in update["events"]
    )


def test_agentic_no_context_live_stream_emits_answer_text_exactly_once():
    """custom + updates consumers must not double-emit pure-miss answer text.

    agentic_no_context_response historically wrote a token via stream_writer AND
    put a token in returned events. _stream_explicit_graph yields both custom and
    updates channels, so the frontend concatenated NO_CONTEXT_ANSWER twice.
    Prefer a single emission channel (match answer nodes: live tokens via
    stream_writer only when available).
    """
    from app.services.rag_agent_service import (
        _stream_chunk_from_custom_payload,
        _stream_chunks_from_graph_update,
    )

    nodes = RagGraphNodes(retriever_provider=Mock(), answer_generator=Mock())
    custom_payloads: list[dict] = []

    with patch("app.agents.rag_graph._get_stream_writer_or_none") as mock_writer:
        mock_writer.return_value = lambda event: custom_payloads.append(event)
        update = nodes.agentic_no_context_response({})

    content_payloads: list[object] = []
    for payload in custom_payloads:
        chunk = _stream_chunk_from_custom_payload(payload)
        if chunk is not None and chunk.get("type") in {"token", "content"}:
            content_payloads.append(chunk.get("data"))
    for chunk in _stream_chunks_from_graph_update(update):
        if chunk.get("type") in {"token", "content"}:
            content_payloads.append(chunk.get("data"))

    answer_hits = [data for data in content_payloads if data == NO_CONTEXT_ANSWER]
    assert len(answer_hits) == 1

    update_chunks = _stream_chunks_from_graph_update(update)
    assert any(chunk.get("type") == "done" for chunk in update_chunks)
    assert any(
        chunk.get("type") == "answer_mode"
        and (chunk.get("data") or {}).get("mode") == "no_context"
        for chunk in update_chunks
    )


def test_agentic_graph_durable_kb_miss_survives_later_non_kb_round():
    """Round1 KB miss + non-KB tool, round2 only non-KB, then final answer:
    used_tools_without_knowledge_base must still reflect the earlier KB miss."""
    generator = _ScriptedAnswerGenerator(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "search_knowledge_base",
                        "args": {"query": "不存在的问题"},
                        "id": "call-1",
                    },
                    {"name": "get_current_time", "args": {}, "id": "call-2"},
                ],
            ),
            _ai_message_with_tool_call("get_current_time", {}, call_id="call-3"),
            AIMessage(content="结合时间的常识回答"),
        ]
    )
    executor = _RecordingToolExecutor(
        {
            "search_knowledge_base": {
                "name": "search_knowledge_base",
                "output": {"hit": False, "documents": []},
                "metadata": {},
            },
            "get_current_time": {
                "name": "get_current_time",
                "output": "2026-07-20T12:00:00",
                "metadata": {},
            },
        }
    )
    graph = build_agentic_graph(
        retriever_provider=Mock(),
        answer_generator=generator,
        tool_executor=executor,
        available_tools=list(executor.tools_by_name.values()),
    )
    result = graph.invoke({"question": "不存在的问题", "session_id": "s1", "space_id": "default"})

    assert result["final_response"]["answer"] == "结合时间的常识回答"
    assert result["final_response"]["answer_mode"] == "direct"
    assert result["final_response"]["used_tools_without_knowledge_base"] is True
    assert len(generator.calls) == 3


def test_registry_contains_baseline_and_agentic():
    registry = build_rag_graph_registry(
        retriever_provider=Mock(),
        answer_generator=Mock(),
        tool_executor=_RecordingToolExecutor({}),
        available_tools=[],
    )

    assert set(registry.keys()) == {"baseline", "agentic"}


def test_registry_supports_adding_a_future_path_without_schema_changes():
    """Delta spec 'Registry extensibility for future paths': a future path
    (e.g. corrective/graph_rag) can be registered by adding a dict entry —
    no change to KnowledgeSpace's data model or to already-registered paths
    is required. This test proves the registry is just a plain dict merge,
    so RagAgentService picking a name out of it by string key already
    supports arbitrary future path names once someone adds a builder."""
    registry = build_rag_graph_registry(
        retriever_provider=Mock(),
        answer_generator=Mock(),
        tool_executor=_RecordingToolExecutor({}),
        available_tools=[],
    )
    registry["corrective"] = registry["baseline"]  # stand-in for a future builder

    assert set(registry.keys()) == {"baseline", "agentic", "corrective"}
    # Adding the new key must not have mutated or replaced the existing entries.
    assert registry["baseline"] is not None
    assert registry["agentic"] is not None


def test_registry_baseline_graph_behaves_like_build_rag_state_graph():
    from app.agents.rag_graph import build_rag_state_graph

    generator = _ScriptedAnswerGenerator([AIMessage(content="answer")])
    registry = build_rag_graph_registry(
        retriever_provider=Mock(retrieve=Mock(return_value=RetrievalResult(query="q", documents=[]))),
        answer_generator=generator,
        tool_executor=_RecordingToolExecutor({}),
        available_tools=[],
    )
    generator_direct = _ScriptedAnswerGenerator([AIMessage(content="answer")])
    direct_graph = build_rag_state_graph(
        retriever_provider=Mock(retrieve=Mock(return_value=RetrievalResult(query="q", documents=[]))),
        answer_generator=generator_direct,
        tool_executor=_RecordingToolExecutor({}),
        available_tools=[],
    )

    from_registry = registry["baseline"].invoke(
        {"question": "q", "session_id": "s1", "space_id": "default"}
    )
    from_direct = direct_graph.invoke({"question": "q", "session_id": "s1", "space_id": "default"})

    assert from_registry["final_response"]["answer"] == from_direct["final_response"]["answer"]
    assert "answer_mode" not in from_registry["final_response"]
