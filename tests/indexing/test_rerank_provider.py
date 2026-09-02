"""Remote two-stage cross-encoder reranker transport tests (mock transport).

成功映射（按 index 还原输入顺序）、部分/重复/非有限输出拒绝、超时/429/5xx 分类、
TwoStage 两阶段编排（粗筛裁剪→精排唯一分数）与既有降级语义复用。
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.indexing.models import IndexChunk, RetrievalHit, RetrievalProfile
from app.indexing.rerank import (
    DEFAULT_COARSE_KEEP_PER_LIBRARY,
    DEFAULT_LIBRARY_CANDIDATE_LIMIT,
    RerankerRelease,
    TwoStageReranker,
)
from app.indexing.rerank_provider import HttpCrossEncoderReranker
from app.platform.errors import PlatformError


def _hit(chunk_id: str, source: str = "dense") -> RetrievalHit:
    return RetrievalHit(
        chunk=IndexChunk(
            chunk_id=chunk_id,
            generation_id="generation_initial",
            publication_id="publication_1",
            document_id=f"document_{chunk_id}",
            document_version_id="version_1",
            space_id="space_1",
            text=f"text {chunk_id}",
            embedding_text=f"text {chunk_id}",
            locator={},
            snippet=chunk_id,
            media_kind="text/plain",
            manifest_hash="manifest_1",
        ),
        score=0.5,
        source=source,
    )


def _stage(handler, *, model: str = "qwen3-reranker-8b") -> HttpCrossEncoderReranker:
    return HttpCrossEncoderReranker(
        base_url="https://reranker.example.test",
        model=model,
        api_key="stage-key",
        transport=httpx.MockTransport(handler),
        sleep=lambda _seconds: None,
    )


def test_score_posts_rerank_contract_and_maps_rows_to_input_order() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        # Provider returns rows sorted by relevance, not by input index.
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 2, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.1},
                    {"index": 1, "relevance_score": 0.5},
                ]
            },
        )

    stage = _stage(handler)
    hits = [_hit("a"), _hit("b"), _hit("c")]
    assert stage.score("query", hits) == (0.1, 0.5, 0.9)
    assert len(seen) == 1
    assert str(seen[0].url).endswith("/rerank")
    assert seen[0].headers["authorization"] == "Bearer stage-key"
    body = json.loads(seen[0].content.decode("utf-8"))
    assert body == {
        "model": "qwen3-reranker-8b",
        "query": "query",
        "documents": ["text a", "text b", "text c"],
    }
    stage.close()


def test_empty_candidates_do_not_egress() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no send expected for empty candidates")

    stage = _stage(handler)
    assert stage.score("query", ()) == ()
    stage.close()


def test_provider_identity_and_optional_auth() -> None:
    stage = _stage(lambda request: httpx.Response(200, json={"results": []}))
    assert stage.provider_name == "vllm"
    stage.close()

    unauthenticated = HttpCrossEncoderReranker(
        base_url="https://reranker.example.test",
        model="qwen3-reranker-0.6b",
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
        sleep=lambda _seconds: None,
    )
    assert "authorization" not in unauthenticated._client.headers
    unauthenticated.close()


@pytest.mark.parametrize(
    "payload",
    (
        {"results": []},
        {"results": [{"index": 0, "relevance_score": 0.1}]},
        {"results": [{"index": 0}, {"index": 1, "relevance_score": 0.1}]},
        {"results": [{"index": 0, "relevance_score": 0.1}, {"index": 0, "relevance_score": 0.2}]},
        {"results": [{"index": 0, "relevance_score": 0.1}, {"index": 5, "relevance_score": 0.2}]},
        {"results": [{"index": 0, "relevance_score": 0.1}, {"index": 1, "relevance_score": None}]},
        {"unexpected": True},
    ),
)
def test_partial_or_fabricated_output_is_rejected(payload: object) -> None:
    stage = _stage(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(PlatformError) as error:
        stage.score("query", [_hit("a"), _hit("b")])
    assert error.value.code == "reranker_invalid_output"
    stage.close()


def test_timeout_and_rate_limit_classification() -> None:
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out")

    stage = _stage(timeout_handler)
    with pytest.raises(PlatformError) as error:
        stage.score("query", [_hit("a")])
    assert error.value.code == "reranker_timeout"
    assert error.value.status_code == 504
    stage.close()

    stage = _stage(lambda request: httpx.Response(429, text="rate"))
    with pytest.raises(PlatformError) as error:
        stage.score("query", [_hit("a")])
    assert error.value.code == "reranker_rate_limited"
    stage.close()

    stage = _stage(lambda request: httpx.Response(500, text="down"))
    with pytest.raises(PlatformError) as error:
        stage.score("query", [_hit("a")])
    assert error.value.code == "reranker_unavailable"
    stage.close()


def test_two_stage_orchestration_over_two_remote_models() -> None:
    requests: list[dict[str, object]] = []

    def coarse_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests.append({"model": body["model"], "documents": list(body["documents"])})
        # Coarse ranks h2 above h1; both libraries keep their candidates.
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": index, "relevance_score": 1.0 - index * 0.1}
                    for index in range(len(body["documents"]))
                ]
            },
        )

    def final_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests.append({"model": body["model"], "documents": list(body["documents"])})
        # Final is the only score that survives: h1 wins, h0 is filtered by threshold.
        final_scores = {"text h1": 0.9, "text h2": 0.8, "text h0": 0.1}
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": index, "relevance_score": final_scores[text]}
                    for index, text in enumerate(body["documents"])
                ]
            },
        )

    reranker = TwoStageReranker(
        release=RerankerRelease(
            provider="vllm",
            coarse_model="qwen3-reranker-0.6b",
            coarse_revision="r1",
            final_model="qwen3-reranker-8b",
            final_revision="r2",
            quantization="int8",
            tokenizer_version="tok-v1",
            candidate_limit=DEFAULT_LIBRARY_CANDIDATE_LIMIT,
            coarse_keep_per_library=DEFAULT_COARSE_KEEP_PER_LIBRARY,
            score_threshold=0.5,
        ),
        coarse_model=_stage(coarse_handler, model="qwen3-reranker-0.6b"),
        final_model=_stage(final_handler, model="qwen3-reranker-8b"),
    )
    hits = [_hit("h0", "dense"), _hit("h1", "dense"), _hit("h2", "sparse")]
    reranked, degradation = reranker.rerank("query", hits, RetrievalProfile())
    assert degradation is None
    assert [hit.chunk.chunk_id for hit in reranked] == ["h1", "h2"]
    assert all(hit.rerank_score is not None for hit in reranked)
    assert [entry["model"] for entry in requests] == [
        "qwen3-reranker-0.6b",
        "qwen3-reranker-8b",
    ]
    reranker._coarse.close()
    reranker._final.close()


def test_final_stage_transport_failure_keeps_existing_degradation_semantics() -> None:
    def coarse_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": index, "relevance_score": 0.5}
                    for index in range(len(body["documents"]))
                ]
            },
        )

    reranker = TwoStageReranker(
        release=RerankerRelease(
            provider="vllm",
            coarse_model="qwen3-reranker-0.6b",
            coarse_revision="r1",
            final_model="qwen3-reranker-8b",
            final_revision="r2",
            quantization="int8",
            tokenizer_version="tok-v1",
            candidate_limit=DEFAULT_LIBRARY_CANDIDATE_LIMIT,
            coarse_keep_per_library=DEFAULT_COARSE_KEEP_PER_LIBRARY,
            score_threshold=0.5,
        ),
        coarse_model=_stage(coarse_handler, model="qwen3-reranker-0.6b"),
        final_model=_stage(lambda request: httpx.Response(503, text="down")),
    )
    hits = [_hit("h0"), _hit("h1")]
    reranked, degradation = reranker.rerank("query", hits, RetrievalProfile())
    assert [hit.chunk.chunk_id for hit in reranked] == ["h0", "h1"]
    assert degradation is not None
    assert degradation["code"] == "rerank_degraded"
    assert degradation["reason"] == "final_unavailable"
    assert degradation["detail"] == "reranker_unavailable"
    assert degradation["threshold"] == "not_applied"
    reranker._coarse.close()
    reranker._final.close()


def test_close_releases_the_long_lived_client() -> None:
    stage = _stage(lambda request: httpx.Response(200, json={}))
    client = stage._client
    stage.close()
    assert client.is_closed
