from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.engine import Connection
from sqlalchemy.pool import StaticPool

from app.platform.database import SqlAlchemyDatabaseClock
from app.platform.errors import PlatformError
from app.usage.billing import BillingSourceRecord, ProviderBillingService
from app.usage.calendar import BusinessCalendarService
from app.usage.ledger import (
    LocalMeasurement,
    OwnershipSnapshot,
    ProviderMeasurement,
    UsageLedger,
)
from app.usage.metering import LocalUsageMeterService
from app.usage.price import PriceCatalogService
from app.usage.schema import (
    local_usage_meter_table,
    local_usage_projection_table,
    provider_billing_cost_adjustment_table,
    provider_billing_reconciliation_group_table,
    provider_billing_source_group_table,
    provider_billing_source_record_table,
    usage_cost_projection_table,
    usage_event_table,
    usage_metadata,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


@dataclass
class MutableClock:
    now: datetime

    def now_utc(self, connection: Connection | None = None) -> datetime:
        del connection
        return self.now


def ownership() -> OwnershipSnapshot:
    return OwnershipSnapshot(
        actor_user_id="u1",
        actor_role_snapshot="user",
        actor_department_id_snapshot=None,
        quota_subject_user_id="u1",
        cost_center_key="user:u1",
    )


def local_measurement(**values: int | None) -> LocalMeasurement:
    measurement_sources = {
        field: "client_measured" for field, value in values.items() if value is not None
    }
    return LocalMeasurement(
        item_count=values.get("item_count"),
        page_count=values.get("page_count"),
        input_bytes=values.get("input_bytes"),
        gpu_milliseconds=values.get("gpu_milliseconds"),
        cpu_milliseconds=values.get("cpu_milliseconds"),
        peak_vram_bytes=values.get("peak_vram_bytes"),
        measurement_sources=measurement_sources,
    )


def provider_measurement() -> ProviderMeasurement:
    return ProviderMeasurement(
        input_tokens=1000,
        output_tokens=200,
        prompt_cache_hit_tokens=None,
        prompt_cache_miss_tokens=None,
        reasoning_tokens=None,
        image_count=None,
        visual_input_tokens=None,
        embedding_input_tokens=None,
        vector_count=None,
        measurement_sources={
            "input_tokens": "provider_reported",
            "output_tokens": "provider_reported",
        },
    )


def make_env():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    usage_metadata.create_all(engine)
    clock = MutableClock(NOW)
    calendar = BusinessCalendarService(engine, SqlAlchemyDatabaseClock(engine), "Asia/Shanghai")
    prices = PriceCatalogService(engine, clock)
    ledger = UsageLedger(engine, clock, calendar, prices)
    return (
        engine,
        clock,
        ledger,
        LocalUsageMeterService(ledger, clock),
        ProviderBillingService(ledger, clock),
    )


def seed_price(engine, ledger) -> None:
    with engine.begin() as connection:
        ledger.calendar.lock_or_verify(connection)
        ledger.prices.register(
            connection,
            provider="dashscope",
            model="qwen-plus",
            operation="generate",
            currency_code="USD",
            lines=[
                {"meter": "input_tokens", "unit": "token", "rate": Decimal("0.000020")},
                {"meter": "output_tokens", "unit": "token", "rate": Decimal("0.000060")},
            ],
            effective_from_utc=NOW,
        )


def seed_provider_usage(engine: Engine, ledger: UsageLedger) -> str:
    seed_price(engine, ledger)
    call_id = ledger.prepare_provider_call(
        provider="dashscope",
        model="qwen-plus",
        operation="generate",
        execution_kind="generation",
        execution_id="gen-1",
        generation_id="gen-1",
        deadline_utc=NOW + timedelta(minutes=5),
        request_fingerprint="fp-gen-1",
    )
    ledger.mark_dispatching(call_id, started_at_provider=NOW)
    return ledger.complete_provider_call(
        provider_call_id=call_id,
        measurement=provider_measurement(),
        ownership=ownership(),
        result="succeeded",
        provider_request_id="req-1",
    )


def billing_record(**changes: object) -> BillingSourceRecord:
    values: dict[str, object] = {
        "provider": "dashscope",
        "provider_account_id": "account-1",
        "billing_source_record_id": "bill-row-1",
        "model": "qwen-plus",
        "operation": "generate",
        "provider_request_id": "req-1",
        "service_start_utc": datetime(2026, 8, 1, tzinfo=UTC),
        "service_end_utc": datetime(2026, 8, 2, tzinfo=UTC),
        "service_month": None,
        "amount": Decimal("0.040"),
        "currency_code": "USD",
        "source_status": "billed",
        "measurements": {"input_tokens": 1000, "output_tokens": 200},
        "source_metadata": {"statement": "2026-08"},
    }
    values.update(changes)
    return BillingSourceRecord(**values)


def test_local_meter_is_unique_and_finalizes_once() -> None:
    engine, clock, ledger, meter, _ = make_env()

    first = meter.start(
        execution_kind="ingestion",
        execution_id="attempt-1",
        stage="ocr",
        resource_kind="gpu",
        ownership=ownership(),
        lease_expires_at_utc=NOW + timedelta(minutes=5),
    )
    second = meter.start(
        execution_kind="ingestion",
        execution_id="attempt-1",
        stage="ocr",
        resource_kind="gpu",
        ownership=ownership(),
        lease_expires_at_utc=NOW + timedelta(minutes=5),
    )
    meter.checkpoint(
        execution_kind="ingestion",
        execution_id="attempt-1",
        stage="ocr",
        resource_kind="gpu",
        sequence=1,
        measurement=local_measurement(page_count=2, input_bytes=10),
    )

    with pytest.raises(PlatformError) as failure:
        meter.checkpoint(
            execution_kind="ingestion",
            execution_id="attempt-1",
            stage="ocr",
            resource_kind="gpu",
            sequence=2,
            measurement=local_measurement(page_count=1, input_bytes=10),
        )

    assert first["meter_id"] == second["meter_id"]
    assert failure.value.code == "validation_error"
    with engine.connect() as connection:
        assert (
            connection.execute(
                select(func.count()).select_from(local_usage_meter_table)
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(select(func.count()).select_from(usage_event_table)).scalar_one()
            == 0
        )

    result = meter.finalize(
        execution_kind="ingestion",
        execution_id="attempt-1",
        stage="ocr",
        resource_kind="gpu",
        result="succeeded",
        measurement=local_measurement(page_count=3, input_bytes=20),
        ownership=ownership(),
        started_at_utc=NOW - timedelta(minutes=1),
    )
    replay = meter.finalize(
        execution_kind="ingestion",
        execution_id="attempt-1",
        stage="ocr",
        resource_kind="gpu",
        result="succeeded",
        measurement=local_measurement(page_count=3, input_bytes=20),
        ownership=ownership(),
        started_at_utc=NOW - timedelta(minutes=1),
    )

    assert result["usage_event_id"] == replay["usage_event_id"]
    with engine.connect() as connection:
        rows = connection.execute(select(local_usage_meter_table)).mappings().one()
        events = connection.execute(select(usage_event_table)).mappings().all()
        projections = connection.execute(select(local_usage_projection_table)).mappings().all()
        assert rows["status"] == "completed"
        assert rows["checkpoint_sequence"] == 2
        assert len(events) == 1
        assert events[0]["event_kind"] == "local_usage"
        assert events[0]["page_count"] == 3
        assert len(projections) == 1
        assert projections[0]["usage_event_id"] == result["usage_event_id"]
        assert projections[0]["page_count"] == 3


def test_local_meter_recovery_is_idempotent_and_uses_last_checkpoint() -> None:
    engine, clock, ledger, meter, _ = make_env()
    meter.start(
        execution_kind="ingestion",
        execution_id="attempt-crash",
        stage="embedding",
        resource_kind="cpu",
        ownership=ownership(),
        lease_expires_at_utc=NOW - timedelta(seconds=1),
    )
    meter.checkpoint(
        execution_kind="ingestion",
        execution_id="attempt-crash",
        stage="embedding",
        resource_kind="cpu",
        sequence=1,
        measurement=local_measurement(item_count=7),
    )

    first = meter.recover_expired(now_utc=NOW)
    second = meter.recover_expired(now_utc=NOW)

    assert first[0]["status"] == "abandoned"
    assert second == []
    with engine.connect() as connection:
        row = connection.execute(select(local_usage_meter_table)).mappings().one()
        events = connection.execute(select(usage_event_table)).mappings().all()
        assert row["status"] == "abandoned"
        assert row["tail_estimated"] == 1
        assert row["error_code"] == "meter_lease_expired"
        assert row["measurement_sources"] == {"item_count": "client_measured"}
        assert len(events) == 1
        assert events[0]["item_count"] == 7
        assert events[0]["result"] == "abandoned"


def test_billing_import_is_immutable_and_reconciles_request_linked_usage() -> None:
    engine, clock, ledger, _, billing = make_env()
    event_id = seed_provider_usage(engine, ledger)
    record = billing_record()
    source_id = billing.import_record(record)

    assert billing.import_record(record) == source_id
    with pytest.raises(PlatformError) as conflict:
        billing.import_record(billing_record(amount=Decimal("0.050")))
    with pytest.raises(PlatformError) as missing:
        billing.import_record(billing_record(billing_source_record_id=""))

    assert conflict.value.code == "billing_source_conflict"
    assert missing.value.code == "billing_source_id_missing"

    reconciled = billing.reconcile(source_id, ownership=ownership())
    replay = billing.reconcile(source_id, ownership=ownership())
    assert reconciled.adjustment_event_id == replay.adjustment_event_id
    assert reconciled.status == "reconciled"
    assert reconciled.amount_delta == Decimal("0.008")

    with engine.connect() as connection:
        source_rows = (
            connection.execute(select(provider_billing_source_record_table)).mappings().all()
        )
        adjustment = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.event_kind == "cost_adjustment")
            )
            .mappings()
            .one()
        )
        original = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.usage_event_id == event_id)
            )
            .mappings()
            .one()
        )
        projection = (
            connection.execute(
                select(usage_cost_projection_table).where(
                    usage_cost_projection_table.c.target_kind == "provider_event"
                )
            )
            .mappings()
            .one()
        )
        assert len(source_rows) == 1
        assert adjustment["referenced_usage_event_id"] == event_id
        assert adjustment["estimated_cost_amount"] == Decimal("0.008")
        for field in (
            "effective_calendar_version_id",
            "effective_at_utc",
            "effective_period",
        ):
            assert adjustment[field] == original[field]
        assert original["estimated_cost_amount"] == Decimal("0.032")
        assert projection["cost_status"] == "reconciled"
        assert projection["projected_amount"] == Decimal("0.040")
    rebuilt = billing.cost_projection()
    assert [
        (row["target_kind"], row["cost_status"], row["effective_period"]) for row in rebuilt
    ] == [("provider_event", "reconciled", "2026-08")]
    assert rebuilt[0]["recorded_period"] == "2026-08"


