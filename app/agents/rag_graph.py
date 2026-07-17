"""Explicit LangGraph RAG orchestration."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Annotated, Any, Protocol, runtime_checkable

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from app.providers import RetrievalRequest, RetrievalResult, RetrievalSource, RetrieverProvider

NO_CONTEXT_ANSWER = "知识库中没有足够依据回答这个问题。"
RESET_EVENTS_TYPE = "__reset_events__"
MAX_MODEL_TOOL_ROUNDS = 8


class RagGraphState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    tool_rounds: int
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


@dataclass
class RagGraphNodes:
    retriever_provider: RetrieverProvider
    answer_generator: AnswerGenerator
    tool_executor: ToolExecutor | None = None
    available_tools: list[Any] = field(default_factory=list)
    system_prompt: str = ""
    default_top_k: int = 3

    def normalize_input(self, state: RagGraphState) -> dict[str, Any]:
        normalized_question = state["question"].strip()
        return {
            "normalized_question": normalized_question,
            "tool_plan": {},
            "tool_request": dict(state.get("tool_request") or {}),
            "tool_result": {},
            "tool_rounds": 0,
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
                },
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
        elif not state.get("normalized_question", ""):
            decision = {"action": "handoff", "reason": "empty_question"}
        else:
            decision = {
                "action": "retrieve",
                "reason": "question_available",
            }
        return {
            "retrieval_decision": decision,
            "events": [
                {
                    "type": "retrieval_decision",
                    "node": "decide_retrieval",
                    "data": decision,
                }
            ],
        }

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
            if state.get("tool_rounds", 0) >= MAX_MODEL_TOOL_ROUNDS:
                return _error_update(
                    "answer",
                    ValueError("model tool round limit exceeded"),
                )

            prepared_messages, bootstrap_messages = self._prepare_model_messages(state)
            tools = self._resolve_available_tools()

            if hasattr(self.answer_generator, "invoke_messages"):
                stream_writer = _get_stream_writer_or_none()
                use_stream = (
                    stream_writer is not None
                    and hasattr(self.answer_generator, "stream_messages")
                    and not tools
                )
                if use_stream:
                    answer_text = _stream_answer_tokens(
                        state=state,
                        answer_generator=self.answer_generator,
                        stream_writer=stream_writer,
                        messages=prepared_messages,
                        tools=tools,
                    )
                    message = AIMessage(content=answer_text)
                else:
                    message = self.answer_generator.invoke_messages(
                        prepared_messages,
                        tools,
                    )
                    if (
                        stream_writer is not None
                        and not _has_tool_calls(message)
                        and _message_text(message)
                        and hasattr(self.answer_generator, "stream_messages")
                    ):
                        # Public token stream for final visible content only.
                        answer_text = _message_text(message)
                        stream_writer(
                            {
                                "type": "token",
                                "node": "answer",
                                "data": answer_text,
                            }
                        )
            else:
                stream_writer = _get_stream_writer_or_none()
                if stream_writer is not None and hasattr(self.answer_generator, "stream"):
                    answer_text = _stream_answer_tokens(
                        state=state,
                        answer_generator=self.answer_generator,
                        stream_writer=stream_writer,
                    )
                    return {
                        "answer": answer_text,
                        "events": [
                            {
                                "type": "answer",
                                "data": {"answer": answer_text},
                            }
                        ],
                    }
                answer_text = self.answer_generator.generate(state)
                return {
                    "answer": answer_text,
                    "events": [
                        {
                            "type": "answer",
                            "data": {"answer": answer_text},
                        }
                    ],
                }

            updates: dict[str, Any] = {
                "messages": [*bootstrap_messages, message],
            }
            if _has_tool_calls(message):
                updates["tool_rounds"] = state.get("tool_rounds", 0) + 1
                return updates

            answer_text = _message_text(message)
            updates["answer"] = answer_text
            updates["events"] = [
                {"type": "answer", "data": {"answer": answer_text}}
            ]
            return updates
        except Exception as exc:
            return _error_update("answer", exc)

    def tool(self, state: RagGraphState) -> dict[str, Any]:
        messages = list(state.get("messages") or [])
        last_message = messages[-1] if messages else None

        if last_message is not None and _has_tool_calls(last_message):
            return self._execute_model_tool_calls(state, last_message)

        return self._execute_explicit_tool_request(state)

    def _execute_model_tool_calls(
        self,
        state: RagGraphState,
        last_message: Any,
    ) -> dict[str, Any]:
        if state.get("tool_rounds", 0) > MAX_MODEL_TOOL_ROUNDS:
            return _error_update(
                "tool",
                ValueError("model tool round limit exceeded"),
            )
        try:
            if self.tool_executor is None:
                raise RuntimeError("tool executor unavailable")
            tools_by_name = self._tools_by_name()
            try:
                requests = _validated_tool_requests(last_message, tools_by_name)
            except ValueError as exc:
                return _error_update("tool", exc)

            tool_messages: list[ToolMessage] = []
            events: list[dict[str, Any]] = []
            last_result: dict[str, Any] = {}
            for request in requests:
                tool_call_event = {
                    "type": "tool_call",
                    "node": "tool",
                    "data": {
                        "name": request["name"],
                        "args": dict(request["args"]),
                    },
                }
                events.append(tool_call_event)
                raw_result = self.tool_executor.execute(
                    {"name": request["name"], "args": request["args"]}
                )
                last_result = _normalize_tool_result(
                    {"name": request["name"], "args": request["args"]},
                    raw_result,
                )
                tool_messages.append(
                    ToolMessage(
                        content=_tool_result_content(raw_result),
                        tool_call_id=request["id"],
                        name=request["name"],
                    )
                )
                events.append(
                    {
                        "type": "tool_result",
                        "node": "tool",
                        "data": last_result,
                    }
                )
            return {
                "messages": tool_messages,
                "tool_result": last_result,
                "events": events,
            }
        except Exception as exc:
            error = _error_update("tool", exc)
            return {
                "answer": "",
                "errors": error["errors"],
                "events": error["events"],
            }

    def _execute_explicit_tool_request(self, state: RagGraphState) -> dict[str, Any]:
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
        errors = list(state.get("errors", []))
        if not errors and state.get("tool_rounds", 0) >= MAX_MODEL_TOOL_ROUNDS:
            errors = ["tool: model tool round limit exceeded"]
        policy = {
            "action": "fail",
            "recoverable": False,
            "errors": errors,
        }
        return {
            "error_policy": policy,
            "errors": errors,
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
        if state.get("errors"):
            return "error_policy"
        messages = state.get("messages") or []
        if messages and _has_tool_calls(messages[-1]):
            return "tool"
        return "final_response"

    def route_after_tool(self, state: RagGraphState) -> str:
        if state.get("errors"):
            return "error_policy"
        if state.get("tool_rounds", 0) >= MAX_MODEL_TOOL_ROUNDS:
            return "error_policy"
        if state.get("tool_rounds", 0) > 0:
            return "answer"
        return "final_response"

    def _prepare_model_messages(
        self,
        state: RagGraphState,
    ) -> tuple[list[BaseMessage], list[BaseMessage]]:
        existing = list(state.get("messages") or [])
        bootstrap: list[BaseMessage] = []
        messages = list(existing)

        system_prompt = self.system_prompt or getattr(
            self.answer_generator,
            "system_prompt",
            "",
        )
        if not any(isinstance(message, SystemMessage) for message in messages):
            system_message = SystemMessage(content=str(system_prompt or ""))
            messages.append(system_message)
            bootstrap.append(system_message)

        last = messages[-1] if messages else None
        if last is None or isinstance(last, (SystemMessage, AIMessage)):
            human_message = HumanMessage(content=_build_answer_prompt(state))
            messages.append(human_message)
            bootstrap.append(human_message)

        return messages, bootstrap

    def _resolve_available_tools(self) -> list[Any]:
        if self.available_tools:
            return list(self.available_tools)
        return list(self._tools_by_name().values())

    def _tools_by_name(self) -> dict[str, Any]:
        if self.tool_executor is not None and hasattr(
            self.tool_executor,
            "tools_by_name",
        ):
            return dict(self.tool_executor.tools_by_name)
        return {
            getattr(tool, "name", getattr(tool, "__name__", "")): tool
            for tool in self.available_tools
        }


def build_rag_state_graph(
    *,
    retriever_provider: RetrieverProvider,
    answer_generator: AnswerGenerator,
    tool_executor: ToolExecutor | None = None,
    available_tools: list[Any] | None = None,
    system_prompt: str = "",
    checkpointer=None,
    default_top_k: int = 3,
):
    resolved_tools = list(available_tools or [])
    if not resolved_tools and tool_executor is not None and hasattr(
        tool_executor,
        "tools_by_name",
    ):
        resolved_tools = list(tool_executor.tools_by_name.values())
    resolved_system_prompt = system_prompt or getattr(
        answer_generator,
        "system_prompt",
        "",
    )
    nodes = RagGraphNodes(
        retriever_provider=retriever_provider,
        answer_generator=answer_generator,
        tool_executor=tool_executor,
        available_tools=resolved_tools,
        system_prompt=str(resolved_system_prompt or ""),
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
        {
            "final_response": "final_response",
            "error_policy": "error_policy",
            "tool": "tool",
        },
    )
    builder.add_conditional_edges(
        "tool",
        nodes.route_after_tool,
        {
            "final_response": "final_response",
            "error_policy": "error_policy",
            "answer": "answer",
        },
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


class ChatModelAnswerGenerator:
    """Answer generator backed by the configured chat model provider."""

    def __init__(self, chat_model_provider, system_prompt: str):
        self.chat_model_provider = chat_model_provider
        self.system_prompt = system_prompt
        self._model = None
        self._streaming_model = None

    def generate(self, state: RagGraphState) -> str:
        messages = [
            SystemMessage(content=self.system_prompt),
            HumanMessage(content=_build_answer_prompt(state)),
        ]
        return _message_text(self.invoke_messages(messages, tools=None))

    def stream(self, state: RagGraphState) -> Iterable[str]:
        messages = [
            SystemMessage(content=self.system_prompt),
            HumanMessage(content=_build_answer_prompt(state)),
        ]
        yield from self.stream_messages(messages, tools=None)

    def invoke_messages(
        self,
        messages: list[BaseMessage],
        tools: list[Any] | None = None,
    ) -> Any:
        model = self._get_model()
        if tools and hasattr(model, "bind_tools"):
            model = model.bind_tools(tools)
        return model.invoke(list(messages))

    def stream_messages(
        self,
        messages: list[BaseMessage],
        tools: list[Any] | None = None,
    ) -> Iterable[str]:
        model = self._get_streaming_model()
        if tools and hasattr(model, "bind_tools"):
            model = model.bind_tools(tools)
        if not hasattr(model, "stream"):
            text = _message_text(self.invoke_messages(messages, tools=tools))
            if text:
                yield text
            return
        for chunk in model.stream(list(messages)):
            token = _message_text(chunk)
            if token:
                yield token

    def _get_model(self):
        if self._model is None:
            self._model = self.chat_model_provider.create_chat_model(streaming=False)
        return self._model

    def _get_streaming_model(self):
        if self._streaming_model is None:
            self._streaming_model = self.chat_model_provider.create_chat_model(
                streaming=True
            )
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
    messages: list[BaseMessage] | None = None,
    tools: list[Any] | None = None,
) -> str:
    tokens: list[str] = []
    if messages is not None and hasattr(answer_generator, "stream_messages"):
        stream_iter = answer_generator.stream_messages(messages, tools)
    else:
        stream_iter = answer_generator.stream(state)
    for token in stream_iter:
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


def _has_tool_calls(message: Any) -> bool:
    tool_calls = getattr(message, "tool_calls", None) or []
    if tool_calls:
        return True
    additional_kwargs = getattr(message, "additional_kwargs", None) or {}
    if additional_kwargs.get("tool_calls") or additional_kwargs.get("deepseek_tool_calls"):
        return True
    return False


def _validated_tool_requests(
    message: Any,
    tools_by_name: dict[str, Any],
) -> list[dict[str, Any]]:
    raw_specs = _raw_tool_call_specs(message)
    if not raw_specs:
        raise ValueError("tool arguments missing: no tool calls found")

    validated: list[dict[str, Any]] = []
    for spec in raw_specs:
        name = str(spec.get("name") or "").strip()
        call_id = str(spec.get("id") or "")
        raw_arguments = spec.get("raw_arguments", "{}")
        if not name:
            raise ValueError("tool arguments invalid: tool name is required")
        if name not in tools_by_name:
            raise ValueError(f"tool arguments invalid: unknown tool {name}")

        args = _parse_tool_arguments(raw_arguments)
        _validate_tool_args_schema(tools_by_name[name], args)
        validated.append(
            {
                "id": call_id,
                "name": name,
                "args": args,
                "raw_arguments": (
                    raw_arguments
                    if isinstance(raw_arguments, str)
                    else json.dumps(raw_arguments, ensure_ascii=False)
                ),
            }
        )
    return validated


def _raw_tool_call_specs(message: Any) -> list[dict[str, Any]]:
    additional_kwargs = getattr(message, "additional_kwargs", None) or {}
    deepseek_calls = additional_kwargs.get("deepseek_tool_calls") or []
    if deepseek_calls:
        specs: list[dict[str, Any]] = []
        for call in deepseek_calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            specs.append(
                {
                    "id": call.get("id") or "",
                    "name": function.get("name") or call.get("name") or "",
                    "raw_arguments": function.get("arguments", call.get("arguments", "{}")),
                }
            )
        return specs

    specs = []
    for call in getattr(message, "tool_calls", None) or []:
        if not isinstance(call, dict):
            continue
        args = call.get("args", {})
        if isinstance(args, str):
            raw_arguments = args
        else:
            raw_arguments = json.dumps(args or {}, ensure_ascii=False)
        specs.append(
            {
                "id": call.get("id") or "",
                "name": call.get("name") or "",
                "raw_arguments": raw_arguments,
            }
        )
    if specs:
        return specs

    for call in additional_kwargs.get("tool_calls", []) or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        raw_arguments = function.get("arguments", call.get("arguments", call.get("args", "{}")))
        specs.append(
            {
                "id": call.get("id") or "",
                "name": function.get("name") or call.get("name") or "",
                "raw_arguments": raw_arguments,
            }
        )
    return specs


def _parse_tool_arguments(raw_arguments: Any) -> dict[str, Any]:
    if isinstance(raw_arguments, dict):
        return dict(raw_arguments)
    if raw_arguments is None:
        raise ValueError("tool arguments invalid: arguments are required")
    if not isinstance(raw_arguments, str):
        raise ValueError("tool arguments invalid: arguments must be a JSON object string")
    try:
        parsed = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        raise ValueError(f"tool arguments invalid JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("tool arguments invalid: arguments must be a JSON object")
    return parsed


def _validate_tool_args_schema(tool: Any, args: dict[str, Any]) -> None:
    schema = getattr(tool, "args_schema", None)
    if schema is None:
        return
    try:
        if hasattr(schema, "model_validate"):
            schema.model_validate(args)
        elif hasattr(schema, "parse_obj"):
            schema.parse_obj(args)
        elif callable(schema):
            schema(**args)
    except Exception as exc:
        raise ValueError(f"tool arguments schema validation failed: {exc}") from exc


def _tool_result_content(result: dict[str, Any]) -> str:
    output = result.get("output", "")
    if isinstance(output, str):
        return output
    try:
        return json.dumps(output, ensure_ascii=False)
    except TypeError:
        return str(output)


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
