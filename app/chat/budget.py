"""Per-generation budget policy and deterministic candidate selection."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from .models import RetrievalHitOutcome

RAG_BUDGET_POLICY_VERSION = "chat-rag-budget-v2"

# quick/think/deep RAG round caps (a round is one retrieval + generation pass).
_CANDIDATE_LIMITS = {"quick": 5, "think": 7, "deep": 9}
EFFORT_RAG_LIMITS: dict[str, int] = {"quick": 1, "think": 4, "deep": 10}
EFFORT_CANDIDATE_LIMITS: dict[str, int] = _CANDIDATE_LIMITS

# Effort may only be upgraded by one level when needed.
_UPGRADE_CHAIN = {"quick": "think", "think": "deep"}
EFFORT_UPGRADE_CHAIN = _UPGRADE_CHAIN


@dataclass(slots=True)
class GenerationBudget:
    effort_level: str
    rag_calls_used: int = 0

    @property
    def policy_version(self) -> str:
        return RAG_BUDGET_POLICY_VERSION

    @property
    def rag_calls_remaining(self) -> int:
        return max(EFFORT_RAG_LIMITS[self.effort_level] - self.rag_calls_used, 0)

    def can_start_rag_round(self) -> bool:
        return self.rag_calls_remaining > 0

    def record_rag_round(self) -> None:
        if not self.can_start_rag_round():
            raise ValueError("rag budget exhausted")
        self.rag_calls_used += 1

    def upgrade_effort(self) -> str | None:
        """Upgrade by at most one level; returns the new level or None."""

        upgraded = _UPGRADE_CHAIN.get(self.effort_level)
        if upgraded is None:
            return None
        self.effort_level = upgraded
        return upgraded

    def to_checkpoint(self) -> dict[str, object]:
        return {
            "effort_level": self.effort_level,
            "rag_calls_used": self.rag_calls_used,
            "policy_version": self.policy_version,
        }

    @classmethod
    def from_checkpoint(
        cls, effort_level: str, checkpoint: dict[str, object] | None
    ) -> GenerationBudget:
        budget = cls(effort_level=effort_level)
        if not checkpoint:
            return budget
        budget.rag_calls_used = int(str(checkpoint.get("rag_calls_used", 0)))
        return budget


def select_budget_candidates(
    hits: Iterable[RetrievalHitOutcome], *, limit: int
) -> tuple[tuple[RetrievalHitOutcome, ...], tuple[dict[str, str], ...]]:
    """Return the stable tree-search subset and missing-identity degradations."""

    if limit <= 0:
        raise ValueError("limit must be positive")
    ranked = sorted(
        hits,
        key=lambda hit: (
            -(hit.rerank_score if hit.rerank_score is not None else 0.0),
            hit.document_id,
            hit.chunk_id,
        ),
    )
    selected: list[RetrievalHitOutcome] = []
    documents: set[str] = set()
    missing_identity = False
    for hit in ranked:
        if not hit.document_id:
            missing_identity = True
            continue
        if hit.document_id in documents:
            continue
        if len(selected) == limit:
            continue
        selected.append(hit)
        documents.add(hit.document_id)
    degradations = ({"code": "missing_document_identity"},) if missing_identity else ()
    return tuple(selected), degradations


def conservative_chat_token_estimate(content: str, snippets: Iterable[str | None]) -> int:
    """Conservatively map request/context characters to model tokens."""

    source_characters = len(content) + sum(len(snippet or "") for snippet in snippets)
    return math.ceil(source_characters * 1.1) + 2000