def test_month_group_reconciliation_allocates_or_stays_unallocated() -> None:
    engine, clock, ledger, _, billing = make_env()
    source_id = billing.import_record(
        billing_record(
            billing_source_record_id="bill-group-1",
            provider_request_id=None,
            service_start_utc=datetime(2026, 7, 1, tzinfo=UTC),
            service_end_utc=datetime(2026, 8, 31, tzinfo=UTC),
            amount=Decimal("0.300"),
        )
    )
    unallocated = billing.reconcile(source_id, ownership=ownership())
    assert unallocated.status == "billing_period_unallocated"
    assert unallocated.adjustments == ()

    allocated = billing.reconcile(
        source_id,
        ownership=ownership(),
        allocations=[
            {"period": "2026-07", "amount_delta": Decimal("0.180")},
            {"period": "2026-08", "amount_delta": Decimal("0.120")},
        ],
    )
    replay = billing.reconcile(
        source_id,
        ownership=ownership(),
        allocations=[
            {"period": "2026-07", "amount_delta": Decimal("0.180")},
            {"period": "2026-08", "amount_delta": Decimal("0.120")},
        ],
    )
    assert [item.period for item in allocated.adjustments] == ["2026-07", "2026-08"]
    assert [item.adjustment_id for item in allocated.adjustments] == [
        item.adjustment_id for item in replay.adjustments
    ]

    with engine.connect() as connection:
        group = (
            connection.execute(select(provider_billing_reconciliation_group_table)).mappings().one()
        )
        links = connection.execute(select(provider_billing_source_group_table)).mappings().all()
        assert len(links) == 1
        rows = (
            connection.execute(
                select(provider_billing_cost_adjustment_table).order_by(
                    provider_billing_cost_adjustment_table.c.effective_period
                )
            )
            .mappings()
            .all()
        )
        assert (
            links[0]["provider_billing_reconciliation_group_id"]
            == group["provider_billing_reconciliation_group_id"]
        )
        assert [(row["effective_period"], row["amount_delta"]) for row in rows] == [
            ("2026-07", Decimal("0.180")),
            ("2026-08", Decimal("0.120")),
        ]
        assert all(row["recorded_period"] == "2026-08" for row in rows)
        projection_rows = (
            connection.execute(
                select(usage_cost_projection_table).order_by(
                    usage_cost_projection_table.c.effective_period
                )
            )
            .mappings()
            .all()
        )
        assert [row["cost_status"] for row in projection_rows] == ["reconciled", "reconciled"]
        assert [row["projected_amount"] for row in projection_rows] == [
            Decimal("0.180"),
            Decimal("0.120"),
        ]
    rebuilt = billing.cost_projection()
    assert [
        (row["effective_period"], row["recorded_period"], row["cost_status"]) for row in rebuilt
    ] == [
        ("2026-07", "2026-08", "reconciled"),
        ("2026-08", "2026-08", "reconciled"),
    ]


