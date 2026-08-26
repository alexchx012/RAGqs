"""`POST /v1/prompt-enhancements` 契约与 DashScope 适配器错误分类测试（prompt-enhance）。

路由测试沿用本包 conftest 的 build_test_env（sqlite 临时库 + build_runtime 注入
fake + TestClient + provision_and_login）；错误 envelope 形状对齐
tests/api_v1/test_quota_routes.py 的 assert_error_shape（error 对象精确 4 键）。
适配器测试经 httpx.MockTransport 注入传输层，覆盖 429/5xx/网络/超时/畸形响应的
错误映射与不泄露上游响应体。
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.chat.ports import UnavailablePromptEnhanceProviderPort
from app.chat.prompt_enhance import ENHANCE_SYSTEM_PROMPT, DashScopePromptEnhanceProvider
from app.platform.app_factory import create_platform_app
from app.platform.errors import PlatformError
from app.platform.runtime import build_runtime

from .conftest import NullObjectStore, build_test_env, provision_and_login


def assert_error_shape(response, status: int, code: str) -> dict:
    """钉住平台完整 error shape：error 对象精确 4 键 + request_id 前缀。"""
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"error"}
    error = body["error"]
    assert set(error) == {"code", "message", "details", "request_id"}
    assert error["code"] == code
    assert isinstance(error["message"], str) and error["message"]
    assert isinstance(error["details"], dict)
    assert isinstance(error["request_id"], str) and error["request_id"].startswith("req_")
    return error


def _login(env, username: str = "alice") -> dict[str, str]:
    token, _ = provision_and_login(env["identity"], username)
    return {"Authorization": f"Bearer {token}"}


def test_enhance_success_returns_model_shape_and_provider_receives_prompt() -> None:
    env = build_test_env()
    provider = env["prompt_enhance_provider"]
    response = env["client"].post(
        "/v1/prompt-enhancements",
        json={"prompt": "  帮我总结这份文档  "},
        headers=_login(env),
    )
    assert response.status_code == 200
    # 响应直接是 pydantic 模型（无 ApiEnvelope 兼容信封）。
    assert response.json() == {"enhanced_prompt": "enhanced prompt"}
    # 去首尾空白后下发给 provider。
    assert provider.calls == ["帮我总结这份文档"]


def test_enhance_rejects_unknown_body_fields() -> None:
    env = build_test_env()
    response = env["client"].post(
        "/v1/prompt-enhancements",
        json={"prompt": "hello", "extra": True},
        headers=_login(env),
    )
    assert_error_shape(response, 422, "validation_error")


def test_enhance_requires_authentication() -> None:
    env = build_test_env()
    missing = env["client"].post("/v1/prompt-enhancements", json={"prompt": "hello"})
    assert_error_shape(missing, 401, "authentication_required")
    invalid = env["client"].post(
        "/v1/prompt-enhancements",
        json={"prompt": "hello"},
        headers={"Authorization": "Bearer not-a-token"},
    )
    assert_error_shape(invalid, 401, "authentication_required")


@pytest.mark.parametrize("prompt", ["", "   ", "\n\t "])
def test_enhance_rejects_blank_prompt(prompt: str) -> None:
    env = build_test_env()
    response = env["client"].post(
        "/v1/prompt-enhancements",
        json={"prompt": prompt},
        headers=_login(env),
    )
    assert_error_shape(response, 422, "validation_error")


def test_enhance_prompt_length_boundary() -> None:
    env = build_test_env()
    headers = _login(env)
    at_limit = env["client"].post(
        "/v1/prompt-enhancements",
        json={"prompt": "x" * 4000},
        headers=headers,
    )
    assert at_limit.status_code == 200
    over_limit = env["client"].post(
        "/v1/prompt-enhancements",
        json={"prompt": "x" * 4001},
        headers=headers,
    )
    error = assert_error_shape(over_limit, 422, "validation_error")
    assert error["details"]["max_length"] == 4000


@pytest.mark.parametrize(
    ("raised", "status", "code"),
    [
        (
            PlatformError(
                "prompt_enhance_rate_limited",
                "Prompt enhancement is rate limited",
                {"retryable": True},
                429,
                True,
            ),
            429,
            "prompt_enhance_rate_limited",
        ),
        (
            PlatformError(
                "prompt_enhance_unavailable",
                "Prompt enhancement is unavailable",
                {"retryable": True},
                503,
                True,
            ),
            503,
            "prompt_enhance_unavailable",
        ),
        (
            PlatformError(
                "prompt_enhance_timeout",
                "Prompt enhancement timed out",
                {"retryable": True},
                504,
                True,
            ),
            504,
            "prompt_enhance_timeout",
        ),
    ],
)
def test_enhance_maps_provider_errors(raised: PlatformError, status: int, code: str) -> None:
    env = build_test_env()
    env["prompt_enhance_provider"].error = raised
    response = env["client"].post(
        "/v1/prompt-enhancements",
        json={"prompt": "hello"},
        headers=_login(env),
    )
    error = assert_error_shape(response, status, code)
    assert error["details"]["retryable"] is True


def test_unconfigured_provider_assembly_fails_closed() -> None:
    """未注入适配器且全局 provider 无 api key：build_runtime 装配 503 占位。"""
    env = build_test_env()
    settings = env["runtime"].settings
    runtime = build_runtime(
        settings,
        adapters={
            "database_engine": env["engine"],
            "database_clock": env["clock"],
            "identity_access": env["identity"],
            "object_store": NullObjectStore(),
        },
    )
    assert isinstance(
        runtime.resolve("prompt_enhance_provider_port"), UnavailablePromptEnhanceProviderPort
    )
    client = TestClient(create_platform_app(settings, runtime=runtime))
    response = client.post(
        "/v1/prompt-enhancements",
        json={"prompt": "hello"},
        headers=_login(env, "carol"),
    )
    error = assert_error_shape(response, 503, "prompt_enhance_unavailable")
    assert error["details"]["retryable"] is True


def _adapter(handler) -> DashScopePromptEnhanceProvider:
    return DashScopePromptEnhanceProvider(
        base_url="https://dashscope.test",
        api_key="test-key",
        model="qwen3.7-plus",
        timeout_seconds=30,
        transport=httpx.MockTransport(handler),
    )


def test_adapter_posts_system_and_user_messages() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "优化后"}}]})

    provider = _adapter(handler)
    assert provider.enhance("草稿") == "优化后"
    assert len(seen) == 1
    assert str(seen[0].url) == "https://dashscope.test/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer test-key"
    body = json.loads(seen[0].content.decode("utf-8"))
    assert body["model"] == "qwen3.7-plus"
    assert body["messages"] == [
        {"role": "system", "content": ENHANCE_SYSTEM_PROMPT},
        {"role": "user", "content": "草稿"},
    ]


def test_adapter_maps_upstream_429() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(429, json={"error": {"message": "upstream-secret-detail"}})

    with pytest.raises(PlatformError) as raised:
        _adapter(handler).enhance("草稿")
    error = raised.value
    assert error.code == "prompt_enhance_rate_limited"
    assert error.status_code == 429
    assert error.retryable is True
    assert "upstream-secret-detail" not in error.message
    assert "upstream-secret-detail" not in str(error.details)


@pytest.mark.parametrize("status", [400, 401, 500, 502])
def test_adapter_maps_error_status_to_unavailable(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(status, json={"error": {"message": "upstream-secret-detail"}})

    with pytest.raises(PlatformError) as raised:
        _adapter(handler).enhance("草稿")
    error = raised.value
    assert error.code == "prompt_enhance_unavailable"
    assert error.status_code == 503
    assert error.retryable is True
    assert "upstream-secret-detail" not in error.message
    assert "upstream-secret-detail" not in str(error.details)


def test_adapter_maps_timeout_to_504() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow upstream", request=request)

    with pytest.raises(PlatformError) as raised:
        _adapter(handler).enhance("草稿")
    error = raised.value
    assert error.code == "prompt_enhance_timeout"
    assert error.status_code == 504
    assert error.retryable is True


def test_adapter_maps_network_error_to_503() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(PlatformError) as raised:
        _adapter(handler).enhance("草稿")
    error = raised.value
    assert error.code == "prompt_enhance_unavailable"
    assert error.status_code == 503


@pytest.mark.parametrize(
    "payload",
    [
        {"choices": []},
        {"choices": [{"message": {"content": "   "}}]},
        {"unexpected": True},
    ],
)
def test_adapter_maps_malformed_response_to_503(payload: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=payload)

    with pytest.raises(PlatformError) as raised:
        _adapter(handler).enhance("草稿")
    assert raised.value.code == "prompt_enhance_unavailable"
    assert raised.value.status_code == 503


def test_unavailable_placeholder_fails_closed() -> None:
    with pytest.raises(PlatformError) as raised:
        UnavailablePromptEnhanceProviderPort().enhance("草稿")
    error = raised.value
    assert error.code == "prompt_enhance_unavailable"
    assert error.status_code == 503
    assert error.retryable is True
