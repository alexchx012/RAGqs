from __future__ import annotations

from app.agents.selfeval import (
    AcceptingSelfEvaluationPort,
    HeuristicSelfEvaluationPort,
    SelfEvaluationResult,
)


def test_accepting_port_passes_everything() -> None:
    result = AcceptingSelfEvaluationPort().evaluate(
        query="q",
        candidate_content="",
        citations=[],
        context_items=[{"snippet": "s"}],
    )
    assert result.acceptable is True


def test_heuristic_accepts_grounded_candidate() -> None:
    result = HeuristicSelfEvaluationPort().evaluate(
        query="q",
        candidate_content="answer",
        citations=[{"document_id": "d1"}],
        context_items=[{"document_id": "d1"}],
    )
    assert result.acceptable is True


def test_heuristic_accepts_hitless_direct_outcome() -> None:
    result = HeuristicSelfEvaluationPort().evaluate(
        query="q",
        candidate_content="answer",
        citations=[],
        context_items=[],
    )
    assert result.acceptable is True


def test_heuristic_rejects_ungrounded_candidate_without_rewrite() -> None:
    result = HeuristicSelfEvaluationPort().evaluate(
        query="q",
        candidate_content="hallucinated answer",
        citations=[],
        context_items=[{"document_id": "d1"}],
    )
    assert result.acceptable is False
    assert result.rewritten_query is None
    assert result.diagnosis["reason"] == "ungrounded_candidate"


def test_heuristic_rejects_empty_candidate() -> None:
    result = HeuristicSelfEvaluationPort().evaluate(
        query="q",
        candidate_content="  ",
        citations=[{"document_id": "d1"}],
        context_items=[{"document_id": "d1"}],
    )
    assert result.acceptable is False
    assert result.diagnosis["reason"] == "empty_candidate"


def test_result_is_immutable() -> None:
    result = SelfEvaluationResult(acceptable=True)
    assert result.rewritten_query is None
    assert dict(result.diagnosis) == {}