def test_provider_service_month_and_shared_group_are_automatic() -> None:
    engine, clock, ledger, _, billing = make_env()
    monthly_id = billing.import_record(
        billing_record(
            billing_source_record_id="bill-month-1",
            provider_request_id=None,
            service_month="2026-08",
            service_start_utc=None,
            service_end_utc=None,
            amount=Decimal("0.090"),
        )
    )
    monthly = billing.reconcile(monthly_id, ownership=ownership())
    assert monthly.status == "reconciled"
    assert [(item.period, item.amount_delta) for item in monthly.adjustments] == [
        ("2026-08", Decimal("0.090"))
    ]

    first_id = billing.import_record(
        billing_record(
            billing_source_record_id="bill-shared-1",
            provider_request_id=None,
            amount=Decimal("0.100"),
        )
    )
    second_id = billing.import_record(
        billing_record(
            billing_source_record_id="bill-shared-2",
            provider_request_id=None,
            amount=Decimal("0.200"),
        )
    )
    first = billing.reconcile(first_id, ownership=ownership())
    second = billing.reconcile(second_id, ownership=ownership())
    assert first.status == second.status == "billing_period_unallocated"
    assert first.source_record_id == second.source_record_id
    billing.cost_projection()
    metric_counters = billing.metrics.snapshot()["counters"]
    assert {(item["name"], item["outcome"]): item["value"] for item in metric_counters}[
        ("usage_cost_projection_status", "billing_period_unallocated")
    ] >= 2
    with engine.connect() as connection:
        groups = (
            connection.execute(select(provider_billing_reconciliation_group_table)).mappings().all()
        )
        links = connection.execute(select(provider_billing_source_group_table)).mappings().all()
        assert len(groups) == 2
        shared_group_ids = {
            group["provider_billing_reconciliation_group_id"]
            for group in groups
            if group["service_start_utc"] is not None
        }
        assert len(shared_group_ids) == 1
        assert {link["provider_billing_source_record_id"] for link in links} == {
            first_id,
            second_id,
            monthly_id,
        }
    first_allocated = billing.reconcile(
        first_id,
        ownership=ownership(),
        allocations=[{"period": "2026-08", "amount_delta": Decimal("0.100")}],
    )
    second_allocated = billing.reconcile(
        second_id,
        ownership=ownership(),
        allocations=[{"period": "2026-08", "amount_delta": Decimal("0.200")}],
    )
    assert first_allocated.source_record_id == second_allocated.source_record_id
    third_id = billing.import_record(
        billing_record(
            provider="other-provider",
            provider_account_id="other-account",
            billing_source_record_id="bill-shared-1",
            provider_request_id=None,
            amount=Decimal("0.300"),
        )
    )
    third_allocated = billing.reconcile(
        third_id,
        ownership=ownership(),
        allocations=[{"period": "2026-08", "amount_delta": Decimal("0.300")}],
    )
    assert third_allocated.adjustments[0].adjustment_id not in {
        item.adjustment_id for item in first_allocated.adjustments
    }
    rebuilt = billing.cost_projection()
    shared_rows = [
        row
        for row in rebuilt
        if row["provider_billing_source_record_id"] in {first_id, second_id, third_id}
    ]
    assert sorted(row["projected_amount"] for row in shared_rows) == sorted(
        [Decimal("0.100"), Decimal("0.200"), Decimal("0.300")]
    )


