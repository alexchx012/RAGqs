"""Deterministic evaluation metrics for RAG regression checks."""

from __future__ import annotations

from statistics import mean
from typing import Any

from app.evaluation.models import AgentRunResult, EvaluationMetrics, EvaluationReport, GoldenExample

_SOURCE_KEYS = (
    "documentId",
    "document_id",
    "chunkId",
    "chunk_id",
    "fileName",
    "file_name",
    "sourcePath",
    "source_path",
)

_REFUSAL_MARKERS = (
    "not enough information",
    "cannot answer",
    "no relevant information",
    "不知道",
    "没有相关信息",
    "无法回答",
    "知识库中没有",
)


def evaluate_results(
    examples: list[GoldenExample],
    results: list[AgentRunResult],
) -> EvaluationReport:
    """Compare captured run results against golden expectations."""

    result_by_id = {result.example_id: result for result in results}
    retrieval_scores: list[float] = []
    answer_trait_scores: list[float] = []
    faithfulness_scores: list[float] = []
    citation_scores: list[float] = []
    refusal_scores: list[float] = []
    latencies: list[float] = []
    failures: list[dict[str, Any]] = []

    for example in examples:
        result = result_by_id.get(example.id, AgentRunResult(exampleId=example.id))
        latencies.append(result.latency_ms)
        issues: list[str] = []
        citation_miss = False
        faithfulness_miss = False

        if example.expected_sources:
            retrieved_values = _source_values(result.retrieved_sources or result.cited_sources)
            cited_values = _source_values(result.cited_sources or result.retrieved_sources)
            retrieval_hit = _has_any_expected_source(example.expected_sources, retrieved_values)
            citation_score = _expected_source_coverage(example.expected_sources, cited_values)
            retrieval_scores.append(1.0 if retrieval_hit else 0.0)
            citation_scores.append(citation_score)
            if not retrieval_hit:
                issues.append("retrieval_miss")
            if citation_score < 1.0:
                citation_miss = True

        if example.expected_answer_traits:
            trait_score = _answer_trait_coverage(result.answer, example.expected_answer_traits)
            answer_trait_scores.append(trait_score)
            if trait_score < 1.0:
                issues.append("answer_trait_miss")

        if result.faithfulness is not None:
            faithfulness_scores.append(result.faithfulness.score)
            if not result.faithfulness.passed:
                faithfulness_miss = True

        if faithfulness_miss:
            issues.append("faithfulness_miss")

        if citation_miss:
            issues.append("citation_miss")

        if example.expects_refusal:
            refused = result.refused or _looks_like_refusal(result.answer)
            refusal_scores.append(1.0 if refused else 0.0)
            if not refused:
                issues.append("refusal_miss")

        if issues:
            failures.append({"exampleId": example.id, "issues": issues})

    return EvaluationReport(
        metrics=EvaluationMetrics(
            totalExamples=len(examples),
            retrievalHitRate=_rounded_mean(retrieval_scores),
            answerTraitCoverage=_rounded_mean(answer_trait_scores),
            answerFaithfulness=_rounded_mean(faithfulness_scores),
            citationAccuracy=_rounded_mean(citation_scores),
            noAnswerRefusalRate=_rounded_mean(refusal_scores),
            averageLatencyMs=_rounded_mean(latencies),
        ),
        failures=failures,
    )


def _source_values(sources: list[dict[str, Any]]) -> set[str]:
    values: set[str] = set()
    for source in sources:
        for key in _SOURCE_KEYS:
            value = source.get(key)
            if value is None:
                continue
            text = str(value).strip().lower()
            if text:
                values.add(text)
                values.add(text.replace("\\", "/").rsplit("/", 1)[-1])
    return values


def _has_any_expected_source(expected_sources: list[str], source_values: set[str]) -> bool:
    return any(_normalize_expected_source(source) in source_values for source in expected_sources)


def _expected_source_coverage(expected_sources: list[str], source_values: set[str]) -> float:
    if not expected_sources:
        return 0.0
    matches = sum(
        1 for source in expected_sources if _normalize_expected_source(source) in source_values
    )
    return matches / len(expected_sources)


def _normalize_expected_source(source: str) -> str:
    return source.strip().lower().replace("\\", "/").rsplit("/", 1)[-1]


def _answer_trait_coverage(answer: str, expected_traits: list[str]) -> float:
    if not expected_traits:
        return 0.0
    normalized_answer = answer.lower()
    matches = sum(1 for trait in expected_traits if trait.lower() in normalized_answer)
    return matches / len(expected_traits)


def _looks_like_refusal(answer: str) -> bool:
    normalized_answer = answer.lower()
    return any(marker in normalized_answer for marker in _REFUSAL_MARKERS)


def _rounded_mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(mean(values), 4)
