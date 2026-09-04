"""Remote PageIndex tree-search transport (``deepseek-v4-flash-0731``).

``PageIndexTreeRouter`` only accepts the configured remote provider, so the
transport identity is the model name itself. Physical sends go through the
unified egress kernel (``app.platform.model_http``): bounded synchronous short
retries, backoff, circuit breaking per ``provider + operation``, and a shared
absolute deadline. Transport failures surface as classified ``PlatformError``
codes; the router maps them into per-document degradation outcomes that the
retrieval pipeline reports as notices — never silently swallowed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from app.platform.errors import PlatformError
from app.platform.model_http import (
    ModelHttpError,
    build_model_http_client,
    model_http_post,
)
from app.platform.provider import CircuitOpen

from .models import RetrievalHit

TREE_SEARCH_MODEL = "deepseek-v4-flash-0731"
TREE_SEARCH_OPERATION = "indexing.tree_search"
TREE_SEARCH_DEFAULT_TIMEOUT_SECONDS = 30.0

TREE_SEARCH_SYSTEM_INSTRUCTION = (
    "You search one candidate document for the user's question. The provided "
    "excerpt is the navigation entry into the document tree; answer only from "
    "the document and keep the answer in the document language. If the document "
    "does not contain the answer, state that plainly."
)


class DashScopeTreeSearchProvider:
    """OpenAI-compatible chat-completions transport for one-document tree search.

    The long-lived pooled client is built through the platform factory; callers
    own its lifecycle via ``close``/``dispose`` (PlatformRuntime.close does).
    """

    provider_name = TREE_SEARCH_MODEL

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = TREE_SEARCH_MODEL,
        timeout_seconds: float = TREE_SEARCH_DEFAULT_TIMEOUT_SECONDS,
        transport: Any = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if model != TREE_SEARCH_MODEL:
            raise ValueError("tree search model must be deepseek-v4-flash-0731")
        self._model = model
        self._timeout_seconds = float(timeout_seconds)
        self._now = now
        self._sleep = sleep
        self._base_url = base_url.rstrip("/")
        self._client = build_model_http_client(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=float(timeout_seconds),
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def dispose(self) -> None:
        self.close()

    def search_document(self, query: str, hit: RetrievalHit) -> Mapping[str, Any]:
        prompt = "\n\n".join(
            (
                f"Question: {query}",
                f"Document {hit.chunk.document_id} excerpt:\n{hit.chunk.text}",
            )
        )
        try:
            egress = model_http_post(
                provider=self.provider_name,
                operation=TREE_SEARCH_OPERATION,
                url=f"{self._base_url}/chat/completions",
                payload={
                    "model": self._model,
                    "temperature": 0,
                    "messages": [
                        {"role": "system", "content": TREE_SEARCH_SYSTEM_INSTRUCTION},
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout_seconds=self._timeout_seconds,
                client=self._client,
                asynchronous=False,
                now=self._now,
                sleep=self._sleep,
            )
        except CircuitOpen as exc:
            raise self._unavailable() from exc
        except ModelHttpError as exc:
            raise self._translate(exc) from exc
        content: Any = None
        if isinstance(egress.body, dict):
            choices = egress.body.get("choices")
            if isinstance(choices, list) and choices:
                first = choices[0]
                message = first.get("message") if isinstance(first, dict) else None
                if isinstance(message, dict):
                    content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise PlatformError(
                "tree_provider_invalid_response",
                "Tree search provider response was malformed",
                {},
                502,
            )
        return {
            "document_id": str(hit.chunk.document_id),
            "chunk_id": hit.chunk.chunk_id,
            "text": content.strip(),
        }

    @staticmethod
    def _unavailable() -> PlatformError:
        return PlatformError(
            "tree_provider_unavailable",
            "Tree search provider is unavailable",
            {"retryable": True},
            503,
            True,
        )

    @staticmethod
    def _translate(exc: ModelHttpError) -> PlatformError:
        if exc.timeout:
            return PlatformError(
                "tree_provider_timeout",
                "Tree search provider timed out",
                {"retryable": True},
                504,
                True,
            )
        if exc.status_code == 429:
            return PlatformError(
                "tree_provider_rate_limited",
                "Tree search provider is rate limited",
                {"retryable": True},
                429,
                True,
            )
        if exc.error_class == "invalid_response_body":
            return PlatformError(
                "tree_provider_invalid_response",
                "Tree search provider response was malformed",
                {},
                502,
            )
        return DashScopeTreeSearchProvider._unavailable()


__all__ = [
    "TREE_SEARCH_DEFAULT_TIMEOUT_SECONDS",
    "TREE_SEARCH_MODEL",
    "TREE_SEARCH_OPERATION",
    "DashScopeTreeSearchProvider",
]
