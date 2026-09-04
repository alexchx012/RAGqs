"""Remote tree-search provider transport tests (mock transport injection).

成功映射、超时/429/5xx/坏响应分类、同步短重试恢复、router 集成（分类错误进入
既有 degraded 路径）与长命 client 生命周期。
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.indexing.models import IndexChunk, RetrievalHit
from app.indexing.tree_search import PageIndexTreeRouter
from app.indexing.tree_search_provider import DashScopeTreeSearchProvider
from app.platform.errors import PlatformError


def _hit(document_id: str = "doc_1", chunk_id: str = "c1") -> RetrievalHit:
    return RetrievalHit(
        chunk=IndexChunk(
            chunk_id=chunk_id,
            generation_id="generation_initial",
            publication_id="publication_1",
            document_id=document_id,
            document_version_id="version_1",
            space_id="space_1",
            text=f"document text {document_id}",
            embedding_text=f"document text {document_id}",
            locator={},
            snippet=chunk_id,
            media_kind="text/plain",
            manifest_hash="manifest_1",
        ),
        score=0.5,
        source="dense",
        rerank_score=0.9,
    )


def _provider(handler, **overrides) -> DashScopeTreeSearchProvider:
    values: dict[str, object] = {
        "base_url": "https://dashscope.example.test/compatible-mode/v1",
        "api_key": "test-key",
        "transport": httpx.MockTransport(handler),
        "sleep": lambda _seconds: None,
    }
    values.update(overrides)
    return DashScopeTreeSearchProvider(**values)  # type: ignore[arg-type]


def test_search_document_posts_chat_completion_and_maps_answer() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "  tree answer  "}}]},
        )

    provider = _provider(handler)
    result = provider.search_document("user question", _hit("doc_9", "c3"))
    assert result == {"document_id": "doc_9", "chunk_id": "c3", "text": "tree answer"}
    assert len(seen) == 1
    assert str(seen[0].url).endswith("/chat/completions")
    assert seen[0].headers["authorization"] == "Bearer test-key"
    body = json.loads(seen[0].content.decode("utf-8"))
    assert body["model"] == "deepseek-v4-flash-0731"
    assert any("user question" in str(m.get("content", "")) for m in body["messages"])
    assert any("document text doc_9" in str(m.get("content", "")) for m in body["messages"])
    provider.close()


def test_provider_identity_is_the_remote_model_only() -> None:
    provider = _provider(lambda request: httpx.Response(200, json={}))
    assert provider.provider_name == "deepseek-v4-flash-0731"
    provider.close()
    with pytest.raises(ValueError, match="deepseek-v4-flash-0731"):
        _provider(
            lambda request: httpx.Response(200, json={}),
            model="local-model",
        )


def test_rate_limit_is_retried_then_recovers_within_sync_budget() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, text="rate limited")
        return httpx.Response(200, json={"choices": [{"message": {"content": "recovered"}}]})

    provider = _provider(handler)
    result = provider.search_document("q", _hit())
    assert result["text"] == "recovered"
    assert len(attempts) == 2
    provider.close()


def test_rate_limit_exhaustion_classifies_as_rate_limited() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(429, text="rate limited")

    provider = _provider(handler)
    with pytest.raises(PlatformError) as error:
        provider.search_document("q", _hit())
    assert error.value.code == "tree_provider_rate_limited"
    assert error.value.status_code == 429
    assert len(attempts) == 3  # synchronous budget
    provider.close()


def test_timeout_classifies_as_provider_timeout() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ReadTimeout("read timed out")

    provider = _provider(handler)
    with pytest.raises(PlatformError) as error:
        provider.search_document("q", _hit())
    assert error.value.code == "tree_provider_timeout"
    assert error.value.status_code == 504
    provider.close()


def test_non_retryable_5xx_classifies_as_unavailable_without_retry() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(500, text="server error")

    provider = _provider(handler)
    with pytest.raises(PlatformError) as error:
        provider.search_document("q", _hit())
    assert error.value.code == "tree_provider_unavailable"
    assert error.value.status_code == 503
    assert len(attempts) == 1
    provider.close()


@pytest.mark.parametrize(
    "payload",
    (
        {"choices": []},
        {"choices": [{"message": {"content": ""}}]},
        {"unexpected": True},
    ),
)
def test_malformed_success_body_is_rejected_not_fabricated(payload: object) -> None:
    provider = _provider(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(PlatformError) as error:
        provider.search_document("q", _hit())
    assert error.value.code == "tree_provider_invalid_response"
    provider.close()


def test_non_json_body_is_classified_not_silent() -> None:
    provider = _provider(lambda request: httpx.Response(200, text="not-json"))
    with pytest.raises(PlatformError) as error:
        provider.search_document("q", _hit())
    assert error.value.code == "tree_provider_invalid_response"
    provider.close()


def test_transport_errors_route_into_existing_degradation_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    provider = _provider(handler)
    router = PageIndexTreeRouter(provider)
    outcome = router("query", [_hit("doc_a"), _hit("doc_b")], max_documents=2, rag_call_limit=4)
    assert not outcome.skipped
    degraded = [item for item in outcome.documents if item.status == "degraded"]
    assert {item.reason for item in degraded} == {"tree_provider_rate_limited"}
    provider.close()


def test_close_releases_the_long_lived_client() -> None:
    provider = _provider(lambda request: httpx.Response(200, json={}))
    client = provider._client
    provider.close()
    assert client.is_closed
