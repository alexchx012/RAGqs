from __future__ import annotations

import json

import httpx
import pytest

from app.indexing.embedding import (
    DEFAULT_EMBEDDING_BASE_URL,
    EmbeddingConfig,
    InMemoryEmbeddingProvider,
    OpenAICompatibleEmbedding,
)
from app.platform.errors import PlatformError


def _config(**overrides: object) -> EmbeddingConfig:
    values: dict[str, object] = {
        "base_url": DEFAULT_EMBEDDING_BASE_URL,
        "api_key": "test-key",
        "model": "text-embedding-v4",
        "revision": "text-embedding-v4",
        "dimension": 4,
        "metric": "cosine",
    }
    values.update(overrides)
    return EmbeddingConfig(**values)  # type: ignore[arg-type]


def test_embedding_config_rejects_missing_fields() -> None:
    with pytest.raises(PlatformError) as error:
        EmbeddingConfig(
            base_url="",
            api_key="key",
            model="model",
            revision="rev",
            dimension=8,
            metric="cosine",
        )
    assert error.value.code == "embedding_config_invalid"


def test_memory_embedding_is_deterministic_and_uses_configured_dimension() -> None:
    provider = InMemoryEmbeddingProvider(_config())
    first = provider.embed(["hello world"])
    second = provider.embed(["hello world"])
    assert first == second
    assert len(first[0]) == 4


def test_openai_compatible_embedding_posts_shared_ingest_and_query_config() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3, 0.4]}]},
        )

    provider = OpenAICompatibleEmbedding(
        _config(),
        transport=httpx.MockTransport(handler),
    )
    ingest = provider.embed(["chunk text"])
    query = provider.embed(["user query"])
    assert ingest[0] == (0.1, 0.2, 0.3, 0.4)
    assert query[0] == (0.1, 0.2, 0.3, 0.4)
    assert len(seen) == 2
    assert str(seen[0].url).endswith("/embeddings")
    body = json.loads(seen[0].content.decode("utf-8"))
    assert body["model"] == "text-embedding-v4"
    assert seen[0].headers["authorization"] == "Bearer test-key"


def test_openai_compatible_embedding_rejects_dimension_mismatch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})

    provider = OpenAICompatibleEmbedding(
        _config(),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(PlatformError) as error:
        provider.embed(["chunk text"])
    assert error.value.code == "embedding_dimension_mismatch"


@pytest.mark.parametrize(
    "payload",
    (
        {"data": []},
        {"data": [{"index": 0}]},
        "not-json",
    ),
)
def test_openai_compatible_embedding_does_not_fabricate_on_failure(payload: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        if payload == "not-json":
            return httpx.Response(200, text="nope")
        return httpx.Response(200, json=payload)

    provider = OpenAICompatibleEmbedding(
        _config(),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(PlatformError) as error:
        provider.embed(["chunk text"])
    assert error.value.code == "embedding_failed"
