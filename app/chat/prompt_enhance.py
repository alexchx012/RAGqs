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

import httpx

from app.platform.errors import PlatformError

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
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._model = model
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

    def enhance(self, prompt: str) -> str:
        try:
            response = self._client.post(
                "/chat/completions",
                json={
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": ENHANCE_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                },
            )
        except httpx.TimeoutException as exc:
            raise PlatformError(
                "prompt_enhance_timeout",
                "Prompt enhancement timed out",
                {"retryable": True},
                504,
                True,
            ) from exc
        except httpx.HTTPError as exc:
            raise self._unavailable() from exc
        if response.status_code == 429:
            raise PlatformError(
                "prompt_enhance_rate_limited",
                "Prompt enhancement is rate limited",
                {"retryable": True},
                429,
                True,
            )
        if response.status_code >= 400:
            raise self._unavailable()
        try:
            payload = response.json()
        except ValueError as exc:
            raise self._unavailable() from exc
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
