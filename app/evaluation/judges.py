"""Faithfulness judge interfaces and implementations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from langchain_core.messages import HumanMessage, SystemMessage

from app.evaluation.models import AgentRunResult, FaithfulnessVerdict, GoldenExample
from app.providers.contracts import ChatModelProvider


@runtime_checkable
class FaithfulnessJudge(Protocol):
    """Answer groundedness judge boundary for local or LLM-backed evaluation."""

    def judge(self, example: GoldenExample, result: AgentRunResult) -> FaithfulnessVerdict:
        """Return a faithfulness verdict for one example result."""


@dataclass(frozen=True)
class StaticFaithfulnessJudge:
    """Deterministic judge for tests and fake-provider evaluation."""

    score: float = 1.0
    rationale: str = "static faithfulness verdict"
    passed: bool | None = None

    def judge(self, example: GoldenExample, result: AgentRunResult) -> FaithfulnessVerdict:
        passed = self.passed if self.passed is not None else self.score >= 1.0
        return FaithfulnessVerdict(score=self.score, passed=passed, rationale=self.rationale)


class ModelFaithfulnessJudge:
    """LLM-backed groundedness judge using the configured chat model provider."""

    def __init__(self, chat_model_provider: ChatModelProvider):
        self.chat_model_provider = chat_model_provider
        self._model: Any | None = None

    def judge(self, example: GoldenExample, result: AgentRunResult) -> FaithfulnessVerdict:
        model = self._get_model()
        response = model.invoke(self._build_messages(example, result))
        content = getattr(response, "content", response)

        try:
            payload = json.loads(str(content).strip())
            score = float(payload["score"])
            passed = payload["passed"]
            if not isinstance(passed, bool):
                raise ValueError("passed must be a boolean")
            return FaithfulnessVerdict(
                score=score,
                passed=passed,
                rationale=str(payload.get("rationale", "")),
            )
        except Exception as exc:
            return FaithfulnessVerdict(
                score=0.0,
                passed=False,
                rationale=f"invalid faithfulness judge response: {exc}",
            )

    def _get_model(self) -> Any:
        if self._model is None:
            self._model = self.chat_model_provider.create_chat_model(streaming=False)
        return self._model

    def _build_messages(
        self,
        example: GoldenExample,
        result: AgentRunResult,
    ) -> list[SystemMessage | HumanMessage]:
        expected_sources = _json_text(example.expected_sources)
        retrieved_sources = _json_text(result.retrieved_sources)
        cited_sources = _json_text(result.cited_sources)
        return [
            SystemMessage(
                content=(
                    "You are a strict RAG evaluation judge. Decide whether the "
                    "answer is grounded in the retrieved or cited sources. "
                    "Return JSON only with keys score, passed, and rationale."
                )
            ),
            HumanMessage(
                content=(
                    f"Question:\n{example.question}\n\n"
                    f"Answer:\n{result.answer}\n\n"
                    f"Expected sources:\n{expected_sources}\n\n"
                    f"Retrieved sources:\n{retrieved_sources}\n\n"
                    f"Cited sources:\n{cited_sources}\n\n"
                    "Use a score from 0.0 to 1.0. Pass only when the answer is "
                    "supported by the available source metadata."
                )
            ),
        ]


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
