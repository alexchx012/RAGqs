from __future__ import annotations

from app.chat.budget import conservative_chat_token_estimate, select_budget_candidates
from app.chat.models import RetrievalHitOutcome


def hit(
    document_id: str,
    chunk_id: str,
    *,
    rerank_score: float | None = None,
) -> RetrievalHitOutcome:
    return RetrievalHitOutcome(
        document_id=document_id,
        document_version_id=f"version-{document_id}-{chunk_id}",
        publication_id=f"publication-{document_id}-{chunk_id}",
        chunk_id=chunk_id,
        space_id="space-1",
        locator={"page": 1},
        snippet="snippet",
        rerank_score=rerank_score,
    )


def test_candidates_use_final_score_and_stable_document_dedupe() -> None:
    selected, degradations = select_budget_candidates(
        (
            hit("doc-b", "chunk-b1", rerank_score=0.8),
            hit("", "chunk-missing", rerank_score=1.0),
            hit("doc-a", "chunk-a2", rerank_score=0.9),
            hit("doc-a", "chunk-a1", rerank_score=0.9),
            hit("doc-c", "chunk-c1", rerank_score=0.95),
        ),
        limit=2,
    )

    assert [(item.document_id, item.chunk_id) for item in selected] == [
        ("doc-c", "chunk-c1"),
        ("doc-a", "chunk-a1"),
    ]
    assert [dict(item) for item in degradations] == [{"code": "missing_document_identity"}]


def test_candidates_keep_retrieval_order_when_scores_are_unavailable() -> None:
    selected, degradations = select_budget_candidates(
        (hit("doc-b", "chunk-b1"), hit("doc-a", "chunk-a2"), hit("doc-a", "chunk-a1")),
        limit=2,
    )

    assert [(item.document_id, item.chunk_id) for item in selected] == [
        ("doc-a", "chunk-a1"),
        ("doc-b", "chunk-b1"),
    ]
    assert degradations == ()


def test_chat_token_estimate_includes_request_context_and_safety_margin() -> None:
    assert conservative_chat_token_estimate("abcd", [None, "ef"]) == 2007
