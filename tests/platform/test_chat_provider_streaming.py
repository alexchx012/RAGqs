"""chat provider payload 思考开关与流式路径契约（chat-generation-latency）。

思考开关：`enable_thinking` 只出现在 chat 生成 payload——quick/think 显式 false、
deep 显式 true；prompt-enhance 等非生成路径不携带该字段。
流式路径：`on_delta` 存在时请求带 `stream: true` + `stream_options.include_usage`，
SSE 段落逐段回调、末块 usage 与 chunk id 落入既有 ChatProviderResponse 契约。
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from app.chat.models import ChatProviderResponse
from app.chat.ports import ChatProviderRequest
from app.chat.prompt_enhance import DashScopePromptEnhanceProvider
from app.platform.chat_provider import DashScopeChatProvider
from app.platform.errors import PlatformError

_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class _CapturingTransport(httpx.BaseTransport):
    """捕获完整 httpx.Request 并回放固定响应。"""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._response


def _provider(transport: httpx.BaseTransport) -> DashScopeChatProvider:
    return DashScopeChatProvider(base_url=_BASE_URL, api_key="test-key", transport=transport)


def _request(
    effort: str = "quick", on_delta: Callable[[str], None] | None = None
) -> ChatProviderRequest:
    return ChatProviderRequest(
        generation_id="gen_1",
        owner_user_id="user_1",
        content="prompt",
        effort_level=effort,
        candidate=None,
        context_items=(),
        on_delta=on_delta,
    )


def _sent_payload(transport: _CapturingTransport) -> dict:
    return json.loads(transport.requests[0].content.decode("utf-8"))


def _sse_response(chunks: list[dict], *, status: int = 200) -> httpx.Response:
    lines = [f"data: {json.dumps(chunk)}" for chunk in chunks]
    body = "\n\n".join(lines) + "\n\ndata: [DONE]\n\n"
    return httpx.Response(status, content=body.encode("utf-8"))


def _stream_chunks() -> list[dict]:
    return [
        {"id": "chatcmpl-1", "choices": [{"delta": {"content": "answer "}}]},
        {"id": "chatcmpl-1", "choices": [{"delta": {"content": "for prompt"}}]},
        {
            "id": "chatcmpl-1",
            "choices": [{"delta": {}}],
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": 5,
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
        },
    ]


@pytest.mark.parametrize(
    ("effort", "expected"),
    [("quick", False), ("think", False), ("deep", True)],
)
def test_enable_thinking_follows_effort_level(effort: str, expected: bool) -> None:
    transport = _CapturingTransport(
        httpx.Response(200, json={"choices": [{"message": {"content": "a"}}]})
    )
    provider = _provider(transport)
    try:
        provider.generate(_request(effort))
    finally:
        provider.close()

    payload = _sent_payload(transport)
    assert payload["enable_thinking"] is expected
    assert "stream" not in payload


def test_prompt_enhance_does_not_carry_thinking_or_stream_fields() -> None:
    transport = _CapturingTransport(
        httpx.Response(200, json={"choices": [{"message": {"content": "better"}}]})
    )
    provider = DashScopePromptEnhanceProvider(
        base_url=_BASE_URL, api_key="k", model="qwen3.7-plus", transport=transport
    )
    try:
        assert provider.enhance("draft") == "better"
    finally:
        provider.close()

    payload = _sent_payload(transport)
    assert "enable_thinking" not in payload
    assert "stream" not in payload


def test_streaming_request_streams_and_passes_deltas_through() -> None:
    deltas: list[str] = []
    transport = _CapturingTransport(_sse_response(_stream_chunks()))
    provider = _provider(transport)
    try:
        outcome = provider.generate(_request("quick", on_delta=deltas.append))
    finally:
        provider.close()

    payload = _sent_payload(transport)
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["enable_thinking"] is False
    assert deltas == ["answer ", "for prompt"]
    assert isinstance(outcome, ChatProviderResponse)
    assert outcome.content == "answer for prompt"
    assert (outcome.input_tokens, outcome.output_tokens) == (3, 5)
    assert outcome.reasoning_tokens == 0
    # 无 x-request-id 头时回退到 chunk id（与非流式 body["id"] 回退同型）。
    assert outcome.provider_request_id == "chatcmpl-1"


def test_streaming_ignores_non_data_lines_and_empty_deltas() -> None:
    body = (
        ": keep-alive\n\n"
        "data: " + json.dumps({"id": "c1", "choices": [{"delta": {}}]}) + "\n\n"
        "data: " + json.dumps({"id": "c1", "choices": [{"delta": {"content": "ok"}}]}) + "\n\n"
        "data: [DONE]\n\n"
    )
    transport = _CapturingTransport(httpx.Response(200, content=body.encode("utf-8")))
    deltas: list[str] = []
    provider = _provider(transport)
    try:
        outcome = provider.generate(_request("quick", on_delta=deltas.append))
    finally:
        provider.close()
    assert deltas == ["ok"]
    assert outcome.content == "ok"


def test_malformed_stream_chunk_fails_unavailable() -> None:
    transport = _CapturingTransport(httpx.Response(200, content=b"data: {broken\n\n"))
    provider = _provider(transport)
    try:
        with pytest.raises(PlatformError) as raised:
            provider.generate(_request("quick", on_delta=lambda text: None))
    finally:
        provider.close()
    assert raised.value.code == "provider_unavailable"
    assert raised.value.retryable is True


def test_stream_without_content_fails_unavailable() -> None:
    transport = _CapturingTransport(_sse_response([{"id": "c1", "choices": [{"delta": {}}]}]))
    provider = _provider(transport)
    try:
        with pytest.raises(PlatformError) as raised:
            provider.generate(_request("quick", on_delta=lambda text: None))
    finally:
        provider.close()
    assert raised.value.code == "provider_unavailable"


def test_stream_http_500_maps_unavailable() -> None:
    transport = _CapturingTransport(httpx.Response(500, text="boom"))
    provider = _provider(transport)
    try:
        with pytest.raises(PlatformError) as raised:
            provider.generate(_request("quick", on_delta=lambda text: None))
    finally:
        provider.close()
    assert raised.value.code == "provider_unavailable"
    # 单 attempt：流不可重放，5xx 也不重试。
    assert len(transport.requests) == 1
