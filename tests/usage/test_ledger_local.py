"""UsageLedger usage_adjustment / cost_adjustment 追加测试（Task 6，正式 spec + Task 5 review 约束）。

语义（正式 spec 优先于旧 task brief/plan）：
- 追加式、稳定 source/allocation/referenced event；唯一键四元组
  (event_kind, adjustment_source_namespace, adjustment_source_id,
  adjustment_allocation_key)；canonical fingerprint；同指纹重放复用 persisted ID，
  异指纹 409 ledger_invariant_conflict。
- 幂等优先级（Task 6 review R2）：同四元组已有行时先按完整 canonical fingerprint
  （含 referenced_event_id/ownership/result/currency/extra）比较；异指纹统一 409，
  不得先因新 referenced_event_id 缺失/是 adjustment chain/meter 或 currency 不兼容
  而返回 404/422。引用语义校验只影响首次插入。
- usage delta 是**有符号 correction**：非零 signed BigInteger，完整 64 位有符号
  范围 [-2**63, 2**63-1]（-2**63 接受、-2**63-1 拒绝），负值表达计量冲减。
- cost amount_delta 是**有符号 correction**：可为负（退款/冲减由 adjustment 表达，
  amount-only 对账的非负约束不适用于 adjustment）；currency 必须与被引用 provider
  usage 一致（无跨币种模型）；cost_adjustment 只引用原始 provider_usage。
- usage_adjustment 只引用原始 provider_usage/local_usage；delta meter 必须属于被
  引用事件类别（provider meters / local meters）。
- effective_* 继承被引用事件的日历事实（effective_at/period/calendar_version_id）；
  recorded_* 用当前 DB 时间与日历（clock.now 归期）。
- 引用不存在的事件（首次插入）→ 404 usage_event_not_found。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Connection
from sqlalchemy.pool import StaticPool

from app.platform.database import SqlAlchemyDatabaseClock
from app.platform.errors import PlatformError
from app.usage.calendar import BusinessCalendarService
from app.usage.ledger import (
    LocalMeasurement,
    OwnershipSnapshot,
    ProviderMeasurement,
    UsageLedger,
)
from app.usage.price import PriceCatalogService
from app.usage.schema import usage_event_table, usage_metadata

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
JULY = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class FixedClock:
    now: datetime

    def now_utc(self, connection: Connection | None = None) -> datetime:
        del connection
        return self.now


@dataclass
class MutableClock:
    now: datetime

    def now_utc(self, connection: Connection | None = None) -> datetime:
        del connection
        return self.now


def make_ledger(now: datetime = NOW, clock=None):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    usage_metadata.create_all(engine)
    if clock is None:
        clock = FixedClock(now)
    calendar = BusinessCalendarService(engine, SqlAlchemyDatabaseClock(engine), "Asia/Shanghai")
    prices = PriceCatalogService(engine, clock)
    return engine, UsageLedger(engine, clock, calendar, prices)


def ownership() -> OwnershipSnapshot:
    return OwnershipSnapshot(
        actor_user_id="u1",
        actor_role_snapshot="user",
        actor_department_id_snapshot=None,
        quota_subject_user_id="u1",
        cost_center_key="user:u1",
    )


def measurement() -> ProviderMeasurement:
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


def seed_local(engine, ledger, *, execution_id: str = "attempt-1") -> str:
    with engine.begin() as connection:
        ledger.calendar.lock_or_verify(connection)
    return ledger.submit_local_usage(
        execution_kind="ingestion",
        execution_id=execution_id,
        stage="ocr",
        resource_kind="gpu",
        measurement=LocalMeasurement(
            page_count=10,
            input_bytes=1024,
            item_count=None,
            gpu_milliseconds=None,
            cpu_milliseconds=None,
            peak_vram_bytes=None,
        ),
        ownership=ownership(),
        result="succeeded",
        started_at_utc=NOW,
    )


def _seed_provider_event(
    engine, ledger, *, execution_id: str, started_at_utc: datetime = NOW
) -> str:
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
            effective_from_utc=started_at_utc,
        )
    call_id = ledger.prepare_provider_call(
        provider="dashscope",
        model="qwen-plus",
        operation="generate",
        execution_kind="generation",
        execution_id=execution_id,
        attempt_id=f"attempt-{execution_id}",
        generation_id=f"generation-{execution_id}",
        resource_id=f"resource-{execution_id}",
        # Absolute deadline remains current/future even when the effective-period
        # fixture intentionally uses a historical actual started fact.
        deadline_utc=NOW + timedelta(hours=1),
        request_fingerprint=f"fp-{execution_id}",
    )
    ledger.mark_dispatching(call_id, started_at_provider=started_at_utc)
    return ledger.complete_provider_call(
        provider_call_id=call_id,
        measurement=measurement(),
        ownership=ownership(),
        result="succeeded",
    )


def _usage_adjustment_call(**overrides) -> dict:
    base = dict(
        referenced_event_id="ue_ref",
        adjustment_source_namespace="meter_recheck",
        adjustment_source_id="recheck-src",
        adjustment_allocation_key="ocr-gpu",
        deltas={"page_count": 1},
        ownership=ownership(),
    )
    base.update(overrides)
    return base


def test_usage_adjustment_appends_and_is_unique() -> None:
    engine, ledger = make_ledger()
    local_id = seed_local(engine, ledger)
    adjustment_id = ledger.append_usage_adjustment(
        referenced_event_id=local_id,
        adjustment_source_namespace="meter_recheck",
        adjustment_source_id="recheck-1",
        adjustment_allocation_key="ocr-gpu",
        deltas={"page_count": 2},
        ownership=ownership(),
    )
    replay = ledger.append_usage_adjustment(
        referenced_event_id=local_id,
        adjustment_source_namespace="meter_recheck",
        adjustment_source_id="recheck-1",
        adjustment_allocation_key="ocr-gpu",
        deltas={"page_count": 2},
        ownership=ownership(),
    )
    assert replay == adjustment_id
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.usage_event_id == adjustment_id)
            )
            .mappings()
            .one()
        )
    assert row["event_kind"] == "usage_adjustment"
    assert row["referenced_usage_event_id"] == local_id
    assert row["effective_period"] == "2026-08"
    assert row["page_count"] == 2
    assert row["execution_kind"] == "ingestion"
    assert row["execution_id"] == "attempt-1"
    assert row["attempt_id"] is None
    assert row["generation_id"] is None
    assert row["resource_id"] is None
    assert row["stage"] is None
    assert row["resource_kind"] is None


def test_usage_adjustment_conflicting_fingerprint_is_invariant_error() -> None:
    engine, ledger = make_ledger()
    local_id = seed_local(engine, ledger)
    ledger.append_usage_adjustment(
        referenced_event_id=local_id,
        adjustment_source_namespace="meter_recheck",
        adjustment_source_id="recheck-1",
        adjustment_allocation_key="ocr-gpu",
        deltas={"page_count": 2},
        ownership=ownership(),
    )
    with pytest.raises(PlatformError) as exc:
        ledger.append_usage_adjustment(
            referenced_event_id=local_id,
            adjustment_source_namespace="meter_recheck",
            adjustment_source_id="recheck-1",
            adjustment_allocation_key="ocr-gpu",
            deltas={"page_count": 5},
            ownership=ownership(),
        )
    assert exc.value.code == "ledger_invariant_conflict"


def test_cost_adjustment_appends_amount() -> None:
    engine, ledger = make_ledger()
    provider_id = _seed_provider_event(engine, ledger, execution_id="gen_cost")
    cost_id = ledger.append_cost_adjustment(
        referenced_event_id=provider_id,
        adjustment_source_namespace="billing",
        adjustment_source_id="bill-1",
        adjustment_allocation_key="cost",
        amount_delta=Decimal("0.000001"),
        currency_code="USD",
        ownership=ownership(),
    )
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.usage_event_id == cost_id)
            )
            .mappings()
            .one()
        )
    assert row["event_kind"] == "cost_adjustment"
    assert Decimal(str(row["estimated_cost_amount"])) == Decimal("0.000001")
    assert row["currency_code"] == "USD"
    assert row["estimated_cost_status"] == "complete"


def test_usage_adjustment_references_provider_event_with_provider_meters() -> None:
    """usage_adjustment 允许引用原始 provider_usage，delta 用 provider meter。"""
    engine, ledger = make_ledger()
    provider_id = _seed_provider_event(engine, ledger, execution_id="gen_adj")
    adjustment_id = ledger.append_usage_adjustment(
        referenced_event_id=provider_id,
        adjustment_source_namespace="meter_recheck",
        adjustment_source_id="recheck-prov",
        adjustment_allocation_key="gen-gpu",
        deltas={"input_tokens": 5},
        ownership=ownership(),
    )
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.usage_event_id == adjustment_id)
            )
            .mappings()
            .one()
        )
    assert row["event_kind"] == "usage_adjustment"
    assert row["referenced_usage_event_id"] == provider_id
    assert row["input_tokens"] == 5
    assert row["page_count"] is None  # 只写 delta meter，不写其他 meter
    assert row["execution_kind"] == "generation"
    assert row["execution_id"] == "gen_adj"
    assert row["attempt_id"] == "attempt-gen_adj"
    assert row["generation_id"] == "generation-gen_adj"
    assert row["resource_id"] == "resource-gen_adj"
    assert row["stage"] is None
    assert row["resource_kind"] is None


def test_usage_adjustment_effective_fields_inherit_referenced_facts() -> None:
    """有效事实继承被引用事件（7 月 local + 8 月追加）；recorded 用当前 DB 时间/日历。"""
    engine, ledger = make_ledger()  # clock NOW = 2026-08-05（Asia/Shanghai → 2026-08）
    with engine.begin() as connection:
        ledger.calendar.lock_or_verify(connection)
    local_id = ledger.submit_local_usage(
        execution_kind="ingestion",
        execution_id="attempt-jul",
        stage="ocr",
        resource_kind="gpu",
        measurement=LocalMeasurement(
            page_count=10,
            input_bytes=1024,
            item_count=None,
            gpu_milliseconds=None,
            cpu_milliseconds=None,
            peak_vram_bytes=None,
        ),
        ownership=ownership(),
        result="succeeded",
        started_at_utc=JULY,  # 被引用事件归期 2026-07
    )
    adjustment_id = ledger.append_usage_adjustment(
        referenced_event_id=local_id,
        adjustment_source_namespace="meter_recheck",
        adjustment_source_id="recheck-jul",
        adjustment_allocation_key="ocr-gpu",
        deltas={"page_count": 2},
        ownership=ownership(),
    )
    with engine.connect() as connection:
        ref = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.usage_event_id == local_id)
            )
            .mappings()
            .one()
        )
        adj = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.usage_event_id == adjustment_id)
            )
            .mappings()
            .one()
        )
    # effective_* 继承引用事实，不按追加时刻重新归期
    assert adj["effective_period"] == "2026-07"
    assert adj["effective_at_utc"] == ref["effective_at_utc"]
    assert adj["effective_calendar_version_id"] == ref["effective_calendar_version_id"]
    assert adj["started_at_utc"] == ref["effective_at_utc"]
    # recorded_* 用当前 DB 时间/日历
    assert adj["recorded_period"] == "2026-08"
    assert adj["recorded_at_utc"] == NOW.replace(tzinfo=None)
    assert adj["completed_at_utc"] == NOW.replace(tzinfo=None)


def test_cost_adjustment_effective_fields_inherit_referenced_facts() -> None:
    """cost_adjustment 同样继承被引用 provider usage 的日历事实。"""
    engine, ledger = make_ledger()
    provider_id = _seed_provider_event(engine, ledger, execution_id="gen_jul", started_at_utc=JULY)
    cost_id = ledger.append_cost_adjustment(
        referenced_event_id=provider_id,
        adjustment_source_namespace="billing",
        adjustment_source_id="bill-jul",
        adjustment_allocation_key="cost",
        amount_delta=Decimal("0.000001"),
        currency_code="USD",
        ownership=ownership(),
    )
    with engine.connect() as connection:
        ref = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.usage_event_id == provider_id)
            )
            .mappings()
            .one()
        )
        adj = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.usage_event_id == cost_id)
            )
            .mappings()
            .one()
        )
    assert adj["effective_period"] == "2026-07"
    assert adj["effective_at_utc"] == ref["effective_at_utc"]
    assert adj["recorded_period"] == "2026-08"
    assert adj["currency_code"] == "USD"


def test_adjustment_moving_clock_replay_reuses_persisted_id() -> None:
    """指纹不含 recorded_*：moving clock 重放复用 persisted ID，不改写记录时间。"""
    mutable_clock = MutableClock(now=NOW)
    engine, ledger = make_ledger(clock=mutable_clock)
    local_id = seed_local(engine, ledger)
    first = ledger.append_usage_adjustment(
        referenced_event_id=local_id,
        adjustment_source_namespace="meter_recheck",
        adjustment_source_id="recheck-mv",
        adjustment_allocation_key="ocr-gpu",
        deltas={"page_count": 2},
        ownership=ownership(),
    )
    mutable_clock.now = NOW + timedelta(days=1)
    replay = ledger.append_usage_adjustment(
        referenced_event_id=local_id,
        adjustment_source_namespace="meter_recheck",
        adjustment_source_id="recheck-mv",
        adjustment_allocation_key="ocr-gpu",
        deltas={"page_count": 2},
        ownership=ownership(),
    )
    assert replay == first
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.usage_event_id == first)
            )
            .mappings()
            .one()
        )
    assert row["recorded_at_utc"] == NOW.replace(tzinfo=None)  # 重放不改写记录时间


def test_cost_adjustment_replay_reuses_persisted_id() -> None:
    engine, ledger = make_ledger()
    provider_id = _seed_provider_event(engine, ledger, execution_id="gen_replay")
    first = ledger.append_cost_adjustment(
        referenced_event_id=provider_id,
        adjustment_source_namespace="billing",
        adjustment_source_id="bill-replay",
        adjustment_allocation_key="cost",
        amount_delta=Decimal("0.000001"),
        currency_code="USD",
        ownership=ownership(),
    )
    replay = ledger.append_cost_adjustment(
        referenced_event_id=provider_id,
        adjustment_source_namespace="billing",
        adjustment_source_id="bill-replay",
        adjustment_allocation_key="cost",
        amount_delta=Decimal("0.000001"),
        currency_code="USD",
        ownership=ownership(),
    )
    assert replay == first
    with engine.connect() as connection:
        rows = connection.execute(
            select(usage_event_table).where(usage_event_table.c.event_kind == "cost_adjustment")
        ).all()
    assert len(rows) == 1


def test_cost_adjustment_conflicting_fingerprint_is_invariant_error() -> None:
    engine, ledger = make_ledger()
    provider_id = _seed_provider_event(engine, ledger, execution_id="gen_conf")
    ledger.append_cost_adjustment(
        referenced_event_id=provider_id,
        adjustment_source_namespace="billing",
        adjustment_source_id="bill-conf",
        adjustment_allocation_key="cost",
        amount_delta=Decimal("0.000001"),
        currency_code="USD",
        ownership=ownership(),
    )
    with pytest.raises(PlatformError) as exc:
        ledger.append_cost_adjustment(
            referenced_event_id=provider_id,
            adjustment_source_namespace="billing",
            adjustment_source_id="bill-conf",
            adjustment_allocation_key="cost",
            amount_delta=Decimal("0.000002"),  # 异金额 → 异指纹
            currency_code="USD",
            ownership=ownership(),
        )
    assert exc.value.code == "ledger_invariant_conflict"


def test_adjustment_referenced_event_not_found_is_404() -> None:
    engine, ledger = make_ledger()
    with engine.begin() as connection:
        ledger.calendar.lock_or_verify(connection)
    with pytest.raises(PlatformError) as exc:
        ledger.append_usage_adjustment(
            referenced_event_id="ue_missing",
            adjustment_source_namespace="meter_recheck",
            adjustment_source_id="recheck-miss",
            adjustment_allocation_key="ocr-gpu",
            deltas={"page_count": 1},
            ownership=ownership(),
        )
    assert exc.value.code == "usage_event_not_found"
    assert exc.value.status_code == 404
    with pytest.raises(PlatformError) as exc:
        ledger.append_cost_adjustment(
            referenced_event_id="ue_missing",
            adjustment_source_namespace="billing",
            adjustment_source_id="bill-miss",
            adjustment_allocation_key="cost",
            amount_delta=Decimal("0.000001"),
            currency_code="USD",
            ownership=ownership(),
        )
    assert exc.value.code == "usage_event_not_found"
    assert exc.value.status_code == 404


@pytest.mark.parametrize(
    "deltas",
    [
        {},  # 空 dict
        {"page_count": 0},  # 零 delta（任意 delta 0 不放行）
        {"page_count": True},  # bool 不是合法整数
        {"made_up_meter": 1},  # 固定 meter 集合外
        {"page_count": -(2**63) - 1},  # 低于有符号 64 位下限
        {"page_count": 2**63},  # 高于有符号 64 位上限
    ],
)
def test_usage_adjustment_delta_validation(deltas: dict) -> None:
    engine, ledger = make_ledger()
    local_id = seed_local(engine, ledger)
    with pytest.raises(PlatformError) as exc:
        ledger.append_usage_adjustment(
            referenced_event_id=local_id,
            adjustment_source_namespace="meter_recheck",
            adjustment_source_id="recheck-val",
            adjustment_allocation_key="ocr-gpu",
            deltas=deltas,
            ownership=ownership(),
        )
    assert exc.value.code == "validation_error"


def test_usage_adjustment_delta_signed_bigint_boundaries() -> None:
    """usage delta 是 signed correction：完整 64 位有符号范围接受，含 -2**63 与 2**63-1。"""
    engine, ledger = make_ledger()
    local_id = seed_local(engine, ledger)
    accepted = {
        "min": -(2**63),  # 下限本身接受
        "max": 2**63 - 1,  # 上限本身接受
        "minus_one": -1,  # 常规负 correction
    }
    for key, delta in accepted.items():
        adj_id = ledger.append_usage_adjustment(
            referenced_event_id=local_id,
            adjustment_source_namespace="meter_recheck",
            adjustment_source_id=f"recheck-signed-{key}",
            adjustment_allocation_key="ocr-gpu",
            deltas={"page_count": delta},
            ownership=ownership(),
        )
        with engine.connect() as connection:
            row = (
                connection.execute(
                    select(usage_event_table).where(usage_event_table.c.usage_event_id == adj_id)
                )
                .mappings()
                .one()
            )
        assert row["page_count"] == delta


@pytest.mark.parametrize("amount", [None, "1.5", Decimal("1e100")])
def test_cost_adjustment_amount_must_be_finite_decimal(amount) -> None:
    """金额校验在引用解析之前（422），引用 ID 可为任意值。"""
    engine, ledger = make_ledger()
    with engine.begin() as connection:
        ledger.calendar.lock_or_verify(connection)
    with pytest.raises(PlatformError) as exc:
        ledger.append_cost_adjustment(
            referenced_event_id="ue_bogus",
            adjustment_source_namespace="billing",
            adjustment_source_id="bill-val",
            adjustment_allocation_key="cost",
            amount_delta=amount,
            currency_code="USD",
            ownership=ownership(),
        )
    assert exc.value.code == "validation_error"


def test_cost_adjustment_negative_delta_allowed() -> None:
    """cost amount_delta 是**有符号 correction**：负差异（退款/冲减）合法。

    正式 spec：完整计量或账单差异只能以引用原事实的 cost_adjustment 追加，不更新原
    事件；接口命名即 amount_delta（有符号）。amount-only 对账行的非负约束
    （record_reconciliation_amount_only）约束的是“原始金额事实”，不适用于 adjustment
    的差异修正；Task 6 review 的“amount 非负”建议若应用于 cost_adjustment 会破坏
    退款/冲减语义，故不采纳（技术解释见 report）。
    """
    engine, ledger = make_ledger()
    provider_id = _seed_provider_event(engine, ledger, execution_id="gen_neg")
    cost_id = ledger.append_cost_adjustment(
        referenced_event_id=provider_id,
        adjustment_source_namespace="billing",
        adjustment_source_id="bill-neg",
        adjustment_allocation_key="cost",
        amount_delta=Decimal("-0.000001"),
        currency_code="USD",
        ownership=ownership(),
    )
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.usage_event_id == cost_id)
            )
            .mappings()
            .one()
        )
    assert Decimal(str(row["estimated_cost_amount"])) == Decimal("-0.000001")


def test_cost_adjustment_negative_scale_boundary() -> None:
    """负数同样受 Numeric(30,10) 约束：-1e20 拒绝（边界外）、-0.0000000001 拒绝（超 scale）。"""
    engine, ledger = make_ledger()
    provider_id = _seed_provider_event(engine, ledger, execution_id="gen_negscale")
    for amount in (Decimal("-1e20"), Decimal("-0.00000000001")):
        with pytest.raises(PlatformError) as exc:
            ledger.append_cost_adjustment(
                referenced_event_id=provider_id,
                adjustment_source_namespace="billing",
                adjustment_source_id="bill-negscale",
                adjustment_allocation_key="cost",
                amount_delta=amount,
                currency_code="USD",
                ownership=ownership(),
            )
        assert exc.value.code == "validation_error"


@pytest.mark.parametrize(
    "overrides",
    [
        {"adjustment_source_namespace": "  "},
        {"adjustment_source_id": ""},
        {"adjustment_allocation_key": ""},
        {"referenced_event_id": ""},
    ],
)
def test_adjustment_source_fields_validation(overrides: dict) -> None:
    engine, ledger = make_ledger()
    with engine.begin() as connection:
        ledger.calendar.lock_or_verify(connection)
    with pytest.raises(PlatformError) as exc:
        ledger.append_usage_adjustment(**_usage_adjustment_call(**overrides))
    assert exc.value.code == "validation_error"


def test_adjustment_ownership_validation() -> None:
    engine, ledger = make_ledger()
    local_id = seed_local(engine, ledger)
    bad = OwnershipSnapshot(
        actor_user_id="",
        actor_role_snapshot="user",
        actor_department_id_snapshot=None,
        quota_subject_user_id="u1",
        cost_center_key="user:u1",
    )
    with pytest.raises(PlatformError) as exc:
        ledger.append_usage_adjustment(
            referenced_event_id=local_id,
            adjustment_source_namespace="meter_recheck",
            adjustment_source_id="recheck-own",
            adjustment_allocation_key="ocr-gpu",
            deltas={"page_count": 1},
            ownership=bad,
        )
    assert exc.value.code == "validation_error"


def test_adjustment_result_validation_and_custom_result() -> None:
    engine, ledger = make_ledger()
    local_id = seed_local(engine, ledger)
    with pytest.raises(PlatformError) as exc:
        ledger.append_usage_adjustment(
            referenced_event_id=local_id,
            adjustment_source_namespace="meter_recheck",
            adjustment_source_id="recheck-res",
            adjustment_allocation_key="ocr-gpu",
            deltas={"page_count": 1},
            ownership=ownership(),
            result="",
        )
    assert exc.value.code == "validation_error"
    custom_id = ledger.append_usage_adjustment(
        referenced_event_id=local_id,
        adjustment_source_namespace="meter_recheck",
        adjustment_source_id="recheck-res2",
        adjustment_allocation_key="ocr-gpu",
        deltas={"page_count": 1},
        ownership=ownership(),
        result="recheck_final",
    )
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.usage_event_id == custom_id)
            )
            .mappings()
            .one()
        )
    assert row["result"] == "recheck_final"


def test_usage_adjustment_canonical_fingerprint_is_key_order_independent() -> None:
    """canonical fingerprint：dict 键序不影响指纹，重放复用同一 persisted ID。"""
    engine, ledger = make_ledger()
    local_id = seed_local(engine, ledger)
    first = ledger.append_usage_adjustment(
        referenced_event_id=local_id,
        adjustment_source_namespace="meter_recheck",
        adjustment_source_id="recheck-ord",
        adjustment_allocation_key="ocr-gpu",
        deltas={"page_count": 2, "input_bytes": 10},
        ownership=ownership(),
    )
    replay = ledger.append_usage_adjustment(
        referenced_event_id=local_id,
        adjustment_source_namespace="meter_recheck",
        adjustment_source_id="recheck-ord",
        adjustment_allocation_key="ocr-gpu",
        deltas={"input_bytes": 10, "page_count": 2},  # 键序反转
        ownership=ownership(),
    )
    assert replay == first


def test_adjustment_rows_keep_kind_specific_shape() -> None:
    """usage_adjustment 不带金额/价格字段；cost_adjustment 不带 meter 字段。"""
    engine, ledger = make_ledger()
    local_id = seed_local(engine, ledger)
    provider_id = _seed_provider_event(engine, ledger, execution_id="gen_shape")
    usage_adj = ledger.append_usage_adjustment(
        referenced_event_id=local_id,
        adjustment_source_namespace="meter_recheck",
        adjustment_source_id="recheck-shape",
        adjustment_allocation_key="ocr-gpu",
        deltas={"page_count": 2},
        ownership=ownership(),
    )
    cost_adj = ledger.append_cost_adjustment(
        referenced_event_id=provider_id,
        adjustment_source_namespace="billing",
        adjustment_source_id="bill-shape",
        adjustment_allocation_key="cost",
        amount_delta=Decimal("0.000001"),
        currency_code="USD",
        ownership=ownership(),
    )
    with engine.connect() as connection:
        u_row = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.usage_event_id == usage_adj)
            )
            .mappings()
            .one()
        )
        c_row = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.usage_event_id == cost_adj)
            )
            .mappings()
            .one()
        )
    assert u_row["currency_code"] is None
    assert u_row["estimated_cost_amount"] is None
    assert u_row["estimated_cost_status"] is None
    assert u_row["provider_call_id"] is None
    assert u_row["referenced_usage_event_id"] == local_id
    assert c_row["input_tokens"] is None
    assert c_row["page_count"] is None
    assert c_row["provider_call_id"] is None
    assert c_row["referenced_usage_event_id"] == provider_id


def test_usage_adjustment_same_source_key_different_ref_is_conflict_not_404() -> None:
    """幂等优先级：同四元组已有行 + 新 referenced_event_id 缺失 → 409 而非 404。

    四元组指纹比较先于引用语义校验：同一 source key 引用不存在的引用 ID 是异指纹
    （referenced_event_id 是指纹一部分），必须统一返回 ledger_invariant_conflict，
    不能抢先返回 usage_event_not_found 否定既有事实。
    """
    engine, ledger = make_ledger()
    local_id = seed_local(engine, ledger)
    ledger.append_usage_adjustment(
        referenced_event_id=local_id,
        adjustment_source_namespace="meter_recheck",
        adjustment_source_id="recheck-prio",
        adjustment_allocation_key="ocr-gpu",
        deltas={"page_count": 2},
        ownership=ownership(),
    )
    with pytest.raises(PlatformError) as exc:
        ledger.append_usage_adjustment(
            referenced_event_id="ue_missing",  # 不存在：若先查引用会 404
            adjustment_source_namespace="meter_recheck",
            adjustment_source_id="recheck-prio",  # 同四元组
            adjustment_allocation_key="ocr-gpu",
            deltas={"page_count": 2},
            ownership=ownership(),
        )
    assert exc.value.code == "ledger_invariant_conflict"


def test_usage_adjustment_same_source_key_referencing_adjustment_is_conflict() -> None:
    """同四元组已有行 + 新引用是 adjustment（chain）→ 409 而非 422 chain 拒绝。"""
    engine, ledger = make_ledger()
    local_id = seed_local(engine, ledger)
    adjustment_id = ledger.append_usage_adjustment(
        referenced_event_id=local_id,
        adjustment_source_namespace="meter_recheck",
        adjustment_source_id="recheck-chain",
        adjustment_allocation_key="ocr-gpu",
        deltas={"page_count": 2},
        ownership=ownership(),
    )
    # 首次插入引用 adjustment（chain）→ 422（引用语义校验在首次插入路径生效）
    with pytest.raises(PlatformError) as exc:
        ledger.append_usage_adjustment(
            referenced_event_id=adjustment_id,
            adjustment_source_namespace="meter_recheck",
            adjustment_source_id="recheck-chain2",
            adjustment_allocation_key="ocr-gpu",
            deltas={"page_count": 1},
            ownership=ownership(),
        )
    assert exc.value.code == "validation_error"
    # recheck-chain2 无已插入行 → 当前无 409 依据，仅验证首次插入 chain 校验；
    # 改用先建立一条合法行再换引用验证优先级：
    ledger.append_usage_adjustment(
        referenced_event_id=local_id,
        adjustment_source_namespace="meter_recheck",
        adjustment_source_id="recheck-chain3",
        adjustment_allocation_key="ocr-gpu",
        deltas={"page_count": 1},
        ownership=ownership(),
    )
    # chain3 已有行；再以同一 source key 换引用 adjustment_id（chain）→ 异指纹 409，
    # 而非 422 chain 拒绝。
    with pytest.raises(PlatformError) as exc:
        ledger.append_usage_adjustment(
            referenced_event_id=adjustment_id,
            adjustment_source_namespace="meter_recheck",
            adjustment_source_id="recheck-chain3",  # 同四元组
            adjustment_allocation_key="ocr-gpu",
            deltas={"page_count": 1},
            ownership=ownership(),
        )
    assert exc.value.code == "ledger_invariant_conflict"


def test_cost_adjustment_same_source_key_different_currency_is_conflict() -> None:
    """同四元组已有 cost_adjustment + 新 currency 与被引用 usage 不一致 → 409 而非 422。"""
    engine, ledger = make_ledger()
    provider_id = _seed_provider_event(engine, ledger, execution_id="gen_priocur")
    ledger.append_cost_adjustment(
        referenced_event_id=provider_id,
        adjustment_source_namespace="billing",
        adjustment_source_id="bill-prio",
        adjustment_allocation_key="cost",
        amount_delta=Decimal("0.000001"),
        currency_code="USD",
        ownership=ownership(),
    )
    with pytest.raises(PlatformError) as exc:
        ledger.append_cost_adjustment(
            referenced_event_id=provider_id,
            adjustment_source_namespace="billing",
            adjustment_source_id="bill-prio",  # 同四元组
            adjustment_allocation_key="cost",
            amount_delta=Decimal("0.000001"),
            currency_code="CNY",  # 与被引用 usage(USD) 不一致：若先查引用会 422
            ownership=ownership(),
        )
    assert exc.value.code == "ledger_invariant_conflict"


def test_adjustment_same_source_key_different_ownership_is_conflict() -> None:
    """同四元组已有行 + 不同 ownership（异指纹）→ 409；相同 ownership 重放 → 原 ID。"""
    engine, ledger = make_ledger()
    local_id = seed_local(engine, ledger)
    first = ledger.append_usage_adjustment(
        referenced_event_id=local_id,
        adjustment_source_namespace="meter_recheck",
        adjustment_source_id="recheck-own-prio",
        adjustment_allocation_key="ocr-gpu",
        deltas={"page_count": 2},
        ownership=ownership(),
    )
    different_owner = OwnershipSnapshot(
        actor_user_id="u2",
        actor_role_snapshot="ops",
        actor_department_id_snapshot=None,
        quota_subject_user_id="u2",
        cost_center_key="ops:u2",
    )
    with pytest.raises(PlatformError) as exc:
        ledger.append_usage_adjustment(
            referenced_event_id=local_id,
            adjustment_source_namespace="meter_recheck",
            adjustment_source_id="recheck-own-prio",  # 同四元组
            adjustment_allocation_key="ocr-gpu",
            deltas={"page_count": 2},
            ownership=different_owner,
        )
    assert exc.value.code == "ledger_invariant_conflict"
    # 相同指纹（含 ownership）重放 → 复用 persisted ID
    replay = ledger.append_usage_adjustment(
        referenced_event_id=local_id,
        adjustment_source_namespace="meter_recheck",
        adjustment_source_id="recheck-own-prio",
        adjustment_allocation_key="ocr-gpu",
        deltas={"page_count": 2},
        ownership=ownership(),
    )
    assert replay == first


def test_adjustment_same_source_key_different_extra_is_conflict() -> None:
    """同四元组已有行 + 不同 extra（delta 值）→ 409；cost 不同 amount → 409。"""
    engine, ledger = make_ledger()
    local_id = seed_local(engine, ledger)
    provider_id = _seed_provider_event(engine, ledger, execution_id="gen_prioextra")
    ledger.append_usage_adjustment(
        referenced_event_id=local_id,
        adjustment_source_namespace="meter_recheck",
        adjustment_source_id="recheck-extra",
        adjustment_allocation_key="ocr-gpu",
        deltas={"page_count": 2},
        ownership=ownership(),
    )
    with pytest.raises(PlatformError) as exc:
        ledger.append_usage_adjustment(
            referenced_event_id=local_id,
            adjustment_source_namespace="meter_recheck",
            adjustment_source_id="recheck-extra",  # 同四元组
            adjustment_allocation_key="ocr-gpu",
            deltas={"page_count": 5},  # 异 extra → 异指纹
            ownership=ownership(),
        )
    assert exc.value.code == "ledger_invariant_conflict"
    ledger.append_cost_adjustment(
        referenced_event_id=provider_id,
        adjustment_source_namespace="billing",
        adjustment_source_id="bill-extra",
        adjustment_allocation_key="cost",
        amount_delta=Decimal("0.000001"),
        currency_code="USD",
        ownership=ownership(),
    )
    with pytest.raises(PlatformError) as exc:
        ledger.append_cost_adjustment(
            referenced_event_id=provider_id,
            adjustment_source_namespace="billing",
            adjustment_source_id="bill-extra",  # 同四元组
            adjustment_allocation_key="cost",
            amount_delta=Decimal("0.000002"),  # 异金额 → 异指纹
            currency_code="USD",
            ownership=ownership(),
        )
    assert exc.value.code == "ledger_invariant_conflict"


def test_adjustment_same_source_key_different_result_is_conflict() -> None:
    """同四元组已有行 + 不同 result（异指纹）→ 409。"""
    engine, ledger = make_ledger()
    local_id = seed_local(engine, ledger)
    ledger.append_usage_adjustment(
        referenced_event_id=local_id,
        adjustment_source_namespace="meter_recheck",
        adjustment_source_id="recheck-res-prio",
        adjustment_allocation_key="ocr-gpu",
        deltas={"page_count": 2},
        ownership=ownership(),
        result="adjusted",
    )
    with pytest.raises(PlatformError) as exc:
        ledger.append_usage_adjustment(
            referenced_event_id=local_id,
            adjustment_source_namespace="meter_recheck",
            adjustment_source_id="recheck-res-prio",  # 同四元组
            adjustment_allocation_key="ocr-gpu",
            deltas={"page_count": 2},
            ownership=ownership(),
            result="recheck_final",  # 异 result → 异指纹
        )
    assert exc.value.code == "ledger_invariant_conflict"
