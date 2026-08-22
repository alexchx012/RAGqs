from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Connection
from sqlalchemy.pool import StaticPool

from app.platform.errors import PlatformError
from app.usage.budget import (
    BudgetMeterPolicy,
    BudgetMeterService,
)
from app.usage.schema import (
    generation_budget_meter_table,
    generation_budget_reservation_table,
    usage_metadata,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


@dataclass
class MutableClock:
    now: datetime

    def now_utc(self, connection: Connection | None = None) -> datetime:
        del connection
        return self.now


def make_service() -> BudgetMeterService:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    usage_metadata.create_all(engine)
    policy = BudgetMeterPolicy.configured(
        price_version_id="price-1",
        currency_code="USD",
        max_estimated_cost_amounts={
            "quick": Decimal("0.100"),
            "think": Decimal("0.200"),
            "deep": Decimal("0.500"),
        },
        cost_estimator=lambda operation, tokens: Decimal("0.00001") * tokens,
    )
    return BudgetMeterService(engine, MutableClock(NOW), policy)


def test_production_policy_requires_cost_price_and_estimator() -> None:
    with pytest.raises(PlatformError) as failure:
        BudgetMeterPolicy.production(
            efforts={},
            price_version_id="",
            currency_code="USD",
            cost_estimator=None,
        )
    assert failure.value.code == "budget_policy_invalid"


def test_default_policy_locks_effort_wall_and_candidate_limits() -> None:
    service = make_service()
    limits = service.policy.efforts
    assert [
        (limits[item].max_rag_calls, limits[item].max_wall_seconds)
        for item in ("quick", "think", "deep")
    ] == [
        (1, 20),
        (4, 60),
        (10, 180),
    ]
    assert [limits[item].max_total_tokens for item in ("quick", "think", "deep")] == [
        12000,
        24000,
        48000,
    ]
    assert [limits[item].candidate_document_limit for item in ("quick", "think", "deep")] == [
        5,
        7,
        9,
    ]
    meter = service.ensure_meter(
        generation_id="gen-1",
        effort_level="quick",
        deadline_at_utc=NOW + timedelta(seconds=1800),
    )
    assert meter.deadline_at_utc == NOW + timedelta(seconds=20)
    assert meter.max_rag_calls == 1
    assert meter.max_total_tokens == 12000
    assert meter.max_estimated_cost_amount == Decimal("0.100")
    assert meter.candidate_document_limit == 5


def test_budget_reserve_settles_and_replays_idempotently() -> None:
    service = make_service()
    service.ensure_meter(
        generation_id="gen-1",
        effort_level="quick",
        deadline_at_utc=NOW + timedelta(seconds=20),
    )
    reservation = service.reserve(
        generation_id="gen-1",
        reservation_id="op-1",
        operation_kind="chat_generation",
        estimated_tokens=100,
        estimated_cost=Decimal("0.020"),
        is_rag=True,
        request_fingerprint="fp-1",
    )
    replay = service.reserve(
        generation_id="gen-1",
        reservation_id="op-1",
        operation_kind="chat_generation",
        estimated_tokens=100,
        estimated_cost=Decimal("0.020"),
        is_rag=True,
        request_fingerprint="fp-1",
    )
    with pytest.raises(PlatformError) as conflict:
        service.reserve(
            generation_id="gen-1",
            reservation_id="op-1",
            operation_kind="chat_generation",
            estimated_tokens=101,
            estimated_cost=Decimal("0.020"),
            is_rag=True,
            request_fingerprint="fp-1",
        )

    assert reservation.reservation_id == replay.reservation_id
    assert conflict.value.code == "budget_reservation_conflict"
    service.settle(
        generation_id="gen-1",
        reservation_id="op-1",
        actual_tokens=80,
        actual_cost=Decimal("0.015"),
    )
    row = service.meter(generation_id="gen-1")
    assert row.rag_calls_used == 1
    assert row.total_tokens_used == 80
    assert row.settled_cost_amount == Decimal("0.015")
    assert row.reserved_tokens == 0
    assert row.reserved_cost_amount == Decimal("0")
    assert row.status == "active"


def test_budget_gates_are_conservative_and_fail_closed() -> None:
    service = make_service()
    service.ensure_meter(
        generation_id="gen-1",
        effort_level="quick",
        deadline_at_utc=NOW + timedelta(seconds=20),
    )
    service.reserve(
        generation_id="gen-1",
        reservation_id="rag-1",
        operation_kind="rag_retrieval",
        estimated_tokens=0,
        estimated_cost=Decimal("0.001"),
        is_rag=True,
        request_fingerprint="rag-1",
    )

    with pytest.raises(PlatformError) as rag:
        service.reserve(
            generation_id="gen-1",
            reservation_id="rag-2",
            operation_kind="rag_retrieval",
            estimated_tokens=0,
            estimated_cost=Decimal("0"),
            is_rag=True,
            request_fingerprint="rag-2",
        )
    with pytest.raises(PlatformError) as cost:
        service.reserve(
            generation_id="gen-1",
            reservation_id="tool-1",
            operation_kind="tool",
            estimated_tokens=1,
            estimated_cost=None,
            is_rag=False,
            request_fingerprint="tool-1",
        )
    with pytest.raises(PlatformError) as tokens:
        service.reserve(
            generation_id="gen-1",
            reservation_id="tool-2",
            operation_kind="tool",
            estimated_tokens=12001,
            estimated_cost=Decimal("0"),
            is_rag=False,
            request_fingerprint="tool-2",
        )

    assert rag.value.code == "budget_exhausted"
    assert cost.value.code == "cost_unavailable"
    assert tokens.value.code == "budget_exhausted"


def test_budget_overage_keeps_actual_usage_and_blocks_later_calls() -> None:
    service = make_service()
    service.ensure_meter(
        generation_id="gen-1",
        effort_level="quick",
        deadline_at_utc=NOW + timedelta(seconds=20),
    )
    service.reserve(
        generation_id="gen-1",
        reservation_id="op-1",
        operation_kind="chat_generation",
        estimated_tokens=100,
        estimated_cost=Decimal("0.020"),
        is_rag=False,
        request_fingerprint="fp-1",
    )
    service.settle(
        generation_id="gen-1",
        reservation_id="op-1",
        actual_tokens=13000,
        actual_cost=Decimal("0.030"),
    )
    row = service.meter(generation_id="gen-1")
    assert row.total_tokens_used == 13000
    assert row.settled_cost_amount == Decimal("0.030")
    assert row.status == "exhausted"

    with pytest.raises(PlatformError) as failure:
        service.reserve(
            generation_id="gen-1",
            reservation_id="op-2",
            operation_kind="chat_generation",
            estimated_tokens=1,
            estimated_cost=Decimal("0"),
            is_rag=False,
            request_fingerprint="fp-2",
        )
    assert failure.value.code == "budget_exhausted"


def test_budget_upgrade_is_at_most_once_and_preserves_usage() -> None:
    service = make_service()
    service.ensure_meter(
        generation_id="gen-1",
        effort_level="quick",
        deadline_at_utc=NOW + timedelta(seconds=60),
    )
    service.reserve(
        generation_id="gen-1",
        reservation_id="rag-1",
        operation_kind="rag_retrieval",
        estimated_tokens=0,
        estimated_cost=Decimal("0.001"),
        is_rag=True,
        request_fingerprint="rag-1",
    )
    service.settle(
        generation_id="gen-1",
        reservation_id="rag-1",
        actual_tokens=10,
        actual_cost=Decimal("0.001"),
    )

    assert service.upgrade(generation_id="gen-1") == "think"
    assert service.upgrade(generation_id="gen-1") is None
    row = service.meter(generation_id="gen-1")
    assert row.effort_level == "think"
    assert row.rag_calls_used == 1
    assert row.total_tokens_used == 10
    assert row.settled_cost_amount == Decimal("0.001")


def test_budget_upgrade_requires_next_step_capacity() -> None:
    service = make_service()
    service.ensure_meter(
        generation_id="gen-1",
        effort_level="quick",
        deadline_at_utc=NOW + timedelta(seconds=60),
    )
    assert (
        service.upgrade(
            generation_id="gen-1",
            next_step_tokens=24000,
            next_step_cost=Decimal("0.200001"),
        )
        is None
    )


def test_budget_meter_and_reservation_rows_are_persisted() -> None:
    service = make_service()
    service.ensure_meter(
        generation_id="gen-1",
        effort_level="quick",
        deadline_at_utc=NOW + timedelta(seconds=20),
    )
    service.reserve(
        generation_id="gen-1",
        reservation_id="op-1",
        operation_kind="chat_generation",
        estimated_tokens=100,
        estimated_cost=Decimal("0.020"),
        is_rag=False,
        request_fingerprint="fp-1",
    )
    engine = service.engine
    with engine.connect() as connection:
        meter = connection.execute(select(generation_budget_meter_table)).mappings().one()
        reservation = (
            connection.execute(select(generation_budget_reservation_table)).mappings().one()
        )
    assert meter["status"] == "active"
    assert meter["price_version_id"] == "price-1"
    assert reservation["status"] == "reserved"
