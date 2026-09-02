from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Literal, Protocol
from urllib.parse import urljoin

from app.platform.errors import PlatformError
from app.usage.ledger import OwnershipSnapshot, ProviderMeasurement
from app.usage.ports import UsageSubmissionPort

EmbeddingMetric = Literal["cosine", "l2", "ip"]
DEFAULT_EMBEDDING_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
# DashScope-compatible endpoints cap texts per embeddings request well below
# large-document chunk counts, so embed() fans out in fixed batches.
_EMBEDDING_BATCH_SIZE = 10
# Batches are independent sends (usage is idempotent per batch index),
# so a small worker pool overlaps their network latency without
# reordering results.
_EMBEDDING_BATCH_CONCURRENCY = 4
_DOCUMENT_EMBEDDING_OPERATION = "document_embedding"


@dataclass(frozen=True, slots=True)
class EmbeddingConfig:
    base_url: str
    api_key: str
    model: str
    revision: str
    dimension: int
    metric: EmbeddingMetric

    def __post_init__(self) -> None:
        if not self.base_url.strip():
            raise PlatformError(
                "embedding_config_invalid", "embedding base_url is required", {}, 422
            )
        if not self.api_key.strip():
            raise PlatformError(
                "embedding_config_invalid", "embedding api_key is required", {}, 422
            )
        if not self.model.strip():
            raise PlatformError("embedding_config_invalid", "embedding model is required", {}, 422)
        if not self.revision.strip():
            raise PlatformError(
                "embedding_config_invalid", "embedding revision is required", {}, 422
            )
        if self.dimension < 1:
            raise PlatformError(
                "embedding_config_invalid", "embedding dimension is invalid", {}, 422
            )
        if self.metric not in {"cosine", "l2", "ip"}:
            raise PlatformError("embedding_config_invalid", "embedding metric is invalid", {}, 422)

    def matches(
        self,
        *,
        model: str | None = None,
        revision: str | None = None,
        dimension: int | None = None,
        metric: str | None = None,
    ) -> bool:
        if model is not None and model != self.model:
            return False
        if revision is not None and revision != self.revision:
            return False
        if dimension is not None and int(dimension) != self.dimension:
            return False
        if metric is not None and metric != self.metric:
            return False
        return True

    def to_manifest(self) -> dict[str, Any]:
        return {
            "embedding_model": self.model,
            "embedding_revision": self.revision,
            "embedding_dimension": self.dimension,
            "embedding_metric": self.metric,
        }


@dataclass(frozen=True, slots=True)
class EmbeddingUsageContext:
    """Immutable execution facts used when an embedding batch is actually sent."""

    execution_kind: str
    execution_id: str
    attempt_id: str
    generation_id: str
    publication_id: str
    deadline_utc: datetime
    replay_generation: int
    ownership: OwnershipSnapshot

    def __post_init__(self) -> None:
        if (
            any(
                not isinstance(value, str) or not value.strip()
                for value in (
                    self.execution_kind,
                    self.execution_id,
                    self.attempt_id,
                    self.generation_id,
                    self.publication_id,
                )
            )
            or not isinstance(self.deadline_utc, datetime)
            or self.deadline_utc.tzinfo is None
            or isinstance(self.replay_generation, bool)
            or not isinstance(self.replay_generation, int)
            or self.replay_generation < 0
            or not isinstance(self.ownership, OwnershipSnapshot)
        ):
            raise PlatformError(
                "embedding_usage_context_invalid",
                "embedding usage context is invalid",
                {},
                422,
            )

    def resource_id_for_batch(self, batch_index: int) -> str:
        if isinstance(batch_index, bool) or not isinstance(batch_index, int) or batch_index < 0:
            raise PlatformError(
                "embedding_usage_context_invalid",
                "embedding batch index is invalid",
                {},
                422,
            )
        return f"{self.publication_id}:embedding:{batch_index}"


class EmbeddingProvider(Protocol):
    provider_name: str
    config: EmbeddingConfig

    def embed(
        self,
        texts: Sequence[str],
        *,
        usage_context: EmbeddingUsageContext | None = None,
    ) -> tuple[tuple[float, ...], ...]: ...


