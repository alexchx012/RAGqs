"""DeepSeek chat model provider using OpenAI-compatible Chat Completions."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any, Callable

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from openai import OpenAI

from app.providers.selection import is_valid_secret


class DeepSeekProviderError(RuntimeError):
    """Normalized DeepSeek upstream failure without secret-bearing details."""

    def __init__(self, category: str, message: str | None = None):
        self.category = category
        text = message if message is not None else category
        super().__init__(text)


def _text_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(content)


def _serialize_assistant_message(message: AIMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "role": "assistant",
        "content": _text_content(message.content),
    }
    additional = message.additional_kwargs or {}
    tool_calls = additional.get("deepseek_tool_calls")
    reasoning_content = additional.get("reasoning_content")

    if tool_calls:
        # Tool-call history must preserve reasoning and raw tool call arguments.
        if reasoning_content is not None:
            payload["reasoning_content"] = reasoning_content
        payload["tool_calls"] = tool_calls
    elif reasoning_content is not None:
        # History assistant reasoning without tool calls may be omitted per protocol;
        # still allow explicit presence when provided without tool calls.
        payload["reasoning_content"] = reasoning_content

    return payload


def _serialize_deepseek_message(message: BaseMessage) -> dict[str, Any]:
    if isinstance(message, SystemMessage):
        return {"role": "system", "content": _text_content(message.content)}
    if isinstance(message, HumanMessage):
        return {"role": "user", "content": _text_content(message.content)}
    if isinstance(message, ToolMessage):
        return {
            "role": "tool",
            "content": _text_content(message.content),
            "tool_call_id": message.tool_call_id,
        }
    if isinstance(message, AIMessage):
        return _serialize_assistant_message(message)
    raise TypeError(f"unsupported DeepSeek message: {type(message).__name__}")


def _tool_call_to_dict(call: Any) -> dict[str, Any]:
    if isinstance(call, dict):
        function = call.get("function") or {}
        if not isinstance(function, dict):
            function = {
                "name": getattr(function, "name", None),
                "arguments": getattr(function, "arguments", "") or "",
            }
        return {
            "id": call.get("id"),
            "type": call.get("type") or "function",
            "function": {
                "name": function.get("name"),
                "arguments": function.get("arguments") or "",
            },
        }

    function = getattr(call, "function", None)
    return {
        "id": getattr(call, "id", None),
        "type": getattr(call, "type", None) or "function",
        "function": {
            "name": getattr(function, "name", None) if function is not None else None,
            "arguments": (
                getattr(function, "arguments", "") if function is not None else ""
            )
            or "",
        },
    }


def _langchain_tool_calls(raw_tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for call in raw_tool_calls:
        function = call.get("function") or {}
        arguments = function.get("arguments") or ""
        name = function.get("name")
        call_id = call.get("id")
        if not name or call_id is None:
            continue
        try:
            parsed = json.loads(arguments) if arguments else {}
            if not isinstance(parsed, dict):
                continue
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        result.append(
            {
                "name": name,
                "args": parsed,
                "id": call_id,
                "type": "tool_call",
            }
        )
    return result


def _invalid_langchain_tool_calls(
    raw_tool_calls: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for call in raw_tool_calls:
        function = call.get("function") or {}
        arguments = function.get("arguments") or ""
        name = function.get("name")
        call_id = call.get("id")
        if not name or call_id is None:
            result.append(
                {
                    "name": name or "",
                    "args": arguments if isinstance(arguments, str) else str(arguments),
                    "id": call_id,
                    "error": "incomplete tool call",
                    "type": "invalid_tool_call",
                }
            )
            continue
        try:
            parsed = json.loads(arguments) if arguments else {}
            if not isinstance(parsed, dict):
                raise ValueError("tool call arguments must be a JSON object")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            result.append(
                {
                    "name": name,
                    "args": arguments if isinstance(arguments, str) else str(arguments),
                    "id": call_id,
                    "error": str(exc),
                    "type": "invalid_tool_call",
                }
            )
    return result


def _usage_metadata(usage: Any | None) -> dict[str, int] | None:
    if usage is None:
        return None
    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    total = getattr(usage, "total_tokens", None)
    if prompt is None and completion is None and total is None:
        return None
    return {
        "input_tokens": int(prompt or 0),
        "output_tokens": int(completion or 0),
        "total_tokens": int(total or 0),
    }


def _to_ai_message(choice: Any, usage: Any | None = None) -> AIMessage:
    message = choice.message
    raw_tool_calls = [
        _tool_call_to_dict(call) for call in (getattr(message, "tool_calls", None) or [])
    ]
    return AIMessage(
        content=getattr(message, "content", None) or "",
        additional_kwargs={
            "reasoning_content": getattr(message, "reasoning_content", None),
            "deepseek_tool_calls": raw_tool_calls,
        },
        tool_calls=_langchain_tool_calls(raw_tool_calls),
        invalid_tool_calls=_invalid_langchain_tool_calls(raw_tool_calls),
        response_metadata={"finish_reason": getattr(choice, "finish_reason", None)},
        usage_metadata=_usage_metadata(usage),
    )


_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"\bds-[A-Za-z0-9_\-]{4,}\b"),
    re.compile(r"\bBearer\s+\S+", re.IGNORECASE),
    re.compile(r"(?i)(api[_-]?key|token|secret|authorization)\s*[:=]\s*\S+"),
)


def _sanitize_error_text(text: str) -> str:
    sanitized = text
    for pattern in _SECRET_PATTERNS:
        sanitized = pattern.sub("[redacted]", sanitized)
    return sanitized


def _request_options(*, stop: list[str] | None, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Forward bind()/invoke() options that Chat Completions understands."""

    options: dict[str, Any] = {}
    if stop:
        options["stop"] = stop

    for key in (
        "tools",
        "tool_choice",
        "temperature",
        "top_p",
        "max_tokens",
        "presence_penalty",
        "frequency_penalty",
        "response_format",
        "user",
        "seed",
        "logit_bias",
        "logprobs",
        "top_logprobs",
        "n",
        "parallel_tool_calls",
    ):
        if key in kwargs and kwargs[key] is not None:
            options[key] = kwargs[key]
    return options


