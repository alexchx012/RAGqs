"""DashScope OpenAI-compatible transport for single-shot prompt enhancement.

Used by ``POST /v1/prompt-enhancements``: one chat-completions call maps the
user draft to a rewritten prompt. Credentials and endpoint come from the global
``ProviderSettings``; the system prompt is baked in here (措辞优化指令) and is
not a configuration item. Errors are classified into the endpoint's stable
codes without leaking upstream bodies, tracebacks or credentials.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.platform.errors import PlatformError
from app.platform.model_http import build_model_http_client

PROMPT_ENHANCE_DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

ENHANCE_SYSTEM_PROMPT = (
    "You are a prompt editor. Rewrite the user's draft prompt so it becomes clearer, "
    "more specific and easier for a retrieval-augmented assistant to answer, while "
    "preserving the original intent and the original language. Return only the "
    "rewritten prompt text: no explanations, no quoting, no markdown fences."
)


class DashScopePromptEnhanceProvider:
    """Synchronous chat-completions adapter implementing ``PromptEnhancePort``."""

    provider = "dashscope"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 30.0,
        transport: Any = None,
    ) -> None:
        self._model = model
        self._timeout_seconds = float(timeout_seconds)
        # 连接池由 platform 层统一构造（唯一白名单）；dispose/close 语义保留在本类。
        self._client = build_model_http_client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
            transport=transport,
        )

    @property
    def model(self) -> str:
        return self._model

    def close(self) -> None:
        self._client.close()

    def dispose(self) -> None:
        self.close()

    def enhance(self, prompt: str) -> str:
        from app.platform.model_http import ModelHttpError, model_http_post
        from app.platform.provider import CircuitOpen

        try:
            egress = model_http_post(
                provider=self.provider,
                operation="chat.prompt_enhance",
                # httpx 把 base_url 规范化为带尾斜杠；先去尾斜杠避免 `//` 双斜杠路径。
                url=f"{str(self._client.base_url).rstrip('/')}/chat/completions",
                headers=self._client.headers,
                payload={
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": ENHANCE_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout_seconds=self._timeout_seconds,
                client=self._client,
            )
        except CircuitOpen as exc:
            raise self._unavailable() from exc
        except ModelHttpError as exc:
            if exc.timeout:
                raise PlatformError(
                    "prompt_enhance_timeout",
                    "Prompt enhancement timed out",
                    {"retryable": True},
                    504,
                    True,
                ) from exc
            if exc.status_code == 429:
                raise PlatformError(
                    "prompt_enhance_rate_limited",
                    "Prompt enhancement is rate limited",
                    {"retryable": True},
                    429,
                    True,
                ) from exc
            raise self._unavailable() from exc
        payload = egress.body
        content: Any = None
        if isinstance(payload, Mapping):
            choices = payload.get("choices")
            if isinstance(choices, list) and choices:
                first = choices[0]
                message = first.get("message") if isinstance(first, Mapping) else None
                if isinstance(message, Mapping):
                    content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise self._unavailable()
        return content.strip()

    @staticmethod
    def _unavailable() -> PlatformError:
        return PlatformError(
            "prompt_enhance_unavailable",
            "Prompt enhancement is unavailable",
            {"retryable": True},
            503,
            True,
        )


__all__ = [
    "ENHANCE_SYSTEM_PROMPT",
    "PROMPT_ENHANCE_DEFAULT_BASE_URL",
    "DashScopePromptEnhanceProvider",
]
