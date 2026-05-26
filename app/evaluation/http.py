"""HTTP evaluation client for a running FastAPI RAG service."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.request import Request, urlopen

from app.evaluation.context import example_space_id
from app.evaluation.judges import FaithfulnessJudge
from app.evaluation.metrics import evaluate_results
from app.evaluation.models import AgentRunResult, EvaluationReport, GoldenExample

PostJson = Callable[[str, dict[str, Any], float], dict[str, Any]]


@dataclass(frozen=True)
class HttpRagEvaluationClient:
    """Small client for evaluating a running `/chat` endpoint."""

    base_url: str
    post_json: PostJson | None = None
    timeout_seconds: float = 30.0

    def query_with_trace(
        self,
        question: str,
        session_id: str,
        space_id: str = "default",
    ) -> dict[str, Any]:
        payload = {"Id": session_id, "Question": question}
        if space_id != "default":
            payload["spaceId"] = space_id
        response = self._post_json(
            self._url("/chat"),
            payload,
            self.timeout_seconds,
        )
        data = response.get("data", {})
        if data.get("success") is False:
            raise RuntimeError(data.get("errorMessage") or response.get("message") or "chat failed")
        retrieval = data.get("retrieval") or {}
        sources = data.get("sources") or retrieval.get("sources") or []
        return {
            "answer": data.get("answer") or "",
            "sources": sources,
            "retrieval": retrieval,
        }

    def _post_json(self, url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        if self.post_json is not None:
            return self.post_json(url, payload, timeout)
        return _stdlib_post_json(url, payload, timeout)

    def _url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"


def run_http_evaluation(
    examples: list[GoldenExample],
    client: HttpRagEvaluationClient,
    *,
    session_id_prefix: str = "eval",
    clock: Callable[[], float] = time.perf_counter,
    faithfulness_judge: FaithfulnessJudge | None = None,
) -> EvaluationReport:
    """Run evaluation against a running FastAPI service."""

    results: list[AgentRunResult] = []
    for example in examples:
        session_id = f"{session_id_prefix}-{example.id}"
        space_id = example_space_id(example)
        started_at = clock()
        trace = client.query_with_trace(
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


def _stdlib_post_json(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))
