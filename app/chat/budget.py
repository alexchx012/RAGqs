"""Per-generation budget policy: RAG round limits."""

from __future__ import annotations

from dataclasses import dataclass

RAG_BUDGET_POLICY_VERSION = "chat-rag-budget-v1"

# quick/think/deep RAG round caps (a round is one retrieval + generation pass).
EFFORT_RAG_LIMITS: dict[str, int] = {"quick": 1, "think": 4, "deep": 10}

# Effort may only be upgraded by one level when needed.
_UPGRADE_CHAIN = {"quick": "think", "think": "deep"}


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
