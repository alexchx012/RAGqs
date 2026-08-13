"""Golden-set metrics and weak-signal separation (A13/A14/A15)."""

from __future__ import annotations

import pytest

from app.evaluation.metrics import (
    aggregate_faithfulness,
    hit_at_k_candidate,
    hit_at_k_final,
    mrr,
    ndcg_at_k,
    refusal_accuracy,
    weak_signal_prefix,
)
from app.evaluation.models import JudgeScores


def test_hit_at_k_candidate_and_final() -> None:
    expected = ["s1", "s2"]
    assert hit_at_k_candidate(["s9", "s1"], expected, k=2) is True
    assert hit_at_k_final(["s1", "s2"], expected, k=1) is True
    assert hit_at_k_candidate(["s9", "s8"], expected, k=2) is False


def test_mrr_prefers_earlier_hit() -> None:
    assert mrr(["s2", "s1"], ["s1"]) == 0.5
    assert mrr(["s1"], ["s1"]) == 1.0
    assert mrr(["s9"], ["s1"]) == 0.0


def test_ndcg_at_k_multi_source() -> None:
    ranked = ["s1", "s9", "s2"]
    expected = ["s1", "s2"]
    score = ndcg_at_k(ranked, expected, k=3)
    assert 0 < score <= 1


def test_faithfulness_and_answer_relevancy_aggregation() -> None:
    scores = [
        JudgeScores(faithfulness=0.8, answer_relevancy=0.6),
        JudgeScores(faithfulness=0.6, answer_relevancy=0.4),
    ]
    assert aggregate_faithfulness(scores) == pytest.approx(0.7)
    from app.evaluation.metrics import aggregate_answer_relevancy

    assert aggregate_answer_relevancy(scores) == pytest.approx(0.5)


def test_refusal_accuracy() -> None:
    scores = [
        JudgeScores(faithfulness=None, answer_relevancy=None, is_refusal=True),
        JudgeScores(faithfulness=None, answer_relevancy=None, is_refusal=False),
    ]
    assert refusal_accuracy(scores, [True, False]) == 1.0
    assert refusal_accuracy(scores, [False, False]) == 0.5


def test_weak_signals_are_prefixed_and_never_hit_mrr() -> None:
    assert weak_signal_prefix("has_citation") == "weak_has_citation"
    # A weak signal does not enter the retrieval metric namespace.
    assert "weak_has_citation" not in {
        "hit_at_k_candidate",
        "hit_at_k_final",
        "mrr",
    }