def _classify_error(exc: BaseException) -> DeepSeekProviderError:
    if isinstance(exc, DeepSeekProviderError):
        return exc

    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)

    raw = str(exc)
    lowered = raw.lower()

    category: str | None = None
    if status == 401 or status == 403 or "401" in lowered or "authentication" in lowered or "invalid key" in lowered or "unauthorized" in lowered:
        category = "authentication"
    elif status == 429 or "rate limit" in lowered or "too many requests" in lowered:
        category = "rate_limit"
    elif status == 400 or "invalid parameter" in lowered or "invalid_request" in lowered or "bad request" in lowered:
        category = "invalid_request"
    elif status == 404 or "not found" in lowered or "resource" in lowered and "not" in lowered:
        category = "resource"
    elif status is not None and int(status) >= 500:
        category = "server"
    elif "server" in lowered or "internal error" in lowered or "503" in lowered or "500" in lowered:
        category = "server"
    elif "resource" in lowered or "insufficient" in lowered or "quota" in lowered:
        category = "resource"
    else:
        category = "upstream"

    # Message must not leak secrets or raw secret-bearing bodies.
    safe_message = f"DeepSeek {category} error"
    return DeepSeekProviderError(category, safe_message)


class DeepSeekChatModel(BaseChatModel):
    """Controlled BaseChatModel adapter for DeepSeek Chat Completions."""

    model_name: str
    client: Any
    streaming: bool = False

    @property
    def _llm_type(self) -> str:
        return "deepseek-chat-completions"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        try:
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=[_serialize_deepseek_message(message) for message in messages],
                stream=False,
                **_request_options(stop=stop, kwargs=kwargs),
            )
        except Exception as exc:  # noqa: BLE001 - normalize all transport failures
            raise _classify_error(exc) from None

        return ChatResult(
            generations=[
                ChatGeneration(message=_to_ai_message(response.choices[0], response.usage))
            ]
        )

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        try:
            stream = self.client.chat.completions.create(
                model=self.model_name,
                messages=[_serialize_deepseek_message(message) for message in messages],
                stream=True,
                **_request_options(stop=stop, kwargs=kwargs),
            )
        except Exception as exc:  # noqa: BLE001 - normalize all transport failures
            raise _classify_error(exc) from None

        try:
            for chunk in stream:
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    # usage-only or empty chunks produce no client tokens
                    continue
                choice = choices[0]
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue
                content = getattr(delta, "content", None)
                if not content:
                    # empty content deltas do not produce client tokens
                    continue
                yield ChatGenerationChunk(message=AIMessageChunk(content=content))
        except Exception as exc:  # noqa: BLE001 - normalize mid-stream failures
            raise _classify_error(exc) from None

    def bind_tools(
        self,
        tools: list[Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Any:
        return self.bind(
            tools=[convert_to_openai_tool(tool) for tool in tools],
            tool_choice=tool_choice,
            **kwargs,
        )


class DeepSeekChatModelProvider:
    """Chat provider that constructs DeepSeekChatModel instances."""

    def __init__(
        self,
        api_key: str,
        model_name: str,
        base_url: str = "https://api.deepseek.com",
        client_factory: Callable[..., Any] = OpenAI,
    ):
        self.api_key = api_key
        self.model_name = model_name
        self.base_url = base_url
        self.client_factory = client_factory

    def create_chat_model(self, streaming: bool = True) -> DeepSeekChatModel:
        if not is_valid_secret(self.api_key):
            raise ValueError("请设置环境变量 DEEPSEEK_API_KEY")

        client = self.client_factory(api_key=self.api_key, base_url=self.base_url)
        return DeepSeekChatModel(
            model_name=self.model_name,
            client=client,
            streaming=streaming,
        )
