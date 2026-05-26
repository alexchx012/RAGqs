"""Explicit LangGraph RAG orchestration."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Annotated, Any, Protocol, runtime_checkable

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from app.providers import RetrievalRequest, RetrievalResult, RetrievalSource, RetrieverProvider

NO_CONTEXT_ANSWER = "知识库中没有足够依据回答这个问题。"
RESET_EVENTS_TYPE = "__reset_events__"


class RagGraphState(TypedDict, total=False):
    question: str
    session_id: str
    space_id: str
    tool_plan: dict[str, Any]
    tool_request: dict[str, Any]
    tool_result: dict[str, Any]
    normalized_question: str
    retrieval_decision: dict[str, Any]
    retrieval_result: RetrievalResult | None
    retrieval_debug: dict[str, Any]
    sources: list[dict[str, Any]]
    answer: str
    error_policy: dict[str, Any]
    final_response: dict[str, Any]
    events: Annotated[list[dict[str, Any]], _merge_events]
    errors: list[str]


@runtime_checkable
class AnswerGenerator(Protocol):
    def generate(self, state: RagGraphState) -> str:
        """Generate an answer from graph state."""


@runtime_checkable
class ToolExecutor(Protocol):
    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        """Execute one structured tool request."""


@runtime_checkable
class ToolPlanner(Protocol):
    def plan(self, state: RagGraphState) -> dict[str, Any]:
        """Plan whether the graph should route to a tool or retrieval."""


@dataclass
class RagGraphNodes:
    retriever_provider: RetrieverProvider
    answer_generator: AnswerGenerator
    tool_executor: ToolExecutor | None = None
    tool_planner: ToolPlanner | None = None
    default_top_k: int = 3

    def normalize_input(self, state: RagGraphState) -> dict[str, Any]:
        normalized_question = state["question"].strip()
        return {
            "normalized_question": normalized_question,
            "tool_plan": {},
            "tool_request": dict(state.get("tool_request") or {}),
            "tool_result": {},
            "retrieval_decision": {},
            "retrieval_result": None,
            "retrieval_debug": {},
            "sources": [],
            "answer": "",
            "error_policy": {},
            "final_response": {},
            "errors": [],
            "events": [
                {"type": RESET_EVENTS_TYPE},
                {
                    "type": "input",
                    "data": {
                        "question": normalized_question,
                        "sessionId": state.get("session_id", ""),
                    },
                }
            ],
        }

    def decide_retrieval(self, state: RagGraphState) -> dict[str, Any]:
        tool_request = state.get("tool_request") or {}
        if tool_request.get("name"):
            decision = {
                "action": "tool",
                "reason": "explicit_tool_request",
                "toolName": tool_request["name"],
            }
            updates = {}
        elif not state.get("normalized_question", ""):
            decision = {"action": "handoff", "reason": "empty_question"}
            updates = {}
        else:
            updates = self._plan_tool_or_retrieval(state)
            tool_plan = dict(updates.get("tool_plan") or {})
            if tool_plan.get("action") == "tool":
                planned_request = dict(tool_plan["tool_request"])
                decision = {
                    "action": "tool",
                    "reason": tool_plan.get("reason", "planner_tool_call"),
                    "toolName": planned_request["name"],
                }
                updates["tool_request"] = planned_request
            else:
                decision = {
                    "action": "retrieve",
                    "reason": tool_plan.get("reason", "question_available"),
                }
        return {
            **updates,
            "retrieval_decision": decision,
            "events": [
                {
                    "type": "retrieval_decision",
                    "node": "decide_retrieval",
                    "data": decision,
                }
            ],
        }

    def _plan_tool_or_retrieval(self, state: RagGraphState) -> dict[str, Any]:
        if self.tool_planner is None:
            return {"tool_plan": {"action": "retrieve", "reason": "question_available"}}
        try:
            tool_plan = _normalize_tool_plan(self.tool_planner.plan(state))
        except Exception as exc:
            tool_plan = {
                "action": "retrieve",
                "reason": "tool_planner_error",
                "tool_request": {},
                "error": str(exc),
            }
        return {"tool_plan": tool_plan}

    def retrieve(self, state: RagGraphState) -> dict[str, Any]:
        try:
            retrieval_result = self.retriever_provider.retrieve(
                RetrievalRequest(
                    query=state["normalized_question"],
                    top_k=self.default_top_k,
                    filters=_filters_from_space_id(state.get("space_id", "default")),
                )
            )
        except Exception as exc:
            return _error_update("retrieve", exc)
        retrieval_data = _serialize_retrieval_result(retrieval_result)
        return {
            "retrieval_result": retrieval_result,
            "retrieval_debug": retrieval_result.debug,
            "sources": retrieval_data["sources"],
            "events": [{"type": "retrieval", "data": retrieval_data}],
        }

    def handoff(self, state: RagGraphState) -> dict[str, Any]:
        reason = state.get("retrieval_decision", {}).get("reason")
        if state.get("retrieval_result") is not None:
            reason = "no_retrieved_context"
        data = {
            "reason": reason or "no_retrieved_context",
            "action": "refuse",
        }
        return {
            "answer": NO_CONTEXT_ANSWER,
            "events": [{"type": "handoff", "node": "handoff", "data": data}],
        }

    def answer(self, state: RagGraphState) -> dict[str, Any]:
        try:
            stream_writer = _get_stream_writer_or_none()
            if stream_writer is not None and hasattr(self.answer_generator, "stream"):
                answer = _stream_answer_tokens(
                    state=state,
                    answer_generator=self.answer_generator,
                    stream_writer=stream_writer,
                )
            else:
                answer = self.answer_generator.generate(state)
        except Exception as exc:
            return _error_update("answer", exc)
        return {
            "answer": answer,
            "events": [{"type": "answer", "data": {"answer": answer}}],
        }

    def tool(self, state: RagGraphState) -> dict[str, Any]:
        tool_request = dict(state.get("tool_request") or {})
        tool_call = {
            "type": "tool_call",
            "node": "tool",
            "data": {
                "name": tool_request.get("name", ""),
                "args": dict(tool_request.get("args") or {}),
            },
        }
        try:
            if self.tool_executor is None:
                raise RuntimeError("tool executor unavailable")
            tool_result = _normalize_tool_result(
                tool_request,
                self.tool_executor.execute(tool_request),
            )
        except Exception as exc:
            error = _error_update("tool", exc)
            return {
                "answer": "",
                "errors": error["errors"],
                "events": [tool_call, *error["events"]],
            }
        return {
            "tool_result": tool_result,
            "answer": str(tool_result.get("output", "")),
            "events": [
                tool_call,
                {
                    "type": "tool_result",
                    "node": "tool",
                    "data": tool_result,
                },
            ],
        }

    def error_policy(self, state: RagGraphState) -> dict[str, Any]:
        policy = {
            "action": "fail",
            "recoverable": False,
            "errors": list(state.get("errors", [])),
        }
        return {
            "error_policy": policy,
            "events": [
                {
                    "type": "error_policy",
                    "node": "error_policy",
                    "data": policy,
                }
            ],
        }

    def final_response(self, state: RagGraphState) -> dict[str, Any]:
        errors = list(state.get("errors", []))
        final_response = {
            "answer": state.get("answer", ""),
            "success": not errors,
            "errors": errors,
        }
        return {
            "final_response": final_response,
            "tool_request": {},
            "events": [
                {
                    "type": "done",
                    "node": "final_response",
                    "data": final_response,
                }
            ],
        }

    def route_after_decision(self, state: RagGraphState) -> str:
        action = state.get("retrieval_decision", {}).get("action")
        if action == "handoff":
            return "handoff"
        if action == "tool":
            return "tool"
        return "retrieve"

    def route_after_retrieval(self, state: RagGraphState) -> str:
        if state.get("errors"):
            return "error_policy"
        retrieval_result = state.get("retrieval_result")
        if retrieval_result is None or not retrieval_result.documents:
            return "handoff"
        return "answer"

    def route_after_answer(self, state: RagGraphState) -> str:
        return "error_policy" if state.get("errors") else "final_response"

    def route_after_tool(self, state: RagGraphState) -> str:
        return "error_policy" if state.get("errors") else "final_response"


def build_rag_state_graph(
    *,
    retriever_provider: RetrieverProvider,
    answer_generator: AnswerGenerator,
    tool_executor: ToolExecutor | None = None,
    tool_planner: ToolPlanner | None = None,
    checkpointer=None,
    default_top_k: int = 3,
):
    nodes = RagGraphNodes(
        retriever_provider=retriever_provider,
        answer_generator=answer_generator,
        tool_executor=tool_executor,
        tool_planner=tool_planner,
        default_top_k=default_top_k,
    )
    builder = StateGraph(RagGraphState)
    builder.add_node("normalize_input", nodes.normalize_input)
    builder.add_node("decide_retrieval", nodes.decide_retrieval)
    builder.add_node("retrieve", nodes.retrieve)
    builder.add_node("handoff", nodes.handoff)
    builder.add_node("tool", nodes.tool)
    builder.add_node("answer", nodes.answer)
    builder.add_node("error_policy", nodes.error_policy)
    builder.add_node("final_response", nodes.final_response)
    builder.add_edge(START, "normalize_input")
    builder.add_edge("normalize_input", "decide_retrieval")
    builder.add_conditional_edges(
        "decide_retrieval",
        nodes.route_after_decision,
        {"retrieve": "retrieve", "handoff": "handoff", "tool": "tool"},
    )
    builder.add_conditional_edges(
        "retrieve",
        nodes.route_after_retrieval,
        {
            "answer": "answer",
            "handoff": "handoff",
            "error_policy": "error_policy",
        },
    )
    builder.add_edge("handoff", "final_response")
    builder.add_conditional_edges(
        "answer",
        nodes.route_after_answer,
        {"final_response": "final_response", "error_policy": "error_policy"},
    )
    builder.add_conditional_edges(
        "tool",
        nodes.route_after_tool,
        {"final_response": "final_response", "error_policy": "error_policy"},
    )
    builder.add_edge("error_policy", "final_response")
    builder.add_edge("final_response", END)
    return builder.compile(checkpointer=checkpointer)


class LangChainToolExecutor:
    """Tool executor backed by a list of LangChain tools."""

    def __init__(self, tools: list[Any]):
        self.tools_by_name = {
            getattr(tool, "name", getattr(tool, "__name__", "")): tool for tool in tools
        }

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        name = str(request.get("name", "")).strip()
        if not name:
            raise ValueError("tool request name is required")
        if name not in self.tools_by_name:
            raise KeyError(f"unknown tool: {name}")
        args = dict(request.get("args") or {})
        tool = self.tools_by_name[name]
        if hasattr(tool, "invoke"):
            output = tool.invoke(args)
        else:
            output = tool(args)
        return {"name": name, "output": output, "metadata": {}}


class LangChainToolPlanner:
    """Tool planner backed by chat-model tool calling."""

    def __init__(self, chat_model_provider, tools: list[Any], system_prompt: str):
        self.chat_model_provider = chat_model_provider
        self.tools = list(tools)
        self.system_prompt = system_prompt
        self._model = None

    def plan(self, state: RagGraphState) -> dict[str, Any]:
        if not self.tools:
            return {"action": "retrieve", "reason": "no_planner_tools", "tool_request": {}}
        model = self._get_model()
        if not hasattr(model, "invoke"):
            return {"action": "retrieve", "reason": "tool_planner_unavailable", "tool_request": {}}
        response = model.invoke(
            [
                SystemMessage(content=self.system_prompt),
                HumanMessage(content=_build_tool_planning_prompt(state)),
            ]
        )
        tool_call = _first_tool_call(response)
        if tool_call is None:
            return {"action": "retrieve", "reason": "model_no_tool_call", "tool_request": {}}
        return {
            "action": "tool",
            "reason": "model_tool_call",
            "tool_request": tool_call,
        }

    def _get_model(self):
        if self._model is None:
            model = self.chat_model_provider.create_chat_model(streaming=False)
            if hasattr(model, "bind_tools"):
                model = model.bind_tools(self.tools)
            self._model = model
        return self._model


class ChatModelAnswerGenerator:
    """Answer generator backed by the configured chat model provider."""

    def __init__(self, chat_model_provider, system_prompt: str):
        self.chat_model_provider = chat_model_provider
        self.system_prompt = system_prompt
        self._model = None
        self._streaming_model = None

    def generate(self, state: RagGraphState) -> str:
        model = self._get_model()
        prompt = _build_answer_prompt(state)
        response = model.invoke(
            [
                SystemMessage(content=self.system_prompt),
                HumanMessage(content=prompt),
            ]
        )
        return _message_text(response)

    def stream(self, state: RagGraphState) -> Iterable[str]:
        model = self._get_streaming_model()
        prompt = _build_answer_prompt(state)
        messages = [
            SystemMessage(content=self.system_prompt),
            HumanMessage(content=prompt),
        ]
        if not hasattr(model, "stream"):
            yield self.generate(state)
            return
        for chunk in model.stream(messages):
            token = _message_text(chunk)
            if token:
                yield token

    def _get_model(self):
        if self._model is None:
            self._model = self.chat_model_provider.create_chat_model(streaming=False)
        return self._model

    def _get_streaming_model(self):
        if self._streaming_model is None:
            self._streaming_model = self.chat_model_provider.create_chat_model(streaming=True)
        return self._streaming_model


def _build_answer_prompt(state: RagGraphState) -> str:
    retrieval_result = state.get("retrieval_result")
    documents = retrieval_result.documents if retrieval_result else []
    context_blocks = []
    for index, document in enumerate(documents, 1):
        source = document.metadata.get("_file_name") or document.metadata.get("source") or "unknown"
        context_blocks.append(f"[{index}] source={source}\n{document.page_content}")
    context = "\n\n".join(context_blocks) if context_blocks else "No retrieved context."
    return (
        "Answer the user question using only the retrieved context.\n\n"
        f"Question: {state.get('normalized_question') or state.get('question', '')}\n\n"
        f"Retrieved context:\n{context}"
    )


def _stream_answer_tokens(
    *,
    state: RagGraphState,
    answer_generator: Any,
    stream_writer: Callable[[dict[str, Any]], None],
) -> str:
    tokens: list[str] = []
    for token in answer_generator.stream(state):
        text = str(token)
        if not text:
            continue
        tokens.append(text)
        stream_writer({"type": "token", "node": "answer", "data": text})
    return "".join(tokens)


def _get_stream_writer_or_none():
    try:
        return get_stream_writer()
    except Exception:
        return None


def _message_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    text = getattr(message, "text", None)
    if isinstance(text, str):
        return text
    if callable(text):
        return str(text())
    return "" if content is None else str(content)


def _build_tool_planning_prompt(state: RagGraphState) -> str:
    return (
        "Decide whether the user request should call one of the bound tools. "
        "If no tool is necessary, answer without tool calls so the graph can use retrieval.\n\n"
        f"User question: {state.get('normalized_question') or state.get('question', '')}"
    )


def _filters_from_space_id(space_id: str) -> dict[str, Any]:
    normalized = (space_id or "default").strip() or "default"
    return {"space_id": normalized} if normalized != "default" else {}


def _serialize_retrieval_result(result: RetrievalResult) -> dict[str, Any]:
    return {
        "query": result.query,
        "rewrittenQuery": result.rewritten_query,
        "sources": [_serialize_source(source) for source in result.sources],
        "debug": result.debug,
    }


def _serialize_source(source: RetrievalSource) -> dict[str, Any]:
    return {
        "index": source.index,
        "sourcePath": source.source_path,
        "fileName": source.file_name,
        "headingPath": source.heading_path,
        "chunkId": source.chunk_id,
        "documentId": source.document_id,
        "score": source.score,
    }


def _normalize_tool_result(
    request: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    return {
        "name": str(result.get("name") or request.get("name", "")),
        "output": str(result.get("output", "")),
        "metadata": dict(result.get("metadata") or {}),
    }


def _normalize_tool_plan(plan: dict[str, Any]) -> dict[str, Any]:
    raw_plan = dict(plan or {})
    action = str(raw_plan.get("action", "")).strip().lower()
    if action != "tool":
        return {
            "action": "retrieve",
            "reason": str(raw_plan.get("reason") or "planner_retrieve"),
            "tool_request": {},
        }

    raw_request = dict(raw_plan.get("tool_request") or {})
    name = str(raw_request.get("name") or raw_plan.get("name") or "").strip()
    if not name:
        return {
            "action": "retrieve",
            "reason": "invalid_tool_plan",
            "tool_request": {},
        }
    args = raw_request.get("args", raw_plan.get("args", {}))
    return {
        "action": "tool",
        "reason": str(raw_plan.get("reason") or "planner_tool_call"),
        "tool_request": {"name": name, "args": dict(args or {})},
    }


def _first_tool_call(response: Any) -> dict[str, Any] | None:
    for call in getattr(response, "tool_calls", None) or []:
        normalized = _normalize_model_tool_call(call)
        if normalized is not None:
            return normalized

    additional_kwargs = getattr(response, "additional_kwargs", {}) or {}
    for call in additional_kwargs.get("tool_calls", []) or []:
        normalized = _normalize_model_tool_call(call)
        if normalized is not None:
            return normalized
    return None


def _normalize_model_tool_call(call: Any) -> dict[str, Any] | None:
    if not isinstance(call, dict):
        return None

    name = call.get("name")
    args = call.get("args", {})
    function = call.get("function")
    if not name and isinstance(function, dict):
        name = function.get("name")
        args = function.get("arguments", {})

    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}

    normalized_name = str(name or "").strip()
    if not normalized_name:
        return None
    return {"name": normalized_name, "args": dict(args or {})}


def _error_update(node: str, exc: Exception) -> dict[str, Any]:
    message = str(exc)
    return {
        "answer": "",
        "errors": [f"{node}: {message}"],
        "events": [
            {
                "type": "error",
                "node": node,
                "data": {"message": message, "recoverable": False},
            }
        ],
    }


def _merge_events(
    existing: list[dict[str, Any]] | None,
    incoming: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if not incoming:
        return existing or []
    if incoming[0].get("type") == RESET_EVENTS_TYPE:
        return incoming[1:]
    return (existing or []) + incoming
