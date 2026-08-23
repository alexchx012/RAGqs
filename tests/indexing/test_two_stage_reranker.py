from __future__ import annotations

from app.indexing import RetrievalHit, RetrievalProfile
from app.indexing.models import IndexChunk
from app.indexing.rerank import (
    DEFAULT_LIBRARY_CANDIDATE_LIMIT,
    RerankerRelease,
    StubRerankerModel,
    TwoStageReranker,
)
from app.platform.errors import PlatformError


def _hit(chunk_id: str, source: str, score: float = 0.5) -> RetrievalHit:
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
        score=score,
        source=source,
    )


def _release(**overrides: object) -> RerankerRelease:
    values: dict[str, object] = {
        "provider": "qwen3",
        "coarse_model": "Qwen3-Reranker-0.6B",
        "coarse_revision": "r1",
        "final_model": "Qwen3-Reranker-8B",
        "final_revision": "r2",
        "quantization": "INT8",
        "tokenizer_version": "tok-v1",
        "candidate_limit": DEFAULT_LIBRARY_CANDIDATE_LIMIT,
        "coarse_keep_per_library": 25,
        "score_threshold": None,
    }
    values.update(overrides)
    return RerankerRelease(**values)  # type: ignore[arg-type]


class FixedModel:
    def __init__(self, scores: tuple[float, ...], *, name: str = "qwen3") -> None:
        self.provider_name = name
        self._scores = scores
        self.calls: list[tuple[str, int]] = []

    def score(self, query: str, hits: list[RetrievalHit]) -> tuple[float, ...]:
        self.calls.append((query, len(hits)))
        return self._scores[: len(hits)]


class FailingModel:
    provider_name = "qwen3"

    def score(self, query: str, hits: list[RetrievalHit]) -> tuple[float, ...]:
        raise RuntimeError("provider down")


def test_each_library_is_truncated_before_coarse_and_final_stages() -> None:
    dense = [_hit(f"d{i}", "dense") for i in range(60)]
    sparse = [_hit(f"s{i}", "sparse") for i in range(60)]
    coarse = FixedModel(tuple(1.0 for _ in range(120)))
    final = FixedModel(tuple(0.9 for _ in range(60)))
    reranker = TwoStageReranker(
        release=_release(candidate_limit=45, coarse_keep_per_library=25),
        coarse_model=coarse,
        final_model=final,
    )
    hits, degradation = reranker.rerank("query", dense + sparse, RetrievalProfile())
    assert degradation is None
    assert coarse.calls[0][1] == 90  # 45 per library
    assert final.calls[0][1] == 50  # 25 per library
    assert len(hits) == 50


def test_final_stage_is_the_only_score_used_for_threshold() -> None:
    hits = [_hit(f"h{i}", "dense") for i in range(4)]
    reranker = TwoStageReranker(
        release=_release(score_threshold=0.5, candidate_limit=40, coarse_keep_per_library=20),
        coarse_model=FixedModel((0.99, 0.99, 0.99, 0.99)),
        final_model=FixedModel((0.9, 0.1, 0.8, 0.05)),
    )
    reranked, degradation = reranker.rerank("query", hits, RetrievalProfile())
    assert degradation is None
    assert [hit.chunk.chunk_id for hit in reranked] == ["h0", "h2"]
    assert all(hit.rerank_score is not None for hit in reranked)


def test_provider_failure_preserves_entering_order_without_threshold() -> None:
    hits = [_hit(f"h{i}", "dense") for i in range(5)]
    reranker = TwoStageReranker(
        release=_release(score_threshold=0.5),
        coarse_model=FixedModel(tuple(1.0 for _ in range(5))),
        final_model=FailingModel(),
    )
    reranked, degradation = reranker.rerank("query", hits, RetrievalProfile())
    assert [hit.chunk.chunk_id for hit in reranked] == [f"h{i}" for i in range(5)]
    assert degradation is not None
    assert degradation["code"] == "rerank_degraded"
    assert degradation["fallback"] == "preserve_candidate_order"
    assert degradation["threshold"] == "not_applied"
    assert all(hit.rerank_score is None for hit in reranked)


def test_stub_none_model_is_rejected_in_production_and_returns_no_scores() -> None:
    stub = StubRerankerModel(environment="production")
    try:
        stub.score("query", [_hit("h0", "dense")])
    except PlatformError as error:
        assert error.code == "reranker_unavailable"
        assert "RERANKER_PROVIDER=none" in str(error.message)
    else:
        raise AssertionError("production must reject the none provider")

    development = StubRerankerModel(environment="test")
    assert development.score("query", [_hit("h0", "dense")]) == ()
