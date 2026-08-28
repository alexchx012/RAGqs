"""OpenAI-compatible Contextual Retrieval transport for ``ds-v4-flash``."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from .contextual import (
    CONTEXTUAL_MODEL,
    ContextualGeneration,
    ContextualProviderRejected,
    ContextualProviderUnavailable,
)

_Headers = Mapping[str, str]
_Options = Mapping[str, Any]
_Transport = Callable[[str, str, _Headers, _Options], Mapping[str, Any]]


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


class DashScopeContextualRetriever:
    """Synchronous chat-completions adapter; retries remain domain-owned."""

    provider = "dashscope"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = CONTEXTUAL_MODEL,
        revision: str = CONTEXTUAL_MODEL,
        timeout_seconds: int = 60,
        transport: _Transport | None = None,
    ) -> None:
        if model != CONTEXTUAL_MODEL:
            raise ValueError("contextual retrieval model must be ds-v4-flash")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._revision = revision or model
        self._timeout_seconds = float(timeout_seconds)
        self._transport = transport or self._http_transport

    @property
    def model(self) -> str:
        return self._model

    @property
    def model_revision(self) -> str:
        return self._revision

    def _http_transport(
        self, url: str, payload: str, headers: _Headers, options: _Options
    ) -> Mapping[str, Any]:
        from app.platform.model_http import ModelHttpError, model_http_post
        from app.platform.provider import CircuitOpen

        try:
            egress = model_http_post(
                provider=self.provider,
                operation="indexing.contextual",
                url=url,
                headers=headers,
                payload=payload,
                timeout_seconds=float(options["timeout_seconds"]),
                asynchronous=True,
            )
        except CircuitOpen as exc:
            raise ContextualProviderUnavailable("contextual provider is unavailable") from exc
        except ModelHttpError as exc:
            if exc.error_class == "invalid_response_body":
                raise ContextualProviderRejected(
                    "contextual provider response was malformed"
                ) from exc
            if exc.timeout:
                raise ContextualProviderUnavailable("contextual provider timed out") from exc
            status = exc.status_code
            if status is not None and status < 500 and status != 429:
                raise ContextualProviderRejected(
                    "contextual provider rejected the request"
                ) from exc
            raise ContextualProviderUnavailable("contextual provider is unavailable") from exc
        return egress.body

    def generate(
        self,
        *,
        prompt: str,
        chunk_id: str,
        warmup: bool,
    ) -> ContextualGeneration:
        del chunk_id, warmup
        payload = {
            "model": self._model,
            "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
        }
        response = self._transport(
            f"{self._base_url}/chat/completions",
            json.dumps(payload, ensure_ascii=False),
            {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
            {"timeout_seconds": self._timeout_seconds},
        )
        try:
            content = str(response["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise ContextualProviderRejected("contextual provider response was malformed") from exc
        raw_usage = response.get("usage")
        usage: Mapping[str, Any] = raw_usage if isinstance(raw_usage, Mapping) else {}
        raw_details = usage.get("prompt_tokens_details")
        details: Mapping[str, Any] = raw_details if isinstance(raw_details, Mapping) else {}
        details = details if isinstance(details, Mapping) else {}
        input_tokens = _integer(usage.get("prompt_tokens"))
        cached = _integer(details.get("cached_tokens"))
        output_tokens = _integer(usage.get("completion_tokens"))
        missed = (
            input_tokens - cached
            if input_tokens is not None and cached is not None and cached <= input_tokens
            else None
        )
        return ContextualGeneration(
            context=content,
            provider=self.provider,
            model=self._model,
            input_tokens=input_tokens,
            prompt_cache_hit_tokens=cached,
            prompt_cache_miss_tokens=missed,
            output_tokens=output_tokens,
            provider_request_id=(
                str(response.get("id")) if response.get("id") is not None else None
            ),
        )


__all__ = ["DashScopeContextualRetriever"]
