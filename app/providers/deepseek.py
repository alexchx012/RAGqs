"""DeepSeek chat model provider using OpenAI-compatible Chat Completions."""

from __future__ import annotations

from typing import Any, Callable

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from openai import OpenAI


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


def _to_ai_message(choice: Any, usage: Any | None = None) -> AIMessage:
    """Minimal non-streaming response mapping for transport contract tests."""
    message = choice.message
    content = getattr(message, "content", None) or ""
    return AIMessage(content=content)


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
        response = self.client.chat.completions.create(
            model=self.model_name,
            messages=[_serialize_deepseek_message(message) for message in messages],
            stream=False,
        )
        return ChatResult(
            generations=[
                ChatGeneration(message=_to_ai_message(response.choices[0], response.usage))
            ]
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
        client = self.client_factory(api_key=self.api_key, base_url=self.base_url)
        return DeepSeekChatModel(
            model_name=self.model_name,
            client=client,
            streaming=streaming,
        )