def test_billing_rejects_sensitive_source_metadata() -> None:
    _, _, _, _, billing = make_env()
    with pytest.raises(PlatformError) as failure:
        billing.import_record(billing_record(source_metadata={"response_body": "<secret/>"}))
    assert failure.value.code == "billing_source_metadata_rejected"


def test_rebuild_cost_projection_is_stable_across_rebuilds() -> None:
    """A40：重建是纯派生——两次重建行数与业务字段完全一致，无重复无漂移。"""

    engine, clock, ledger, _, billing = make_env()
    seed_provider_usage(engine, ledger)

    linked_id = billing.import_record(billing_record())
    billing.reconcile(linked_id, ownership=ownership())

    group_id = billing.import_record(
        billing_record(
            billing_source_record_id="bill-rebuild-group",
            provider_request_id=None,
            service_month="2026-08",
            amount=Decimal("0.300"),
        )
    )
    billing.reconcile(
        group_id,
        ownership=ownership(),
        allocations=[
            {"period": "2026-07", "amount_delta": Decimal("0.180")},
            {"period": "2026-08", "amount_delta": Decimal("0.120")},
        ],
    )
    unallocated_id = billing.import_record(
        billing_record(
            billing_source_record_id="bill-rebuild-unallocated",
            provider_request_id=None,
            service_month=None,
            amount=Decimal("0.050"),
        )
    )
    billing.reconcile(unallocated_id, ownership=ownership())

    def _projection_facts() -> list[tuple]:
        with engine.connect() as connection:
            return sorted(
                (
                    row["target_kind"],
                    row["target_id"],
                    row["effective_period"],
                    row["currency_code"],
                    row["estimated_amount"],
                    row["adjustment_amount"],
                    row["projected_amount"],
                    row["cost_status"],
                )
                for row in connection.execute(select(usage_cost_projection_table)).mappings()
            )

    first_count = billing.rebuild_cost_projection()
    first_facts = _projection_facts()
    second_count = billing.rebuild_cost_projection()
    second_facts = _projection_facts()

    assert first_count == second_count == 4
    assert first_facts == second_facts
    statuses = {fact[0] for fact in first_facts}
    assert statuses == {"provider_event", "reconciliation_group"}
    assert [fact[7] for fact in first_facts].count("reconciled") == 3
    assert [fact[7] for fact in first_facts].count("billing_period_unallocated") == 1


