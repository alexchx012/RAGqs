"""Generation-side self-evaluation and bounded query rewrite.

For ``think`` and ``deep`` the agents layer runs a configured
relevance/self-evaluation step after each generated candidate. When the
candidate is unacceptable and the frozen tier still has RAG rounds plus open
budget gates, the evaluator derives a rewritten query (or bounded
sub-question); retrieval re-runs within the same frozen scope and the
candidate is regenerated and re-evaluated. ``quick`` skips the step entirely.

The evaluation judge itself is owned by the evaluation capability; agents
consumes this port's result only. Rejected drafts and rewrite diagnoses stay
internal execution facts.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.indexing.models import DEEP_RETRIEVAL_STRATEGIES, DeepRetrievalStrategy

MIN_CANDIDATE_CHARS = 1


@dataclass(frozen=True, slots=True)
class DeepRetrievalStrategyPlan:
    """Validated, tool-argument-free deep retrieval choices from the main model."""

    operations: tuple[DeepRetrievalStrategy, ...]

    def __post_init__(self) -> None:
        if any(operation not in DEEP_RETRIEVAL_STRATEGIES for operation in self.operations):
            raise ValueError("deep retrieval strategy is invalid")
        if len(set(self.operations)) != len(self.operations):
            raise ValueError("deep retrieval strategies must be unique")
        granularities = {"sub_chunk", "parent_document", "document_summary"}
        if sum(operation in granularities for operation in self.operations) > 1:
            raise ValueError("deep retrieval granularity is ambiguous")

    @classmethod
    def from_model_content(cls, content: str) -> DeepRetrievalStrategyPlan:
        """Accept the small JSON contract and deliberately discard model rationale."""

        try:
            value = json.loads(content)
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("deep retrieval plan must be JSON") from error
        if not isinstance(value, Mapping) or set(value) != {"strategies"}:
            raise ValueError("deep retrieval plan shape is invalid")
        raw_operations = value["strategies"]
        if not isinstance(raw_operations, list) or any(
            not isinstance(operation, str) for operation in raw_operations
        ):
            raise ValueError("deep retrieval strategies are invalid")
        return cls(tuple(raw_operations))


@dataclass(frozen=True, slots=True)
class SelfEvaluationResult:
    acceptable: bool
    rewritten_query: str | None = None
    diagnosis: Mapping[str, Any] = field(default_factory=dict)


class SelfEvaluationPort(Protocol):
    """Relevance/self-evaluation step consumed from the evaluation side."""

    def evaluate(
        self,
        *,
        query: str,
        candidate_content: str,
        citations: Sequence[Mapping[str, Any]],
        context_items: Sequence[Mapping[str, Any]],
    ) -> SelfEvaluationResult: ...


class AcceptingSelfEvaluationPort:
    """Degenerate evaluator: every candidate passes (used for ``quick``)."""

    def evaluate(
        self,
        *,
        query: str,
        candidate_content: str,
        citations: Sequence[Mapping[str, Any]],
        context_items: Sequence[Mapping[str, Any]],
    ) -> SelfEvaluationResult:
        del query, candidate_content, citations, context_items
        return SelfEvaluationResult(acceptable=True)


class HeuristicSelfEvaluationPort:
    """Deterministic default evaluator until the judge port is wired.

    A candidate is acceptable when it is non-empty and either grounded on at
    least one citation or produced without any retrieval hits (a legitimate
    direct/no_context outcome). A hit-backed candidate with no surviving
    citations is rejected as ungrounded; no rewrite is offered because
    re-running the same query cannot change citation visibility, so the
    bounded loop terminates instead of burning rounds.
    """

    def evaluate(
        self,
        *,
        query: str,
        candidate_content: str,
        citations: Sequence[Mapping[str, Any]],
        context_items: Sequence[Mapping[str, Any]],
    ) -> SelfEvaluationResult:
        del query
        if not candidate_content.strip():
            return SelfEvaluationResult(
                acceptable=False,
                diagnosis={"reason": "empty_candidate"},
            )
        if context_items and not citations:
            return SelfEvaluationResult(
                acceptable=False,
                rewritten_query=None,
                diagnosis={"reason": "ungrounded_candidate"},
            )
        return SelfEvaluationResult(acceptable=True)
