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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

MIN_CANDIDATE_CHARS = 1


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
