from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import urljoin

import httpx

from app.platform.errors import PlatformError

EmbeddingMetric = Literal["cosine", "l2", "ip"]
DEFAULT_EMBEDDING_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


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


class EmbeddingProvider(Protocol):
    provider_name: str
    config: EmbeddingConfig

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]: ...


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

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
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

    def __init__(
        self,
        config: EmbeddingConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.config = config
        self._timeout = timeout
        self._transport = transport

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        payload = _normalize_texts(texts)
        url = urljoin(self.config.base_url.rstrip("/") + "/", "embeddings")
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                response = client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.config.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={"model": self.config.model, "input": list(payload)},
                )
        except httpx.HTTPError as exc:
            raise PlatformError(
                "embedding_failed",
                "embedding request failed",
                {},
                503,
            ) from exc
        if response.status_code >= 400:
            raise PlatformError("embedding_failed", "embedding request failed", {}, 503)
        try:
            body = response.json()
        except ValueError as exc:
            raise PlatformError(
                "embedding_failed", "embedding response is invalid", {}, 503
            ) from exc
        rows = body.get("data") if isinstance(body, dict) else None
        if not isinstance(rows, list) or len(rows) != len(payload):
            raise PlatformError("embedding_failed", "embedding response is incomplete", {}, 503)
        ordered = sorted(rows, key=lambda item: int(item.get("index", 0)))
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


def embedding_config_from_mapping(value: Any) -> EmbeddingConfig:
    if not isinstance(value, dict):
        raise PlatformError(
            "embedding_config_invalid", "embedding configuration is required", {}, 422
        )
    dimension = value.get("dimension")
    try:
        parsed_dimension = int(dimension)
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
    "InMemoryEmbeddingProvider",
    "OpenAICompatibleEmbedding",
    "embedding_config_from_mapping",
]
