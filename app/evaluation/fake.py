"""Deterministic fake evaluation runner."""

from __future__ import annotations

from app.evaluation.metrics import evaluate_results
from app.evaluation.models import (
    AgentRunResult,
    EvaluationReport,
    FaithfulnessVerdict,
    GoldenExample,
)


def run_fake_evaluation(examples: list[GoldenExample]) -> EvaluationReport:
    """Run a deterministic fake provider evaluation for harness regression."""

    results: list[AgentRunResult] = []
    for example in examples:
        if example.expects_refusal:
            answer = "知识库中没有相关信息，无法回答。"
            refused = True
        else:
            answer = " ".join(example.expected_answer_traits) or "fake answer"
            refused = False
        sources = [{"fileName": source} for source in example.expected_sources]
        results.append(
            AgentRunResult(
                exampleId=example.id,
                answer=answer,
                retrievedSources=sources,
                citedSources=sources,
                latencyMs=0.0,
                refused=refused,
                faithfulness=FaithfulnessVerdict(
                    score=1.0,
                    passed=True,
                    rationale="deterministic fake result",
                ),
            )
        )
    return evaluate_results(examples, results)