def _normalize_texts(texts: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for item in texts:
        if not isinstance(item, str) or not item.strip():
            raise PlatformError("embedding_input_invalid", "embedding text is required", {}, 422)
        normalized.append(item)
    if not normalized:
        raise PlatformError("embedding_input_invalid", "embedding text is required", {}, 422)
    return tuple(normalized)


class InMemoryEmbeddingProvider:
    """Deterministic development/test adapter. Production must not use it."""

    provider_name = "memory"

    def __init__(self, config: EmbeddingConfig) -> None:
        self.config = config

    def embed(
        self,
        texts: Sequence[str],
        *,
        usage_context: EmbeddingUsageContext | None = None,
    ) -> tuple[tuple[float, ...], ...]:
        del usage_context
        vectors: list[tuple[float, ...]] = []
        for text in _normalize_texts(texts):
            seed = sum(ord(char) for char in text) + len(text)
            values = [
                (((seed + index * 17) % 1000) / 1000.0) for index in range(self.config.dimension)
            ]
            vectors.append(tuple(values))
        return tuple(vectors)


class OpenAICompatibleEmbedding:
    """OpenAI-compatible embeddings; default target is DashScope compatible-mode."""

    provider_name = "openai-compatible"
    requires_usage_context = True

    def __init__(
        self,
        config: EmbeddingConfig,
        *,
        transport: Any = None,
        timeout: float = 30.0,
        usage_submission: UsageSubmissionPort | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self._timeout = timeout
        self._transport = transport
        from app.platform.model_http import build_model_http_client

        # Long-lived pooled egress client; per-attempt timeouts are
        # still derived per send by ModelHttpTransport.
        self._client = build_model_http_client(timeout=timeout, transport=transport)
        self._usage_submission = usage_submission
        self._now = now or (lambda: datetime.now(UTC))

    def set_usage_submission(self, submission: UsageSubmissionPort | None) -> None:
        self._usage_submission = submission

    def close(self) -> None:
        self._client.close()

    def dispose(self) -> None:
        self.close()

    def embed(
        self,
        texts: Sequence[str],
        *,
        usage_context: EmbeddingUsageContext | None = None,
    ) -> tuple[tuple[float, ...], ...]:
        payload = _normalize_texts(texts)
        batches = [
            payload[start : start + _EMBEDDING_BATCH_SIZE]
            for start in range(0, len(payload), _EMBEDDING_BATCH_SIZE)
        ]
        if len(batches) > 1:
            with ThreadPoolExecutor(
                max_workers=min(_EMBEDDING_BATCH_CONCURRENCY, len(batches)),
                thread_name_prefix="embedding-batch",
            ) as pool:
                futures = [
                    pool.submit(
                        self._embed_batch,
                        batch,
                        usage_context=usage_context,
                        batch_index=batch_index,
                    )
                    for batch_index, batch in enumerate(batches)
                ]
                vectors: list[tuple[float, ...]] = []
                for future in futures:
                    vectors.extend(future.result())
                return tuple(vectors)
        vectors = []
        for batch_index, batch in enumerate(batches):
            vectors.extend(
                self._embed_batch(
                    batch,
                    usage_context=usage_context,
                    batch_index=batch_index,
                )
            )
        return tuple(vectors)

    @staticmethod
    def _unknown_measurement() -> ProviderMeasurement:
        return ProviderMeasurement(
            input_tokens=None,
            prompt_cache_hit_tokens=None,
            prompt_cache_miss_tokens=None,
            output_tokens=None,
            reasoning_tokens=None,
            image_count=None,
            visual_input_tokens=None,
            embedding_input_tokens=None,
            vector_count=None,
            measurement_sources={},
        )

    @staticmethod
    def _measurement_from_response(body: Any, *, vector_count: int) -> ProviderMeasurement:
        prompt_tokens: int | None = None
        if isinstance(body, dict):
            usage = body.get("usage")
            reported = usage.get("prompt_tokens") if isinstance(usage, dict) else None
            if isinstance(reported, int) and not isinstance(reported, bool) and reported >= 0:
                prompt_tokens = reported
        sources = {"vector_count": "client_measured"}
        if prompt_tokens is not None:
            sources["embedding_input_tokens"] = "provider_reported"
        return ProviderMeasurement(
            input_tokens=None,
            prompt_cache_hit_tokens=None,
            prompt_cache_miss_tokens=None,
            output_tokens=None,
            reasoning_tokens=None,
            image_count=None,
            visual_input_tokens=None,
            embedding_input_tokens=prompt_tokens,
            vector_count=vector_count,
            measurement_sources=sources,
        )

    @staticmethod
    def _request_fingerprint(
        context: EmbeddingUsageContext, payload: tuple[str, ...], batch_index: int
    ) -> str:
        facts = "\x00".join(
            (
                context.execution_kind,
                context.execution_id,
                context.attempt_id,
                context.generation_id,
                context.publication_id,
                str(context.replay_generation),
                str(batch_index),
                *payload,
            )
        )
        return f"embedding:{sha256(facts.encode('utf-8')).hexdigest()}"

    def _usage_gate(self, context: EmbeddingUsageContext | None) -> None:
        if context is not None and self._usage_submission is None:
            raise PlatformError(
                "embedding_usage_unavailable",
                "Document embedding usage submission is not configured",
                {},
                503,
            )

    def _parse_vectors(self, body: Any, payload: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        """响应体 → 向量序列；保持既有错误码（embedding_failed/dimension_mismatch）。"""

        rows = body.get("data") if isinstance(body, dict) else None
        if not isinstance(rows, list) or len(rows) != len(payload):
            raise PlatformError("embedding_failed", "embedding response is incomplete", {}, 503)
        if any(not isinstance(item, dict) for item in rows):
            raise PlatformError("embedding_failed", "embedding response is invalid", {}, 503)
        try:
            ordered = sorted(rows, key=lambda item: int(item.get("index", 0)))
        except (TypeError, ValueError) as exc:
            raise PlatformError(
                "embedding_failed", "embedding response is invalid", {}, 503
            ) from exc
        vectors: list[tuple[float, ...]] = []
        for row in ordered:
            raw = row.get("embedding") if isinstance(row, dict) else None
            if not isinstance(raw, list) or not raw:
                raise PlatformError("embedding_failed", "embedding vector is missing", {}, 503)
            try:
                vector = tuple(float(value) for value in raw)
            except (TypeError, ValueError) as exc:
                raise PlatformError(
                    "embedding_failed", "embedding vector is invalid", {}, 503
                ) from exc
            if len(vector) != self.config.dimension:
                raise PlatformError(
                    "embedding_dimension_mismatch",
                    "embedding dimension does not match the configured profile",
                    {"expected": self.config.dimension, "actual": len(vector)},
                    503,
                )
            vectors.append(vector)
        return tuple(vectors)

    def _translate_policy_result(self, result: Any) -> PlatformError:
        error_class = result.error_class or ""
        if error_class.startswith("embedding_dimension_mismatch"):
            suffix = error_class.rsplit("_", 1)[-1]
            actual: int | None = int(suffix) if suffix.isdigit() else None
            return PlatformError(
                "embedding_dimension_mismatch",
                "embedding dimension does not match the configured profile",
                {"expected": self.config.dimension, "actual": actual},
                503,
            )
        return PlatformError("embedding_failed", "embedding request failed", {}, 503)

    def _embed_batch(
        self,
        payload: tuple[str, ...],
        *,
        usage_context: EmbeddingUsageContext | None,
        batch_index: int,
    ) -> tuple[tuple[float, ...], ...]:
        """统一出网：物理发送经平台 transport 内核，usage 走唯一包装生命周期。"""

        from app.platform.model_http import (
            ModelHttpError,
            ModelHttpTransport,
            model_http_post,
            new_provider_call_root_id,
        )
        from app.platform.provider import (
            ProviderCallContext,
            ProviderFailure,
            ProviderPreSendDeadlineExceeded,
        )
        from app.usage.provider_integration import (
            UsageSubmissionLifecycle,
            run_provider_call_with_usage,
        )

        self._usage_gate(usage_context)
        url = urljoin(self.config.base_url.rstrip("/") + "/", "embeddings")
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        request_payload: dict[str, Any] = {
            "model": self.config.model,
            "input": list(payload),
        }
        if usage_context is None:
            try:
                egress = model_http_post(
                    provider=self.provider_name,
                    operation=_DOCUMENT_EMBEDDING_OPERATION,
                    url=url,
                    headers=headers,
                    payload=request_payload,
                    timeout_seconds=self._timeout,
                    client=self._client,
                    asynchronous=True,
                    now=self._now,
                )
                body = egress.body
            except ModelHttpError as exc:
                raise PlatformError(
                    "embedding_failed", "embedding request failed", {}, 503
                ) from exc
            return self._parse_vectors(body, payload)

        context = usage_context
        assert self._usage_submission is not None
        call_context = ProviderCallContext(
            provider=self.provider_name,
            operation=_DOCUMENT_EMBEDDING_OPERATION,
            provider_call_id=new_provider_call_root_id("embed"),
            attempt_id=context.attempt_id,
            deadline_utc=context.deadline_utc,
            resource_id=context.resource_id_for_batch(batch_index),
        )

        def send_and_parse(ctx: ProviderCallContext, req: Any) -> Any:
            """一次物理发送 + 完整响应解析；解析失败按 sent=True 确定失败记账。"""

            transport = ModelHttpTransport(
                url=url, headers=headers, client=self._client, now=self._now
            )
            response = transport(ctx, req)
            try:
                body = response.json()
            except ValueError as exc:
                raise ProviderFailure(
                    "embedding_failed", retryable=False, sent=True, status_code=200
                ) from exc
            try:
                vectors = self._parse_vectors(body, payload)
            except PlatformError as exc:
                error_class = (
                    f"embedding_dimension_mismatch_{exc.details.get('actual')}"
                    if exc.code == "embedding_dimension_mismatch"
                    else "embedding_failed"
                )
                raise ProviderFailure(
                    error_class, retryable=False, sent=True, status_code=200
                ) from exc
            return body, vectors

        def measurement_extractor(
            value: Any, ctx: ProviderCallContext, failure: ProviderFailure | None
        ) -> ProviderMeasurement:
            if failure is None and isinstance(value, tuple):
                body, vectors = value
                return self._measurement_from_response(body, vector_count=len(vectors))
            return self._unknown_measurement()

        def ownership_provider(ctx: ProviderCallContext) -> OwnershipSnapshot:
            del ctx
            return context.ownership

        lifecycle = UsageSubmissionLifecycle(
            self._usage_submission,
            generation_id=context.generation_id,
            replay_generation=context.replay_generation,
        )
        try:
            result = run_provider_call_with_usage(
                operation=send_and_parse,
                context=call_context,
                model=self.config.model,
                lifecycle=lifecycle,
                measurement_extractor=measurement_extractor,
                ownership_provider=ownership_provider,
                execution_kind=context.execution_kind,
                execution_id=context.execution_id,
                request_fingerprint=self._request_fingerprint(context, payload, batch_index),
                request=request_payload,
                asynchronous=True,
                now=self._now,
            )
        except ProviderPreSendDeadlineExceeded as exc:
            raise PlatformError("embedding_failed", "embedding request failed", {}, 503) from exc
        if result.state != "succeeded":
            raise self._translate_policy_result(result)
        body, vectors = result.value
        del body
        return vectors


def embedding_config_from_mapping(value: Any) -> EmbeddingConfig:
    if not isinstance(value, dict):
        raise PlatformError(
            "embedding_config_invalid", "embedding configuration is required", {}, 422
        )
    dimension = value.get("dimension")
    if dimension is None:
        raise PlatformError("embedding_config_invalid", "embedding dimension is invalid", {}, 422)
    try:
        parsed_dimension = int(dimension)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise PlatformError(
            "embedding_config_invalid", "embedding dimension is invalid", {}, 422
        ) from exc
    metric = str(value.get("metric") or "cosine")
    if metric not in {"cosine", "l2", "ip"}:
        raise PlatformError("embedding_config_invalid", "embedding metric is invalid", {}, 422)
    return EmbeddingConfig(
        base_url=str(value.get("base_url") or DEFAULT_EMBEDDING_BASE_URL),
        api_key=str(value.get("api_key") or ""),
        model=str(value.get("model") or ""),
        revision=str(value.get("revision") or value.get("model") or ""),
        dimension=parsed_dimension,
        metric=metric,  # type: ignore[arg-type]
    )


__all__ = [
    "DEFAULT_EMBEDDING_BASE_URL",
    "EmbeddingConfig",
    "EmbeddingMetric",
    "EmbeddingProvider",
    "EmbeddingUsageContext",
    "InMemoryEmbeddingProvider",
    "OpenAICompatibleEmbedding",
    "embedding_config_from_mapping",
]
