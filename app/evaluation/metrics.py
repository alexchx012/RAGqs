"""Retrieval and generation metric primitives for shadow evaluation.

Retrieval metrics are computed against golden ``expected_sources`` labels;
generation metrics are returned by the judge and only aggregated/bounded here.
Weak signals are always prefixed ``weak_`` and never masquerade as hit/MRR.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .models import JudgeScores


def _source_ids(sources: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for source in sources:
        if isinstance(source, Mapping):
            value = source.get("source_id", source.get("document_id", source.get("id")))
        else:
            value = source
        if value is None:
            continue
        item = str(value)
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def hit_at_k_candidate(
    candidate_sources: Iterable[Any], expected_sources: Iterable[Any], k: int
) -> bool:
    expected = _source_ids(expected_sources)
    if not expected:
        return False
    return any(source in expected for source in _source_ids(candidate_sources)[:k])


def hit_at_k_final(final_sources: Iterable[Any], expected_sources: Iterable[Any], k: int) -> bool:
    return hit_at_k_candidate(final_sources, expected_sources, k)


def reciprocal_rank(final_sources: Sequence[Any], expected_sources: Iterable[Any]) -> float:
    expected = _source_ids(expected_sources)
    if not expected:
        return 0.0
    for index, source in enumerate(_source_ids(final_sources), start=1):
        if source in expected:
            return 1.0 / index
    return 0.0


def mrr(final_sources: Sequence[Any], expected_sources: Iterable[Any]) -> float:
    """Mean reciprocal rank; single-sample callers pass a single ranking."""
    return reciprocal_rank(final_sources, expected_sources)


def ndcg_at_k(
    ranked_sources: Sequence[Any],
    expected_sources: Iterable[Any],
    k: int,
) -> float:
    """Multi-source nDCG@k with binary relevance against the golden source set."""
    expected = _source_ids(expected_sources)
    if not expected or k < 1:
        return 0.0
    ideal_count = min(len(expected), k)
    ideal_dcg = sum(1.0 / math.log2(2 + index) for index in range(ideal_count))
    if ideal_dcg <= 0:
        return 0.0
    dcg = 0.0
    seen: set[str] = set()
    for index, source in enumerate(_source_ids(ranked_sources)[:k], start=1):
        if source in seen:
            continue
        seen.add(source)
        if source in expected:
            dcg += 1.0 / math.log2(1 + index)
    return dcg / ideal_dcg


def aggregate_mean(values: Iterable[float | None]) -> float:
    numbers = [float(value) for value in values if value is not None]
    if not numbers:
        return 0.0
    return sum(numbers) / len(numbers)


def clamp_score(value: float | None) -> float | None:
    if value is None:
        return None
    if math.isnan(value):
        return 0.0
    return max(0.0, min(1.0, float(value)))


def aggregate_faithfulness(scores: Iterable[JudgeScores]) -> float:
    return aggregate_mean(clamp_score(score.faithfulness) for score in scores)


def aggregate_answer_relevancy(scores: Iterable[JudgeScores]) -> float:
    return aggregate_mean(clamp_score(score.answer_relevancy) for score in scores)


def refusal_accuracy(scores: Iterable[JudgeScores], expected_refusal: Iterable[bool]) -> float:
    """Accuracy on golden ``expects_refusal`` samples."""
    pairs = list(zip(list(scores), list(expected_refusal), strict=False))
    if not pairs:
        return 1.0
    matched = sum(1 for score, expected in pairs if score.is_refusal == bool(expected))
    return matched / len(pairs)


def weak_signal_prefix(name: str) -> str:
    return f"weak_{name}"


__all__ = [
    "aggregate_answer_relevancy",
    "aggregate_faithfulness",
    "aggregate_mean",
    "clamp_score",
    "hit_at_k_candidate",
    "hit_at_k_final",
    "mrr",
    "ndcg_at_k",
    "reciprocal_rank",
    "refusal_accuracy",
    "weak_signal_prefix",
]