def test_group_allocation_replay_is_idempotent_and_conflict_rolls_back() -> None:
    """A40：同额分摊重放复用既有行；异额重放 409 且不残留任何新事实。"""

    engine, clock, ledger, _, billing = make_env()
    source_id = billing.import_record(
        billing_record(
            billing_source_record_id="bill-idem-group",
            provider_request_id=None,
            service_start_utc=datetime(2026, 7, 1, tzinfo=UTC),
            service_end_utc=datetime(2026, 8, 31, tzinfo=UTC),
            amount=Decimal("0.300"),
        )
    )
    allocations = [
        {"period": "2026-07", "amount_delta": Decimal("0.180")},
        {"period": "2026-08", "amount_delta": Decimal("0.120")},
    ]
    first = billing.reconcile(source_id, ownership=ownership(), allocations=allocations)
    replay = billing.reconcile(source_id, ownership=ownership(), allocations=allocations)

    assert [item.adjustment_id for item in replay.adjustments] == [
        item.adjustment_id for item in first.adjustments
    ]
    # 总额仍等于账单金额，但既有月份的金额不同 → 409 冲突而非静默改写。
    divergent = [
        {"period": "2026-07", "amount_delta": Decimal("0.150")},
        {"period": "2026-08", "amount_delta": Decimal("0.150")},
    ]
    with pytest.raises(PlatformError) as failure:
        billing.reconcile(source_id, ownership=ownership(), allocations=divergent)
    assert failure.value.code == "billing_allocation_conflict"

    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(provider_billing_cost_adjustment_table).order_by(
                    provider_billing_cost_adjustment_table.c.effective_period
                )
            )
            .mappings()
            .all()
        )
        # 冲突事务整体回滚：既有分摊保持原额，无新增行。
        assert [(row["effective_period"], row["amount_delta"]) for row in rows] == [
            ("2026-07", Decimal("0.180")),
            ("2026-08", Decimal("0.120")),
        ]


