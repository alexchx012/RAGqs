from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.chat.budget import (
    EFFORT_CANDIDATE_DOCUMENT_LIMITS,
    EFFORT_RAG_LIMITS,
    EFFORT_TOKEN_LIMITS,
    EFFORT_WALL_LIMITS,
    BudgetMeter,
    BudgetPolicy,
    validate_budget_policies,
)
from app.platform.errors import PlatformError


def _policy(effort: str = "quick", *, pricer=lambda operation, tokens: 1.0) -> BudgetPolicy:
    return BudgetPolicy.for_effort(
        effort,
        price_version="prices-v1",
        max_estimated_cost_amount=10.0,
        pricer=pricer,
    )


def test_default_effort_tables_match_the_confirmed_policy() -> None:
    assert EFFORT_RAG_LIMITS == {"quick": 1, "think": 8, "deep": 10}
    assert EFFORT_WALL_LIMITS == {"quick": 20, "think": 60, "deep": 180}
    assert EFFORT_TOKEN_LIMITS == {"quick": 12_000, "think": 24_000, "deep": 48_000}
    assert EFFORT_CANDIDATE_DOCUMENT_LIMITS == {"quick": 5, "think": 7, "deep": 9}


def test_policy_requires_positive_cost_ceiling_price_version_and_pricer() -> None:
    with pytest.raises(PlatformError) as cost_error:
        BudgetPolicy.for_effort("quick", price_version="v1", max_estimated_cost_amount=0)
    assert cost_error.value.code == "startup_error"

    with pytest.raises(PlatformError) as price_error:
        BudgetPolicy.for_effort("quick", price_version=" ", max_estimated_cost_amount=1.0)
    assert price_error.value.code == "startup_error"

    with pytest.raises(PlatformError):
        validate_budget_policies({})


def test_logical_operation_counting_ignores_provider_fanout_and_retries() -> None:
    meter = BudgetMeter(policy=_policy("quick"))
    now = datetime.now(UTC)
    meter.reserve("retrieval", estimated_tokens=10, now=now)
    assert meter.rag_calls_used == 1
    exhausted = meter.gate("retrieval", estimated_tokens=1, now=now)
    assert exhausted == {"kind": "retrieval_degraded", "detail": {"reason": "budget_exhausted"}}
    # A non-RAG step never consumes the call count even when other gates allow it.
    meter = BudgetMeter(policy=_policy("think"), deadline=now + timedelta(seconds=30))
    for _ in range(10):
        meter.reserve("non_rag", estimated_tokens=1, now=now)
    assert meter.rag_calls_used == 0


def test_unpriceable_operation_returns_cost_unavailable_notice() -> None:
    meter = BudgetMeter(policy=_policy("quick", pricer=lambda operation, tokens: None))
    notice = meter.gate("retrieval", estimated_tokens=10, now=datetime.now(UTC))
    assert notice == {"kind": "retrieval_degraded", "detail": {"reason": "cost_unavailable"}}


def test_reservation_is_conservative_and_reconciliation_releases_unused_cost() -> None:
    meter = BudgetMeter(policy=_policy("quick"))
    now = datetime.now(UTC)
    reserved = meter.reserve("retrieval", estimated_tokens=100, now=now)
    assert reserved == 1.0
    assert meter.cost_reserved == 1.0
    meter.reconcile(reserved, actual_tokens=40, actual_cost=0.4)
    assert meter.cost_reserved == 0.0
    assert meter.cost_spent == 0.4
    assert meter.tokens_used == 140


def test_deadline_and_token_gates_block_outbound_calls_before_side_effects() -> None:
    now = datetime.now(UTC)
    expired = BudgetMeter(policy=_policy("quick"), deadline=now - timedelta(seconds=1))
    assert expired.gate("retrieval", estimated_tokens=1, now=now) is not None

    full = BudgetMeter(policy=_policy("quick"))
    full.tokens_consumed = 12_000
    assert full.gate("retrieval", estimated_tokens=1, now=now) is not None

    pending = BudgetMeter(policy=_policy("quick"))
    pending.tokens_reserved = 12_000
    assert pending.gate("retrieval", estimated_tokens=1, now=now) is not None


def test_effort_upgrade_is_limited_to_once_and_keeps_consumed_usage() -> None:
    meter = BudgetMeter(policy=_policy("quick"))
    now = datetime.now(UTC)
    meter.reserve("retrieval", estimated_tokens=10, now=now)
    assert meter.upgrade_policy(_policy("think")) is True
    assert meter.rag_calls_used == 1
    assert meter.tokens_used == 10
    assert meter.upgrade_policy(_policy("deep")) is False
    assert meter.policy.effort_level == "think"
