"""Remote two-stage cross-encoder reranker transport (vLLM ``/rerank``).

One endpoint serves both stages by model parameter: the 0.6B coarse filter and
the 8B final cross-encoder. Physical sends go through the unified egress kernel
(``app.platform.model_http``): bounded synchronous short retries, backoff,
circuit breaking per ``provider + operation``, and a shared absolute deadline.
Transport failures surface as classified ``PlatformError`` codes; the
``TwoStageReranker`` maps them into the existing ``rerank_degraded`` notice with
``preserve_candidate_order`` fallback — never silently swallowed.

Request/response mapping follows the vLLM (Cohere-compatible) rerank contract:
``{"model", "query", "documents"}`` in, ``{"results": [{"index",
"relevance_score"}]}`` out.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
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

RERANKER_PROVIDER_NAME = "vllm"
RERANK_OPERATION = "indexing.rerank"
RERANK_DEFAULT_TIMEOUT_SECONDS = 10.0


class HttpCrossEncoderReranker:
    """One remote cross-encoder stage behind the ``RerankerModelPort`` protocol."""

    provider_name = RERANKER_PROVIDER_NAME

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout_seconds: float = RERANK_DEFAULT_TIMEOUT_SECONDS,
        transport: Any = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._model = model
        self._timeout_seconds = float(timeout_seconds)
        self._now = now
        self._sleep = sleep
        self._base_url = base_url.rstrip("/")
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key.strip():
            headers["Authorization"] = f"Bearer {api_key.strip()}"
        self._client = build_model_http_client(
            base_url=base_url,
            headers=headers,
            timeout=float(timeout_seconds),
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def dispose(self) -> None:
        self.close()

    def score(self, query: str, hits: Sequence[RetrievalHit]) -> tuple[float, ...]:
        if not hits:
            return ()
        try:
            egress = model_http_post(
                provider=self.provider_name,
                operation=RERANK_OPERATION,
                url=f"{self._base_url}/rerank",
                payload={
                    "model": self._model,
                    "query": query,
                    "documents": [hit.chunk.text for hit in hits],
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
        return _parse_relevance_scores(egress.body, expected_count=len(hits))

    @staticmethod
    def _unavailable() -> PlatformError:
        return PlatformError(
            "reranker_unavailable",
            "Reranker model transport is unavailable",
            {"retryable": True},
            503,
            True,
        )

    @staticmethod
    def _translate(exc: ModelHttpError) -> PlatformError:
        if exc.timeout:
            return PlatformError(
                "reranker_timeout",
                "Reranker model transport timed out",
                {"retryable": True},
                504,
                True,
            )
        if exc.status_code == 429:
            return PlatformError(
                "reranker_rate_limited",
                "Reranker model transport is rate limited",
                {"retryable": True},
                429,
                True,
            )
        return HttpCrossEncoderReranker._unavailable()


def _parse_relevance_scores(body: Any, *, expected_count: int) -> tuple[float, ...]:
    """Response rows → scores in input order; refuse partial or fabricated output."""

    rows = body.get("results") if isinstance(body, Mapping) else None
    if not isinstance(rows, list) or len(rows) != expected_count:
        raise PlatformError(
            "reranker_invalid_output",
            "Reranker response did not score every candidate",
            {"expected": expected_count},
            502,
        )
    scores: list[float | None] = [None] * expected_count
    for row in rows:
        if not isinstance(row, Mapping):
            raise PlatformError(
                "reranker_invalid_output", "Reranker response row is invalid", {}, 502
            )
        index = row.get("index")
        raw_score = row.get("relevance_score", row.get("score"))
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < expected_count:
            raise PlatformError(
                "reranker_invalid_output", "Reranker response index is invalid", {}, 502
            )
        if scores[index] is not None:
            raise PlatformError(
                "reranker_invalid_output", "Reranker response index is duplicated", {}, 502
            )
        if not isinstance(raw_score, (int, float)) or isinstance(raw_score, bool):
            raise PlatformError(
                "reranker_invalid_output", "Reranker response score is invalid", {}, 502
            )
        scores[index] = float(raw_score)
    if any(score is None for score in scores):
        raise PlatformError(
            "reranker_invalid_output", "Reranker response is missing a candidate", {}, 502
        )
    result = tuple(float(score) for score in scores if score is not None)
    if any(not math.isfinite(score) for score in result):
        raise PlatformError(
            "reranker_invalid_output", "Reranker response score is not finite", {}, 502
        )
    return result


__all__ = [
    "RERANK_DEFAULT_TIMEOUT_SECONDS",
    "RERANK_OPERATION",
    "RERANKER_PROVIDER_NAME",
    "HttpCrossEncoderReranker",
]
