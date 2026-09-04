"""DashScope 生产 chat adapter（ChatProviderPort 生产实现）。

runtime 在 ``DASHSCOPE_API_KEY``（全局 ``ProviderSettings``）就绪时装配；
未配置时保持 ``UnavailableChatProviderPort``（dev/test 503 fail-closed）。

物理发送经 ``app.platform.model_http`` 统一内核出网：绝对 deadline、短重试、
退避与熔断由 §2.9 内核提供；prompt 组装走 chat 域契约
``assemble_generation_prompt``（冲突分述指令显式进入 prompt）。
usage 记账由 chat worker 的既有生命周期路径完成（与 wrapper 同构）。
"""

from __future__ import annotations

from typing import Any

import httpx

from app.chat.models import ChatProviderResponse
from app.chat.ports import ChatProviderRequest
from app.chat.prompt import assemble_generation_prompt
from app.platform.errors import PlatformError
from app.platform.model_http import ModelHttpError, model_http_post
from app.platform.provider import CircuitOpen

CHAT_GENERATION_MODEL = "qwen3.7-plus"
CHAT_GENERATION_TIMEOUT_SECONDS = 120.0


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


class DashScopeChatProvider:
    """OpenAI-compatible chat-completions transport for generation."""

    provider = "dashscope"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = CHAT_GENERATION_MODEL,
        timeout_seconds: float = CHAT_GENERATION_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
        now: Any = None,
    ) -> None:
        self._model = model
        self._timeout_seconds = float(timeout_seconds)
        self._now = now
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=float(timeout_seconds),
            transport=transport,
        )

    @property
    def model(self) -> str:
        return self._model

    def close(self) -> None:
        self._client.close()

    def dispose(self) -> None:
        self.close()

    def generate(self, request: ChatProviderRequest) -> ChatProviderResponse:
        try:
            egress = model_http_post(
                provider=self.provider,
                operation="chat.generate",
                # httpx 会把 base_url 规范化为带尾斜杠；直接 f-string 拼接会产出
                # `//chat/completions` 双斜杠路径（DashScope 404）。先去尾斜杠。
                url=f"{str(self._client.base_url).rstrip('/')}/chat/completions",
                headers=self._client.headers,
                payload={
                    "model": self._model,
                    "messages": [
                        {
                            "role": "user",
                            "content": assemble_generation_prompt(request),
                        }
                    ],
                },
                timeout_seconds=self._timeout_seconds,
                client=self._client,
                now=self._now,
                asynchronous=True,
            )
        except CircuitOpen as exc:
            raise self._unavailable() from exc
        except ModelHttpError as exc:
            raise self._translate(exc) from exc
        body = egress.body
        content: Any = None
        if isinstance(body, dict):
            choices = body.get("choices")
            if isinstance(choices, list) and choices:
                first = choices[0]
                message = first.get("message") if isinstance(first, dict) else None
                if isinstance(message, dict):
                    content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise self._unavailable()
        raw_usage = body.get("usage")
        usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        raw_details = usage.get("completion_tokens_details")
        details: dict[str, Any] = raw_details if isinstance(raw_details, dict) else {}
        reasoning = _int_or_none(details.get("reasoning_tokens"))
        request_id = egress.provider_request_id
        if request_id is None and isinstance(body.get("id"), str):
            request_id = body["id"]
        return ChatProviderResponse(
            content=content.strip(),
            input_tokens=_int_or_none(usage.get("prompt_tokens")) or 0,
            output_tokens=_int_or_none(usage.get("completion_tokens")) or 0,
            reasoning_tokens=reasoning,
            provider_request_id=request_id,
        )

    @staticmethod
    def _unavailable() -> PlatformError:
        return PlatformError(
            "provider_unavailable",
            "Chat model transport is unavailable",
            {"retryable": True},
            503,
            True,
        )

    def _translate(self, exc: ModelHttpError) -> PlatformError:
        if exc.timeout:
            return PlatformError(
                "provider_timeout",
                "Chat model transport timed out",
                {"retryable": True},
                504,
                True,
            )
        if exc.status_code == 429:
            return PlatformError(
                "provider_rate_limited",
                "Chat model transport is rate limited",
                {"retryable": True},
                429,
                True,
            )
        return self._unavailable()


__all__ = [
    "CHAT_GENERATION_MODEL",
    "CHAT_GENERATION_TIMEOUT_SECONDS",
    "DashScopeChatProvider",
]
