"""model_http 请求构造合同测试（A1）与出网 4xx 诊断日志测试（A2）。

A1: 经 DashScopeChatProvider.generate → model_http_post → ModelHttpTransport
    发出的物理请求与同一 client/headers/payload/URL 的裸 httpx 调用逐项一致；
    共享 client 分支与临时 client 分支都覆盖。
A2: 不可重试的物理 4xx/5xx 在抛 ProviderFailure 前记录 provider/operation/
    status/响应体摘要；可重试状态与 2xx 不产生该日志；错误码映射不回退。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import httpx
import pytest

from app.chat.models import ChatProviderResponse
from app.chat.ports import ChatProviderRequest
from app.platform.chat_provider import DashScopeChatProvider
from app.platform.errors import PlatformError
from app.platform.model_http import ModelHttpError, model_http_post

_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_CHAT_URL = f"{_BASE_URL}/chat/completions"
_PAYLOAD = {
    "model": "qwen3.7-plus",
    "messages": [{"role": "user", "content": "prompt"}],
}
_HEADERS = {"Authorization": "Bearer test-key"}
_OK_BODY = {
    "choices": [{"message": {"content": "answer"}}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 5},
}


class _RequestCapturingTransport(httpx.BaseTransport):
    """捕获完整 httpx.Request 并回放固定响应。"""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._response


def _provider(transport: httpx.BaseTransport) -> DashScopeChatProvider:
    return DashScopeChatProvider(
        base_url=_BASE_URL,
        api_key="test-key",
        transport=transport,
    )


def _provider_request() -> ChatProviderRequest:
    return ChatProviderRequest(
        generation_id="gen_1",
        owner_user_id="user_1",
        content="prompt",
        effort_level="think",
        candidate=1,
        context_items=(),
    )


def _normalized(request: httpx.Request) -> tuple[str, str, dict[str, str], str]:
    return (
        request.method,
        str(request.url),
        {key.lower(): value for key, value in request.headers.items()},
        request.content.decode("utf-8"),
    )


def test_shared_client_branch_matches_bare_httpx() -> None:
    """A1: 共享 client 分支（DashScopeChatProvider 持有 client）与裸调用一致。

    provider 路径的载荷由 assemble_generation_prompt 组装；裸参照取 provider
    实际发出的同一 JSON 载荷回放，验证 transport 对调用方载荷零改写。
    """

    via_provider = _RequestCapturingTransport(httpx.Response(200, json=_OK_BODY))
    provider = _provider(via_provider)
    try:
        outcome = provider.generate(_provider_request())
    finally:
        provider.close()
    assert isinstance(outcome, ChatProviderResponse)

    sent_payload = json.loads(via_provider.requests[0].content.decode("utf-8"))
    bare_transport = _RequestCapturingTransport(httpx.Response(200, json=_OK_BODY))
    with httpx.Client(
        base_url=_BASE_URL.rstrip("/"),
        headers=_HEADERS,
        timeout=120.0,
        transport=bare_transport,
    ) as client:
        client.request("POST", _CHAT_URL, json=sent_payload)

    assert len(via_provider.requests) == 1
    assert len(bare_transport.requests) == 1
    assert _normalized(via_provider.requests[0]) == _normalized(bare_transport.requests[0])
    # 请求语义钉死：POST 绝对 URL（单斜杠路径）、Bearer 认证、组装后载荷原样发送。
    method, url, headers, body = _normalized(via_provider.requests[0])
    assert (method, url) == ("POST", _CHAT_URL)
    assert "//" not in url.removeprefix("https://")
    assert headers["authorization"] == "Bearer test-key"
    assert json.loads(body) == sent_payload
    assert sent_payload["model"] == "qwen3.7-plus"
    assert sent_payload["messages"][0]["role"] == "user"


def test_prompt_enhance_builds_single_slash_url() -> None:
    """A1: prompt_enhance 同型拼接回归——httpx 尾斜杠 base_url 不得产出 `//`。"""

    from app.chat.prompt_enhance import DashScopePromptEnhanceProvider

    transport = _RequestCapturingTransport(
        httpx.Response(
            200,
            json={"choices": [{"message": {"content": "better prompt"}}]},
        )
    )
    provider = DashScopePromptEnhanceProvider(
        base_url=_BASE_URL,
        api_key="test-key",
        model="qwen3.7-plus",
        transport=transport,
    )
    try:
        assert provider.enhance("prompt") == "better prompt"
    finally:
        provider.close()

    assert len(transport.requests) == 1
    assert str(transport.requests[0].url) == _CHAT_URL


def test_temporary_client_branch_matches_bare_httpx() -> None:
    """A1: 临时 client 分支（model_http_post 不传 client）与裸调用一致。"""

    via_transport = _RequestCapturingTransport(httpx.Response(200, json={"ok": True}))
    model_http_post(
        provider="dashscope",
        operation="chat.generate",
        url=_CHAT_URL,
        headers=_HEADERS,
        payload=dict(_PAYLOAD),
        timeout_seconds=60.0,
        transport=via_transport,
        now=lambda: datetime(2026, 9, 4, tzinfo=UTC),
    )

    bare_transport = _RequestCapturingTransport(httpx.Response(200, json={"ok": True}))
    with httpx.Client(transport=bare_transport, timeout=60.0) as client:
        client.request("POST", _CHAT_URL, headers=dict(_HEADERS), json=dict(_PAYLOAD))

    assert len(via_transport.requests) == 1
    assert _normalized(via_transport.requests[0]) == _normalized(bare_transport.requests[0])


def test_non_retryable_http_failure_logs_provider_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A2: 404 终态失败记录 provider/operation/status/响应体原文；映射不变。"""

    transport = _RequestCapturingTransport(
        httpx.Response(
            404,
            json={"error": {"code": "NotFound", "message": "model not found"}},
        )
    )
    provider = _provider(transport)
    with caplog.at_level(logging.WARNING, logger="app.platform.model_http"):
        with pytest.raises(PlatformError) as raised:
            provider.generate(_provider_request())
    provider.close()

    assert raised.value.code == "provider_unavailable"
    failure_records = [
        record
        for record in caplog.records
        if record.name == "app.platform.model_http" and "model http failure" in record.message
    ]
    assert len(failure_records) == 1
    message = failure_records[0].getMessage()
    assert "provider=dashscope" in message
    assert "operation=chat.generate" in message
    assert "status=404" in message
    assert "model not found" in message


def test_retryable_and_success_paths_do_not_log_failure_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A2: 200 与可重试状态不产生诊断日志（避免重试序列刷屏）。"""

    transport = _RequestCapturingTransport(httpx.Response(200, json=_OK_BODY))
    provider = _provider(transport)
    with caplog.at_level(logging.WARNING, logger="app.platform.model_http"):
        provider.generate(_provider_request())
    provider.close()
    assert not [r for r in caplog.records if "model http failure" in r.message]

    retry_transport = _RequestCapturingTransport(httpx.Response(503, text="down"))
    with caplog.at_level(logging.WARNING, logger="app.platform.model_http"):
        with pytest.raises(ModelHttpError) as raised:
            model_http_post(
                provider="dashscope",
                operation="chat.generate",
                url=_CHAT_URL,
                headers=_HEADERS,
                payload=dict(_PAYLOAD),
                timeout_seconds=60.0,
                transport=retry_transport,
                now=lambda: datetime(2026, 9, 4, tzinfo=UTC),
            )
    assert raised.value.error_class == "http_503"
    assert not [r for r in caplog.records if "model http failure" in r.message]
