"""Evaluation runner for traced RAG service implementations."""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable
from typing import Any, Protocol

from app.evaluation.context import example_space_id
from app.evaluation.judges import FaithfulnessJudge
from app.evaluation.metrics import evaluate_results
from app.evaluation.models import AgentRunResult, EvaluationReport, GoldenExample


class TracedRagService(Protocol):
    """Minimal service contract required for real-provider evaluation."""

    async def query_with_trace(
        self,
        question: str,
        session_id: str,
        space_id: str = "default",
    ) -> dict[str, Any]:
        """Return answer, sources, and retrieval trace for one question."""


async def run_service_evaluation(
    examples: list[GoldenExample],
    service: TracedRagService,
    *,
    session_id_prefix: str = "eval",
    clock: Callable[[], float] = time.perf_counter,
    faithfulness_judge: FaithfulnessJudge | None = None,
) -> EvaluationReport:
    """Run evaluation against a traced RAG service."""

    results: list[AgentRunResult] = []
    for example in examples:
        session_id = f"{session_id_prefix}-{example.id}"
        space_id = example_space_id(example)
        started_at = clock()
        trace = await _call_query_with_trace(
            service,
            example.question,
            session_id=session_id,
            space_id=space_id,
        )
        latency_ms = round((clock() - started_at) * 1000, 4)

        retrieval = trace.get("retrieval", {}) or {}
        sources = list(trace.get("sources") or retrieval.get("sources") or [])
        result = AgentRunResult(
            exampleId=example.id,
            answer=str(trace.get("answer", "")),
            retrievedSources=list(retrieval.get("sources") or sources),
            citedSources=sources,
            latencyMs=latency_ms,
            metadata={"sessionId": session_id, "spaceId": space_id, "retrieval": retrieval},
        )
        if faithfulness_judge is not None:
            result = result.model_copy(
                update={"faithfulness": faithfulness_judge.judge(example, result)}
            )
        results.append(result)

    return evaluate_results(examples, results)


async def _call_query_with_trace(
    service: TracedRagService,
    question: str,
    *,
    session_id: str,
    space_id: str,
) -> dict[str, Any]:
    method = service.query_with_trace
    if _accepts_keyword(method, "space_id"):
        return await method(question, session_id=session_id, space_id=space_id)
    return await method(question, session_id=session_id)


def _accepts_keyword(method: Any, keyword: str) -> bool:
    parameters = inspect.signature(method).parameters.values()
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD or parameter.name == keyword
        for parameter in parameters
    )
