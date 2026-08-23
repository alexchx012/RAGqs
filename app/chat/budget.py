"""Per-generation budget policy: logical RAG operations and resource gates."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.platform.errors import PlatformError

RAG_BUDGET_POLICY_VERSION = "chat-rag-budget-v2"

# quick/think/deep logical RAG operation caps.
EFFORT_RAG_LIMITS: dict[str, int] = {"quick": 1, "think": 4, "deep": 10}
EFFORT_WALL_LIMITS: dict[str, int] = {"quick": 20, "think": 60, "deep": 180}
EFFORT_TOKEN_LIMITS: dict[str, int] = {"quick": 12_000, "think": 24_000, "deep": 48_000}
EFFORT_CANDIDATE_DOCUMENT_LIMITS: dict[str, int] = {"quick": 5, "think": 7, "deep": 9}

RAG_OPERATION_KINDS = ("retrieval", "rewrite", "tree")
BUDGET_REASONS = ("budget_exhausted", "cost_unavailable")

# Effort may only be upgraded by one level when needed.
_UPGRADE_CHAIN = {"quick": "think", "think": "deep"}


def default_pricer(_operation: str, _tokens: int) -> float | None:
    """No production deployment may use this: it cannot estimate cost."""

    return None


@dataclass(frozen=True, slots=True)
class BudgetPolicy:
    """Versioned per-effort gates; every deployment must price operations."""

    effort_level: str
    max_rag_calls: int
    max_wall_seconds: int
    max_total_tokens: int
    max_estimated_cost_amount: float
    price_version: str
    pricer: Callable[[str, int], float | None] = default_pricer

    def __post_init__(self) -> None:
        if self.effort_level not in EFFORT_RAG_LIMITS:
            raise PlatformError("validation_error", "budget effort level is invalid", {}, 422)
        if self.max_rag_calls < 1 or self.max_wall_seconds < 1 or self.max_total_tokens < 1:
            raise PlatformError("validation_error", "budget limits must be positive", {}, 422)
        if self.max_estimated_cost_amount <= 0:
            raise PlatformError("startup_error", "budget cost ceiling must be positive", {}, 500)
        if not self.price_version.strip():
            raise PlatformError("startup_error", "budget price version is required", {}, 500)
        if not callable(self.pricer):
            raise PlatformError("startup_error", "budget pricer is required", {}, 500)

    @classmethod
    def for_effort(
        cls,
        effort_level: str,
        *,
        price_version: str,
        max_estimated_cost_amount: float,
        pricer: Callable[[str, int], float | None] = default_pricer,
    ) -> BudgetPolicy:
        if effort_level not in EFFORT_RAG_LIMITS:
            raise PlatformError("validation_error", "budget effort level is invalid", {}, 422)
        return cls(
            effort_level=effort_level,
            max_rag_calls=EFFORT_RAG_LIMITS[effort_level],
            max_wall_seconds=EFFORT_WALL_LIMITS[effort_level],
            max_total_tokens=EFFORT_TOKEN_LIMITS[effort_level],
            max_estimated_cost_amount=max_estimated_cost_amount,
            price_version=price_version,
            pricer=pricer,
        )


def validate_budget_policies(policies: Mapping[str, Any]) -> None:
    """Startup gate: every active effort needs limits, a price version and a pricer."""

    for effort in EFFORT_RAG_LIMITS:
        policy = policies.get(effort)
        if policy is None:
            raise PlatformError("startup_error", f"missing budget policy for {effort}", {}, 500)
        if not isinstance(policy, BudgetPolicy):
            raise PlatformError("startup_error", f"invalid budget policy for {effort}", {}, 500)


@dataclass(slots=True)
class GenerationBudget:
    effort_level: str
    rag_calls_used: int = 0
    upgraded_once: bool = False

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

        if self.upgraded_once:
            return None
        upgraded = _UPGRADE_CHAIN.get(self.effort_level)
        if upgraded is None:
            return None
        self.effort_level = upgraded
        self.upgraded_once = True
        return upgraded

    def to_checkpoint(self) -> dict[str, object]:
        return {
            "effort_level": self.effort_level,
            "rag_calls_used": self.rag_calls_used,
            "upgraded_once": self.upgraded_once,
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
        budget.upgraded_once = bool(checkpoint.get("upgraded_once", False))
        return budget


@dataclass(slots=True)
class BudgetMeter:
    """Logical-operation gate plus wall/token/cost reservation and reconciliation."""

    policy: BudgetPolicy
    rag_calls_used: int = 0
    tokens_used: int = 0
    tokens_consumed: int = 0
    tokens_reserved: int = 0
    cost_reserved: float = 0.0
    cost_spent: float = 0.0
    deadline: datetime | None = None
    upgraded_once: bool = False
    _pending_reservations: list[tuple[float, int]] = field(default_factory=list, repr=False)

    @property
    def rag_calls_remaining(self) -> int:
        return max(self.policy.max_rag_calls - self.rag_calls_used, 0)

    @staticmethod
    def budget_notice(reason: str) -> Mapping[str, Any]:
        if reason not in BUDGET_REASONS:
            raise ValueError("invalid budget reason")
        return {"kind": "retrieval_degraded", "detail": {"reason": reason}}

    def gate(
        self,
        operation: str,
        *,
        estimated_tokens: int,
        now: datetime,
    ) -> Mapping[str, Any] | None:
        """Return a public budget notice when the operation must not go outbound."""

        logical = operation if operation in RAG_OPERATION_KINDS else "non_rag"
        if logical != "non_rag" and self.rag_calls_used >= self.policy.max_rag_calls:
            return self.budget_notice("budget_exhausted")
        if self.deadline is not None and now >= self.deadline:
            return self.budget_notice("budget_exhausted")
        if (
            self.tokens_consumed + self.tokens_reserved + max(estimated_tokens, 0)
            > self.policy.max_total_tokens
        ):
            return self.budget_notice("budget_exhausted")
        estimate = self._price(operation, estimated_tokens)
        if estimate is None:
            return self.budget_notice("cost_unavailable")
        if self.cost_reserved + estimate > self.policy.max_estimated_cost_amount:
            return self.budget_notice("budget_exhausted")
        return None

    def reserve(
        self,
        operation: str,
        *,
        estimated_tokens: int,
        now: datetime,
    ) -> float:
        """Conservatively reserve cost before the provider call goes outbound."""

        notice = self.gate(operation, estimated_tokens=estimated_tokens, now=now)
        if notice is not None:
            raise PlatformError(
                "budget_exhausted",
                "the operation exceeds the active budget policy",
                dict(notice),
                429,
            )
        if operation in RAG_OPERATION_KINDS:
            self.rag_calls_used += 1
        token_reservation = max(estimated_tokens, 0)
        # Keep the historical ``tokens_used`` field as the observable
        # reservation-plus-usage ledger; gate decisions use the reconciled
        # ``tokens_consumed`` value so an estimate is not charged twice.
        self.tokens_used += token_reservation
        self.tokens_reserved += token_reservation
        estimate = self._price(operation, estimated_tokens)
        assert estimate is not None
        self.cost_reserved += estimate
        self._pending_reservations.append((estimate, token_reservation))
        return estimate

    def reconcile(self, reserved: float, *, actual_tokens: int, actual_cost: float) -> None:
        """Release unused reservation and record actual usage after completion."""

        actual = max(actual_tokens, 0)
        for index, (pending_cost, pending_tokens) in enumerate(self._pending_reservations):
            if pending_cost == reserved:
                self.tokens_reserved = max(self.tokens_reserved - pending_tokens, 0)
                del self._pending_reservations[index]
                break
        self.tokens_used += actual
        self.tokens_consumed += actual
        self.cost_reserved = max(self.cost_reserved - reserved, 0.0)
        self.cost_spent += max(actual_cost, 0.0)

    def upgrade_policy(self, policy: BudgetPolicy, *, reset_usage: bool = False) -> bool:
        """Upgrade at most one level; consumed usage is never reset in practice."""

        if self.upgraded_once or policy.effort_level == self.policy.effort_level:
            return False
        if _UPGRADE_CHAIN.get(self.policy.effort_level) != policy.effort_level:
            return False
        self.policy = policy
        self.upgraded_once = True
        if reset_usage:
            self.rag_calls_used = 0
            self.tokens_used = 0
        return True

    def _price(self, operation: str, tokens: int) -> float | None:
        try:
            estimate = self.policy.pricer(operation, max(tokens, 0))
        except Exception:
            return None
        if estimate is None:
            return None
        try:
            value = float(estimate)
        except (TypeError, ValueError):
            return None
        return value if value >= 0 else None

    def to_checkpoint(self) -> dict[str, object]:
        return {
            "effort_level": self.policy.effort_level,
            "rag_calls_used": self.rag_calls_used,
            "tokens_used": self.tokens_used,
            "tokens_consumed": self.tokens_consumed,
            "tokens_reserved": self.tokens_reserved,
            "cost_reserved": self.cost_reserved,
            "cost_spent": self.cost_spent,
            "upgraded_once": self.upgraded_once,
            "policy_version": RAG_BUDGET_POLICY_VERSION,
            "price_version": self.policy.price_version,
        }
