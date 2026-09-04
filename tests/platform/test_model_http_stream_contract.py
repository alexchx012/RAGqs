"""流式出网（model_http_stream_post）契约：单 attempt、失败分类、熔断记账。

流式响应体不可重放——与 ``model_http_post`` 的内核重试语义不同，这里钉死：
可重试 5xx 也不重试（单次物理发送）；``on_data`` 抛出的 ``ProviderFailure``
按单次失败记账；过期 deadline 不产生物理请求；熔断由同一内核记账。
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from app.platform.model_http import ModelHttpError, model_http_stream_post
from app.platform.provider import CircuitBreakerRegistry, CircuitOpen

_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _stream_post(
    transport: httpx.BaseTransport,
    on_data,
    *,
    timeout_seconds: float = 60.0,
    circuits: CircuitBreakerRegistry | None = None,
):
    return model_http_stream_post(
        provider="dashscope",
        operation="chat.generate",
        url=_URL,
        headers={"Authorization": "Bearer k"},
        payload={"model": "qwen3.7-plus"},
        timeout_seconds=timeout_seconds,
        on_data=on_data,
        transport=transport,
        circuits=circuits,
        now=lambda: _NOW,
    )


def test_stream_retryable_5xx_is_sent_exactly_once() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, text="down")

    with pytest.raises(ModelHttpError) as raised:
        _stream_post(httpx.MockTransport(handler), lambda data: None)

    assert raised.value.error_class == "http_503"
    assert raised.value.attempts == 1
    assert len(requests) == 1


def test_stream_success_invokes_on_data_per_sse_data_line() -> None:
    body = (": keep-alive\n\n" "data: " + '{"choices": []}' + "\n\n" "data: [DONE]\n\n").encode(
        "utf-8"
    )
    seen: list[str] = []
    response = _stream_post(
        httpx.MockTransport(lambda request: httpx.Response(200, content=body)),
        seen.append,
    )

    assert seen == ['{"choices": []}', "[DONE]"]
    assert response.status_code == 200
    assert response.data_count == 2
    assert response.provider_call_id.startswith("mhr_dashscope_chat.generate_")
    assert response.provider_request_id is None


def test_stream_on_data_rejection_accounts_single_failure() -> None:
    from app.platform.provider import ProviderFailure

    def on_data(data: str) -> None:
        raise ProviderFailure("invalid_response_body", retryable=False, sent=True)

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"data: broken\n\n")
    )
    with pytest.raises(ModelHttpError) as raised:
        _stream_post(transport, on_data)

    assert raised.value.error_class == "invalid_response_body"
    assert raised.value.attempts == 1


def test_stream_expired_deadline_does_not_send() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"data: {}\n\n")

    with pytest.raises(ModelHttpError) as raised:
        _stream_post(httpx.MockTransport(handler), lambda data: None, timeout_seconds=0.0)

    assert raised.value.timeout is True
    assert requests == []


def test_stream_failures_record_into_the_shared_circuit() -> None:
    circuits = CircuitBreakerRegistry(threshold=2, open_seconds=60)
    transport = httpx.MockTransport(lambda request: httpx.Response(503, text="down"))
    for _ in range(2):
        with pytest.raises(ModelHttpError):
            _stream_post(transport, lambda data: None, circuits=circuits)

    with pytest.raises(CircuitOpen):
        _stream_post(transport, lambda data: None, circuits=circuits)