def test_group_currencies_stay_separate_and_request_linked_mismatch_is_rejected() -> None:
    """A40：货币边界——分组按币种隔离；request-linked 账单币种不符直接 422。"""

    engine, clock, ledger, _, billing = make_env()
    seed_provider_usage(engine, ledger)

    usd_group_id = billing.import_record(
        billing_record(
            billing_source_record_id="bill-usd-group",
            provider_request_id=None,
            service_month="2026-08",
            amount=Decimal("0.100"),
        )
    )
    billing.reconcile(usd_group_id, ownership=ownership())
    cny_group_id = billing.import_record(
        billing_record(
            billing_source_record_id="bill-cny-group",
            provider_request_id=None,
            service_month="2026-08",
            amount=Decimal("0.300"),
            currency_code="CNY",
        )
    )
    cny = billing.reconcile(cny_group_id, ownership=ownership())
    assert [(item.period, item.amount_delta) for item in cny.adjustments] == [
        ("2026-08", Decimal("0.300"))
    ]

    mismatched = billing.import_record(
        billing_record(
            billing_source_record_id="bill-mismatch",
            currency_code="CNY",
        )
    )
    with pytest.raises(PlatformError) as failure:
        billing.reconcile(mismatched, ownership=ownership())
    assert failure.value.code == "billing_currency_mismatch"

    rebuilt = billing.cost_projection()
    group_rows = [row for row in rebuilt if row["target_kind"] == "reconciliation_group"]
    by_source = {row["provider_billing_source_record_id"]: row for row in group_rows}
    assert by_source[usd_group_id]["currency_code"] == "USD"
    assert by_source[cny_group_id]["currency_code"] == "CNY"
    # 币种不同 → 永不并入同一分摊组。
    assert by_source[usd_group_id]["target_id"] != by_source[cny_group_id]["target_id"]
    with engine.connect() as connection:
        groups = (
            connection.execute(select(provider_billing_reconciliation_group_table)).mappings().all()
        )
        assert {group["currency_code"] for group in groups} == {"USD", "CNY"}
