"""Opt-in real DeepSeek Chat Completions smoke tests.

These tests hit the live DeepSeek API only when both are set:
- DEEPSEEK_SMOKE=1
- a non-placeholder DEEPSEEK_API_KEY

Without the opt-in gate they must SKIP (never PASS). Output and failures must not
print API keys or other secret-bearing material.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from openai import OpenAI

from app.providers.deepseek import DeepSeekChatModelProvider
from app.providers.selection import is_valid_secret
from app.tools.time_tool import get_current_time

pytestmark = pytest.mark.skipif(
    os.getenv("DEEPSEEK_SMOKE") != "1"
    or not is_valid_secret(os.getenv("DEEPSEEK_API_KEY", "")),
    reason="DeepSeek smoke requires DEEPSEEK_SMOKE=1 and a valid DEEPSEEK_API_KEY",
)


class _RecordingCompletions:
    def __init__(self, inner: Any):
        self._inner = inner
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._inner.create(**kwargs)


class _RecordingClient:
    """Thin OpenAI wrapper that records chat.completions.create payloads."""

    def __init__(self, **kwargs: Any):
        inner = OpenAI(**kwargs)
        self.chat = SimpleNamespace(completions=_RecordingCompletions(inner.chat.completions))

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.chat.completions.calls


def _provider(
    *,
    client_factory: Any = OpenAI,
) -> DeepSeekChatModelProvider:
    return DeepSeekChatModelProvider(
        api_key=os.environ["DEEPSEEK_API_KEY"],
        model_name=os.getenv("CHAT_MODEL", "deepseek-v4-pro"),
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        client_factory=client_factory,
    )


def test_real_deepseek_non_streaming_text():
    answer = (
        _provider()
        .create_chat_model(streaming=False)
        .invoke([HumanMessage(content="Reply with exactly: smoke-ok")])
    )
    assert str(answer.content).strip()


def test_real_deepseek_streaming_text():
    chunks = list(
        _provider()
        .create_chat_model(streaming=True)
        .stream([HumanMessage(content="Say hello briefly")])
    )
    assert "".join(str(chunk.content) for chunk in chunks).strip()


def test_real_deepseek_thinking_tool_call_continuation():
    """thinking + tool-call: second adapter request keeps full protocol chain.

    Uses the zero-side-effect ``get_current_time`` tool only. Never runs business
    write tools. First turn forces a tool call; second turn asks only for a final
    public answer from the tool result (no re-forced tool_choice / prompt).

    Thinking is enabled via DeepSeek ``extra_body={"thinking": {"type": "enabled"}}``
    so the live path can exercise reasoning_content when the model returns it.
    ``reasoning_content`` preservation is asserted only when the first turn actually
    produced it — never invent a PASS for missing reasoning. Unit tests still cover
    the full reasoning_content history serialization contract.
    """

    # DeepSeek-only request option; adapter forwards extra_body into Chat Completions.
    thinking_kwargs = {"extra_body": {"thinking": {"type": "enabled"}}}

    provider = _provider(client_factory=_RecordingClient)
    model = provider.create_chat_model(streaming=False)
    bound = model.bind_tools(
        [get_current_time],
        tool_choice={
            "type": "function",
            "function": {"name": "get_current_time"},
        },
        **thinking_kwargs,
    )

    first_human = HumanMessage(
        content=(
            "What is the current time in Asia/Shanghai? "
            "You must call the get_current_time tool."
        )
    )
    first = bound.invoke([first_human])

    tool_calls = list(getattr(first, "tool_calls", None) or [])
    assert tool_calls, "expected model to emit a tool call for get_current_time"
    call = tool_calls[0]
    assert call.get("name") == "get_current_time"
    call_id = call.get("id")
    assert call_id

    args = call.get("args") or {}
    if not isinstance(args, dict):
        args = {}
    tool_output = get_current_time.invoke(args)
    tool_message = ToolMessage(
        content=str(tool_output),
        tool_call_id=str(call_id),
        name="get_current_time",
    )

    # Second turn: no forced tool_choice and no "must call tool" prompt — ask only
    # for a final public answer based on the already-provided tool result.
    followup = model.bind_tools([get_current_time], **thinking_kwargs).invoke(
        [
            HumanMessage(
                content=(
                    "What is the current time in Asia/Shanghai? "
                    "The get_current_time tool result is already in the messages. "
                    "Give a brief final public answer based on that result only. "
                    "Do not call any tool again."
                )
            ),
            first if isinstance(first, AIMessage) else AIMessage(content=str(first.content)),
            tool_message,
        ]
    )

    recorder = model.client
    assert isinstance(recorder, _RecordingClient)
    assert len(recorder.calls) >= 2, "expected two Chat Completions requests"

    # Prefer observing thinking enablement on the first request when the SDK surfaces it.
    first_request = recorder.calls[0]
    first_extra = first_request.get("extra_body") or {}
    if first_extra:
        assert first_extra.get("thinking") == {"type": "enabled"}

    second_request = recorder.calls[1]
    roles = [item.get("role") for item in second_request.get("messages", [])]
    assert "assistant" in roles
    assert "tool" in roles

    assistant_msgs = [
        item for item in second_request["messages"] if item.get("role") == "assistant"
    ]
    assert assistant_msgs, "second request must include assistant tool-call history"
    assistant_with_tools = next(
        (item for item in assistant_msgs if item.get("tool_calls")),
        None,
    )
    assert assistant_with_tools is not None, "assistant history must carry tool_calls"
    assert assistant_with_tools["tool_calls"][0]["function"]["name"] == "get_current_time"
    # Live thinking may still omit reasoning_content depending on model/route.
    # Only require history preservation when the first turn actually returned it.
    if first.additional_kwargs.get("reasoning_content") is not None:
        assert "reasoning_content" in assistant_with_tools

    tool_msgs = [item for item in second_request["messages"] if item.get("role") == "tool"]
    assert tool_msgs, "second request must include matching ToolMessage"
    assert any(item.get("tool_call_id") == call_id for item in tool_msgs)

    assert str(followup.content).strip(), "final public answer must be non-empty"
