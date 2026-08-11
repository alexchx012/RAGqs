"""UsageLedger provider 生命周期 / usage 提交测试（Task 5，正式 spec 修订版 + review agent-11）。

语义（正式 spec 优先于旧 task brief/plan）：
- 每个物理发送使用调用方稳定 provider_call_id；prepare/dispatching 各自独立短事务
  且发送前提交；网络 I/O 绝不持 DB 事务；每次重试使用新 ID。
- 结果已知（成功或已收到 4xx/503 等失败）→ 独立事务原子 completed + provider_usage；
  sent=False → not_sent 无 usage；sent=True 但结果无法确认 → unknown。
- complete/recover 先检查既有 completed+usage：同完整 fingerprint 复用 persisted ID，
  异事实 409 ledger_invariant_conflict；首次才状态转换；必须返回 persisted ID。
- actual send time：mark_dispatching 显式持久化 started_at_utc；completion 用它选价/
  归期（不默认 dispatching_at）；缺失 → 422。
- measurement_sources：key 必须在固定 meter 集合内，每个非 null meter 必须有合法
  source；null 与 0 严格区分。
- local usage：started_at_utc 必填；四元组唯一；本地 V1 无价格金额；moving clock
  重放仍复用 persisted ID。
- adjustment/cost adjustment：追加式、稳定 source/allocation/referenced event，
  同指纹重放/异指纹冲突；只允许引用原始 event kind（禁止 adjustment chain）；
  currency 正式 ISO 校验；amount 适配 Numeric(30,10)；usage delta 限制 BigInteger。
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
from app.usage.schema import provider_call_table, usage_event_table, usage_metadata

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


def _prepare_provider_call(ledger: UsageLedger, **facts) -> str:
    if facts.get("attempt_id") is None and facts.get("generation_id") is None:
        facts["generation_id"] = f"generation:{facts['execution_id']}"
    facts.setdefault("deadline_utc", NOW + timedelta(hours=1))
    return ledger.prepare_provider_call(**facts)


def seed(
    engine,
    ledger,
    *,
    operation: str = "generate",
    rate: str = "0.000020",
    effective_from_utc: datetime = NOW,
) -> None:
    with engine.begin() as connection:
        ledger.calendar.lock_or_verify(connection)
        ledger.prices.register(
            connection,
            provider="dashscope",
            model="qwen-plus",
            operation=operation,
            currency_code="USD",
            lines=[
                {"meter": "input_tokens", "unit": "token", "rate": Decimal(rate)},
                {"meter": "output_tokens", "unit": "token", "rate": Decimal("0.000060")},
            ],
            effective_from_utc=effective_from_utc,
        )


def ownership() -> OwnershipSnapshot:
    return OwnershipSnapshot(
        actor_user_id="u1",
        actor_role_snapshot="user",
        actor_department_id_snapshot=None,
        quota_subject_user_id="u1",
        cost_center_key="user:u1",
        space_id="personal:u1",
        space_kind="personal",
        space_owner_user_id="u1",
        authorization_version=1,
        fence_token=7,
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


def test_prepare_provider_call_requires_attempt_or_generation_identity() -> None:
    engine, ledger = make_ledger()

    with pytest.raises(PlatformError) as failure:
        ledger.prepare_provider_call(
            provider="dashscope",
            model="qwen-plus",
            operation="generate",
            execution_kind="generation",
            execution_id="gen-missing-identity",
            deadline_utc=NOW + timedelta(minutes=5),
            request_fingerprint="fp-missing-identity",
        )

    assert failure.value.status_code == 422
    assert failure.value.code == "validation_error"
    with engine.connect() as connection:
        assert connection.execute(select(provider_call_table)).all() == []


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


def test_complete_writes_single_provider_usage() -> None:
    engine, ledger = make_ledger()
    seed(engine, ledger)
    call_id = _prepare_provider_call(
        ledger,
        provider="dashscope",
        model="qwen-plus",
        operation="generate",
        execution_kind="generation",
        execution_id="gen_1",
        attempt_id="gen_1:1",
        request_fingerprint="fp-1",
    )
    ledger.mark_dispatching(call_id, started_at_provider=NOW)
    event_id = ledger.complete_provider_call(
        provider_call_id=call_id,
        measurement=measurement(),
        ownership=ownership(),
        result="succeeded",
    )
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.provider_call_id == call_id)
            )
            .mappings()
            .all()
        )
        calls = (
            connection.execute(
                select(provider_call_table).where(provider_call_table.c.provider_call_id == call_id)
            )
            .mappings()
            .all()
        )
    assert len(rows) == 1
    assert rows[0]["event_kind"] == "provider_usage"
    assert rows[0]["effective_period"] == "2026-08"
    assert rows[0]["estimated_cost_status"] == "complete"
    # 正式公式：input 1000 blocks × 0.00002 = 0.02；output 200 blocks × 0.00006 = 0.012
    # → 合计 0.032（旧 brief 示例的 0.000032 为算术错误，仅作结构参考）。
    assert Decimal(str(rows[0]["estimated_cost_amount"])) == Decimal("0.032")
    assert rows[0]["attempt_id"] == "gen_1:1"
    assert rows[0]["cost_center_key"] == "user:u1"
    assert rows[0]["usage_event_id"] == event_id
    assert calls[0]["status"] == "completed"
    # SQLite 回读 naive UTC；与写入的 aware UTC 等价
    assert calls[0]["started_at_utc"] == NOW.replace(tzinfo=None)


def test_known_failure_after_send_records_usage() -> None:
    """C4：sent=True 的已知失败（503）→ completed + usage（result=failed）。"""
    engine, ledger = make_ledger()
    seed(engine, ledger)
    call_id = _prepare_provider_call(
        ledger,
        provider="dashscope",
        model="qwen-plus",
        operation="generate",
        execution_kind="generation",
        execution_id="gen_2",
        request_fingerprint="fp-2",
    )
    ledger.mark_dispatching(call_id, started_at_provider=NOW)
    event_id = ledger.complete_provider_call(
        provider_call_id=call_id,
        measurement=measurement(),
        ownership=ownership(),
        result="failed",
    )
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.provider_call_id == call_id)
            )
            .mappings()
            .all()
        )
    assert len(rows) == 1
    assert rows[0]["result"] == "failed"
    assert event_id == rows[0]["usage_event_id"]


def test_not_sent_creates_no_usage() -> None:
    """C4：sent=False → not_sent，无 usage。"""
    engine, ledger = make_ledger()
    seed(engine, ledger)
    call_id = _prepare_provider_call(
        ledger,
        provider="dashscope",
        model="qwen-plus",
        operation="generate",
        execution_kind="generation",
        execution_id="gen_3",
        request_fingerprint="fp-3",
    )
    ledger.mark_dispatching(call_id, started_at_provider=NOW)
    ledger.mark_not_sent(call_id)
    with engine.connect() as connection:
        rows = connection.execute(select(usage_event_table)).mappings().all()
        calls = (
            connection.execute(
                select(provider_call_table).where(provider_call_table.c.provider_call_id == call_id)
            )
            .mappings()
            .all()
        )
    assert rows == []
    assert calls[0]["status"] == "not_sent"


def test_repeat_complete_same_fingerprint_reuses_different_conflicts() -> None:
    """C3：同指纹幂等复用 persisted ID；异指纹 409 ledger_invariant_conflict。"""
    engine, ledger = make_ledger()
    seed(engine, ledger)
    call_id = _prepare_provider_call(
        ledger,
        provider="dashscope",
        model="qwen-plus",
        operation="generate",
        execution_kind="generation",
        execution_id="gen_4",
        request_fingerprint="fp-4",
    )
    ledger.mark_dispatching(call_id, started_at_provider=NOW)
    first = ledger.complete_provider_call(
        provider_call_id=call_id,
        measurement=measurement(),
        ownership=ownership(),
        result="succeeded",
    )
    second = ledger.complete_provider_call(
        provider_call_id=call_id,
        measurement=measurement(),
        ownership=ownership(),
        result="succeeded",
    )
    assert second == first
    other = ProviderMeasurement(
        input_tokens=999,
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
    with pytest.raises(PlatformError) as exc:
        ledger.complete_provider_call(
            provider_call_id=call_id,
            measurement=other,
            ownership=ownership(),
            result="succeeded",
        )
    assert exc.value.code == "ledger_invariant_conflict"


def test_unknown_call_recovers_idempotently() -> None:
    engine, ledger = make_ledger()
    seed(engine, ledger)
    call_id = _prepare_provider_call(
        ledger,
        provider="dashscope",
        model="qwen-plus",
        operation="generate",
        execution_kind="generation",
        execution_id="gen_5",
        request_fingerprint="fp-5",
    )
    ledger.mark_dispatching(call_id, started_at_provider=NOW)
    ledger.mark_unknown(call_id)
    with pytest.raises(PlatformError) as state_exc:
        ledger.mark_dispatching(call_id, started_at_provider=NOW)  # unknown 后不可 dispatching
    assert state_exc.value.code == "provider_call_state_conflict"
    first = ledger.recover_unknown_call(
        provider_call_id=call_id,
        measurement=measurement(),
        ownership=ownership(),
        result="succeeded",
    )
    second = ledger.recover_unknown_call(
        provider_call_id=call_id,
        measurement=measurement(),
        ownership=ownership(),
        result="succeeded",
    )
    assert second == first
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.provider_call_id == call_id)
            )
            .mappings()
            .all()
        )
    assert len(rows) == 1


def test_local_usage_aggregate_unique_per_scope_and_no_price() -> None:
    engine, ledger = make_ledger()
    with engine.begin() as connection:
        ledger.calendar.lock_or_verify(connection)
    first = ledger.submit_local_usage(
        execution_kind="ingestion",
        execution_id="attempt-1",
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
    replay = ledger.submit_local_usage(
        execution_kind="ingestion",
        execution_id="attempt-1",
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
    assert replay == first
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.usage_event_id == first)
            )
            .mappings()
            .one()
        )
    assert row["event_kind"] == "local_usage"
    assert row["price_version_id"] is None
    assert row["estimated_cost_status"] is None
    assert row["effective_period"] == "2026-08"
    assert row["page_count"] == 10


def test_measurement_sources_whitelist_enforced() -> None:
    engine, ledger = make_ledger()
    seed(engine, ledger)
    call_id = _prepare_provider_call(
        ledger,
        provider="dashscope",
        model="qwen-plus",
        operation="generate",
        execution_kind="generation",
        execution_id="gen_6",
        request_fingerprint="fp-6",
    )
    ledger.mark_dispatching(call_id, started_at_provider=NOW)
    bad = ProviderMeasurement(
        input_tokens=1,
        output_tokens=None,
        prompt_cache_hit_tokens=None,
        prompt_cache_miss_tokens=None,
        reasoning_tokens=None,
        image_count=None,
        visual_input_tokens=None,
        embedding_input_tokens=None,
        vector_count=None,
        measurement_sources={"input_tokens": "made_up"},
    )
    with pytest.raises(PlatformError) as exc:
        ledger.complete_provider_call(
            provider_call_id=call_id,
            measurement=bad,
            ownership=ownership(),
            result="succeeded",
        )
    assert exc.value.code == "validation_error"


def test_measurement_sources_missing_for_non_null_meter_rejected() -> None:
    """纠偏：measurement_sources 每个非 null meter 必须有合法 source。"""
    engine, ledger = make_ledger()
    seed(engine, ledger)
    call_id = _prepare_provider_call(
        ledger,
        provider="dashscope",
        model="qwen-plus",
        operation="generate",
        execution_kind="generation",
        execution_id="gen_6b",
        request_fingerprint="fp-6b",
    )
    ledger.mark_dispatching(call_id, started_at_provider=NOW)
    bad = ProviderMeasurement(
        input_tokens=1,
        output_tokens=None,
        prompt_cache_hit_tokens=None,
        prompt_cache_miss_tokens=None,
        reasoning_tokens=None,
        image_count=None,
        visual_input_tokens=None,
        embedding_input_tokens=None,
        vector_count=None,
        measurement_sources={},
    )
    with pytest.raises(PlatformError) as exc:
        ledger.complete_provider_call(
            provider_call_id=call_id,
            measurement=bad,
            ownership=ownership(),
            result="succeeded",
        )
    assert exc.value.code == "validation_error"


def test_measurement_sources_rejects_unknown_meter_key() -> None:
    """review agent-11 #9：measurement_sources 拒绝固定 meter 集合外 key。"""
    engine, ledger = make_ledger()
    seed(engine, ledger)
    call_id = _prepare_provider_call(
        ledger,
        provider="dashscope",
        model="qwen-plus",
        operation="generate",
        execution_kind="generation",
        execution_id="gen_6c",
        request_fingerprint="fp-6c",
    )
    ledger.mark_dispatching(call_id, started_at_provider=NOW)
    bad = ProviderMeasurement(
        input_tokens=1,
        output_tokens=None,
        prompt_cache_hit_tokens=None,
        prompt_cache_miss_tokens=None,
        reasoning_tokens=None,
        image_count=None,
        visual_input_tokens=None,
        embedding_input_tokens=None,
        vector_count=None,
        measurement_sources={"input_tokens": "provider_reported", "made_up": "estimated"},
    )
    with pytest.raises(PlatformError) as exc:
        ledger.complete_provider_call(
            provider_call_id=call_id,
            measurement=bad,
            ownership=ownership(),
            result="succeeded",
        )
    assert exc.value.code == "validation_error"


def test_stale_dispatching_scan_lists_old_calls() -> None:
    clock = MutableClock(NOW - timedelta(seconds=1))
    engine, ledger = make_ledger(clock=clock)
    seed(engine, ledger, effective_from_utc=NOW - timedelta(minutes=1))
    call_id = _prepare_provider_call(
        ledger,
        provider="dashscope",
        model="qwen-plus",
        operation="generate",
        execution_kind="generation",
        execution_id="gen_7",
        attempt_id="attempt-gen-7",
        deadline_utc=NOW,
        request_fingerprint="fp-7",
    )
    ledger.mark_dispatching(call_id, started_at_provider=NOW - timedelta(seconds=1))
    clock.now = NOW
    stale = ledger.list_stale_dispatching(older_than_utc=NOW + timedelta(seconds=1))
    assert [row["provider_call_id"] for row in stale] == [call_id]


def test_provider_call_deadline_is_persisted_and_immutable_on_replay() -> None:
    engine, ledger = make_ledger()
    deadline = NOW + timedelta(minutes=5)
    facts = {
        "provider": "dashscope",
        "model": "qwen-plus",
        "operation": "generate",
        "execution_kind": "generation",
        "execution_id": "gen-deadline",
        "provider_call_id": "pc-deadline",
        "attempt_id": "attempt-deadline",
        "deadline_utc": deadline,
        "request_fingerprint": "fp-deadline",
    }

    assert _prepare_provider_call(ledger, **facts) == "pc-deadline"
    assert _prepare_provider_call(ledger, **facts) == "pc-deadline"
    with engine.connect() as connection:
        persisted = connection.execute(
            select(provider_call_table.c.deadline_utc).where(
                provider_call_table.c.provider_call_id == "pc-deadline"
            )
        ).scalar_one()
    assert persisted == deadline.replace(tzinfo=None)

    changed = {**facts, "deadline_utc": deadline + timedelta(seconds=1)}
    with pytest.raises(PlatformError) as conflict:
        _prepare_provider_call(ledger, **changed)
    assert conflict.value.code == "ledger_invariant_conflict"
    assert conflict.value.details["field"] == "deadline_utc"


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
    seed(engine, ledger)
    call_id = _prepare_provider_call(
        ledger,
        provider="dashscope",
        model="qwen-plus",
        operation="generate",
        execution_kind="generation",
        execution_id="gen_cost",
        request_fingerprint="fp-cost",
    )
    ledger.mark_dispatching(call_id, started_at_provider=NOW)
    provider_id = ledger.complete_provider_call(
        provider_call_id=call_id,
        measurement=measurement(),
        ownership=ownership(),
        result="succeeded",
    )
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


def test_usage_adjustment_rejects_referencing_adjustment_chain() -> None:
    """review agent-11 #9：usage_adjustment 只能引用原始 provider/local，禁止 adjustment chain。"""
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
    with pytest.raises(PlatformError) as exc:
        ledger.append_usage_adjustment(
            referenced_event_id=adjustment_id,
            adjustment_source_namespace="meter_recheck",
            adjustment_source_id="recheck-2",
            adjustment_allocation_key="ocr-gpu",
            deltas={"page_count": 1},
            ownership=ownership(),
        )
    assert exc.value.code == "validation_error"


def test_cost_adjustment_rejects_referencing_local_event() -> None:
    """review agent-11 #9：cost_adjustment 至少引用 provider original，不能引用 local。"""
    engine, ledger = make_ledger()
    local_id = seed_local(engine, ledger)
    with pytest.raises(PlatformError) as exc:
        ledger.append_cost_adjustment(
            referenced_event_id=local_id,
            adjustment_source_namespace="billing",
            adjustment_source_id="bill-1",
            adjustment_allocation_key="cost",
            amount_delta=Decimal("0.000001"),
            currency_code="USD",
            ownership=ownership(),
        )
    assert exc.value.code == "validation_error"


def test_cost_adjustment_validates_currency_and_amount() -> None:
    """review agent-11 #9：currency 正式 ISO 校验；amount 有限且适配 Numeric(30,10)。"""
    engine, ledger = make_ledger()
    local_id = seed_local(engine, ledger, execution_id="attempt-3")
    with pytest.raises(PlatformError) as exc:
        ledger.append_cost_adjustment(
            referenced_event_id=local_id,
            adjustment_source_namespace="billing",
            adjustment_source_id="bill-1",
            adjustment_allocation_key="cost",
            amount_delta=Decimal("0.000001"),
            currency_code="NOTACODE",
            ownership=ownership(),
        )
    assert exc.value.code == "validation_error"
    with pytest.raises(PlatformError) as exc:
        ledger.append_cost_adjustment(
            referenced_event_id=local_id,
            adjustment_source_namespace="billing",
            adjustment_source_id="bill-1",
            adjustment_allocation_key="cost",
            amount_delta=Decimal("1e100"),
            currency_code="USD",
            ownership=ownership(),
        )
    assert exc.value.code == "validation_error"
    with pytest.raises(PlatformError) as exc:
        ledger.append_cost_adjustment(
            referenced_event_id=local_id,
            adjustment_source_namespace="billing",
            adjustment_source_id="bill-1",
            adjustment_allocation_key="cost",
            amount_delta=Decimal("0.00000000001"),
            currency_code="USD",
            ownership=ownership(),
        )
    assert exc.value.code == "validation_error"


def test_usage_delta_limited_to_bigint_range() -> None:
    """review agent-11 #9：usage delta 限制 BigInteger 范围。"""
    engine, ledger = make_ledger()
    local_id = seed_local(engine, ledger, execution_id="attempt-4")
    with pytest.raises(PlatformError) as exc:
        ledger.append_usage_adjustment(
            referenced_event_id=local_id,
            adjustment_source_namespace="meter_recheck",
            adjustment_source_id="recheck-big",
            adjustment_allocation_key="ocr-gpu",
            deltas={"page_count": 2**63},
            ownership=ownership(),
        )
    assert exc.value.code == "validation_error"


def _seed_provider_event(engine, ledger, *, execution_id: str) -> str:
    seed(engine, ledger)
    call_id = _prepare_provider_call(
        ledger,
        provider="dashscope",
        model="qwen-plus",
        operation="generate",
        execution_kind="generation",
        execution_id=execution_id,
        request_fingerprint=f"fp-{execution_id}",
    )
    ledger.mark_dispatching(call_id, started_at_provider=NOW)
    return ledger.complete_provider_call(
        provider_call_id=call_id,
        measurement=measurement(),
        ownership=ownership(),
        result="succeeded",
    )


def test_usage_adjustment_meter_must_match_referenced_event_type() -> None:
    """二轮复审 Important 4：usage_adjustment 的 meter 必须属于被引用事件类型。
    local_usage 引用 input_tokens（provider meter）→ 422；provider_usage 引用
    page_count（local meter）→ 422。"""
    engine, ledger = make_ledger()
    local_id = seed_local(engine, ledger, execution_id="attempt-m1")
    with pytest.raises(PlatformError) as exc:
        ledger.append_usage_adjustment(
            referenced_event_id=local_id,
            adjustment_source_namespace="meter_recheck",
            adjustment_source_id="recheck-m1",
            adjustment_allocation_key="ocr-gpu",
            deltas={"input_tokens": 5},
            ownership=ownership(),
        )
    assert exc.value.code == "validation_error"
    provider_id = _seed_provider_event(engine, ledger, execution_id="gen_m1")
    with pytest.raises(PlatformError) as exc:
        ledger.append_usage_adjustment(
            referenced_event_id=provider_id,
            adjustment_source_namespace="meter_recheck",
            adjustment_source_id="recheck-m2",
            adjustment_allocation_key="gen-gpu",
            deltas={"page_count": 5},
            ownership=ownership(),
        )
    assert exc.value.code == "validation_error"
    # 兼容 meter（local 引用 page_count）→ 允许
    ok_id = ledger.append_usage_adjustment(
        referenced_event_id=local_id,
        adjustment_source_namespace="meter_recheck",
        adjustment_source_id="recheck-m3",
        adjustment_allocation_key="ocr-gpu",
        deltas={"page_count": 2},
        ownership=ownership(),
    )
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.usage_event_id == ok_id)
            )
            .mappings()
            .one()
        )
    assert row["page_count"] == 2


def test_cost_adjustment_currency_must_match_referenced_provider_usage() -> None:
    """二轮复审 Important 4：cost_adjustment 的 currency 必须与被引用 provider usage
    一致（无跨币种模型）。"""
    engine, ledger = make_ledger()
    provider_id = _seed_provider_event(engine, ledger, execution_id="gen_m2")
    with pytest.raises(PlatformError) as exc:
        ledger.append_cost_adjustment(
            referenced_event_id=provider_id,
            adjustment_source_namespace="billing",
            adjustment_source_id="bill-m1",
            adjustment_allocation_key="cost",
            amount_delta=Decimal("0.000001"),
            currency_code="CNY",  # 被引用 usage 为 USD → 拒绝
            ownership=ownership(),
        )
    assert exc.value.code == "validation_error"
    # 一致 currency → 允许
    ok_id = ledger.append_cost_adjustment(
        referenced_event_id=provider_id,
        adjustment_source_namespace="billing",
        adjustment_source_id="bill-m2",
        adjustment_allocation_key="cost",
        amount_delta=Decimal("0.000001"),
        currency_code="USD",
        ownership=ownership(),
    )
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.usage_event_id == ok_id)
            )
            .mappings()
            .one()
        )
    assert row["currency_code"] == "USD"


def test_mark_dispatching_requires_valid_started_at() -> None:
    """review agent-11 #3：actual send time 在 dispatch 入口显式持久化。"""
    engine, ledger = make_ledger()
    call_id = _prepare_provider_call(
        ledger,
        provider="dashscope",
        model="qwen-plus",
        operation="generate",
        execution_kind="generation",
        execution_id="gen_8",
        request_fingerprint="fp-8",
    )
    with pytest.raises(PlatformError) as exc:
        ledger.mark_dispatching(call_id, started_at_provider="2026-08-05T12:00:00Z")  # type: ignore[arg-type]
    assert exc.value.code == "validation_error"
    # 未 dispatch（无 started）直接 complete → 状态冲突
    with pytest.raises(PlatformError) as exc:
        ledger.complete_provider_call(
            provider_call_id=call_id,
            measurement=measurement(),
            ownership=ownership(),
            result="succeeded",
        )
    assert exc.value.code == "provider_call_state_conflict"


def test_prepare_replay_conflicts_on_immutable_field_change() -> None:
    """review agent-11 #7：重放比较全部不可变 provider_call 字段，不仅 request_fingerprint。"""
    engine, ledger = make_ledger()
    _prepare_provider_call(
        ledger,
        provider="dashscope",
        model="qwen-plus",
        operation="generate",
        execution_kind="generation",
        execution_id="gen_9",
        provider_call_id="pc-replay-1",
        attempt_id="gen_9:1",
        request_fingerprint="fp-9",
    )
    with pytest.raises(PlatformError) as exc:
        _prepare_provider_call(
            ledger,
            provider="dashscope",
            model="qwen-plus",
            operation="generate",
            execution_kind="generation",
            execution_id="gen_9_OTHER",
            provider_call_id="pc-replay-1",
            attempt_id="gen_9:1",
            request_fingerprint="fp-9",
        )
    assert exc.value.code == "ledger_invariant_conflict"
    # 完全一致的重放 → 返回同一 persisted ID
    replay_id = _prepare_provider_call(
        ledger,
        provider="dashscope",
        model="qwen-plus",
        operation="generate",
        execution_kind="generation",
        execution_id="gen_9",
        provider_call_id="pc-replay-1",
        attempt_id="gen_9:1",
        request_fingerprint="fp-9",
    )
    assert replay_id == "pc-replay-1"


def test_complete_uses_actual_started_for_price_and_effective_period() -> None:
    """review agent-11 #3/#13：completion 用 actual started 选价/归期，不默认 dispatching_at。"""
    engine, ledger = make_ledger()  # clock NOW = 2026-08-05（Asia/Shanghai → 2026-08）
    seed(engine, ledger, effective_from_utc=JULY)
    call_id = _prepare_provider_call(
        ledger,
        provider="dashscope",
        model="qwen-plus",
        operation="generate",
        execution_kind="generation",
        execution_id="gen_10",
        request_fingerprint="fp-10",
    )
    ledger.mark_dispatching(call_id, started_at_provider=JULY)  # actual started 在 7 月
    ledger.complete_provider_call(
        provider_call_id=call_id,
        measurement=measurement(),
        ownership=ownership(),
        result="succeeded",
    )
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.provider_call_id == call_id)
            )
            .mappings()
            .one()
        )
    assert row["effective_period"] == "2026-07"  # effective 按 actual started
    assert row["recorded_period"] == "2026-08"  # recorded 按 DB now
    assert row["effective_at_utc"] == JULY.replace(tzinfo=None)
    assert row["started_at_utc"] == JULY.replace(tzinfo=None)


def test_complete_rejects_started_mismatch_with_persisted_value() -> None:
    """二轮复审 Important 3：显式 started 与持久值不一致 → 422 拒绝，不静默覆盖。"""
    engine, ledger = make_ledger()
    seed(engine, ledger)
    call_id = _prepare_provider_call(
        ledger,
        provider="dashscope",
        model="qwen-plus",
        operation="generate",
        execution_kind="generation",
        execution_id="gen_11",
        request_fingerprint="fp-11",
    )
    ledger.mark_dispatching(call_id, started_at_provider=NOW)
    with pytest.raises(PlatformError) as exc:
        ledger.complete_provider_call(
            provider_call_id=call_id,
            measurement=measurement(),
            ownership=ownership(),
            result="succeeded",
            started_at_utc=JULY,
        )
    assert exc.value.code == "validation_error"
    # 持久值未被覆盖，调用仍 dispatching
    with engine.connect() as connection:
        call = (
            connection.execute(
                select(provider_call_table).where(provider_call_table.c.provider_call_id == call_id)
            )
            .mappings()
            .one()
        )
    assert call["status"] == "dispatching"
    assert call["started_at_utc"] == NOW.replace(tzinfo=None)


def test_not_sent_clears_started_at() -> None:
    """二轮复审 Important 3：not_sent 清除 started_at_utc（从未实际发送）。"""
    engine, ledger = make_ledger()
    seed(engine, ledger)
    call_id = _prepare_provider_call(
        ledger,
        provider="dashscope",
        model="qwen-plus",
        operation="generate",
        execution_kind="generation",
        execution_id="gen_12",
        request_fingerprint="fp-12",
    )
    ledger.mark_dispatching(call_id, started_at_provider=NOW)
    ledger.mark_not_sent(call_id)
    with engine.connect() as connection:
        call = (
            connection.execute(
                select(provider_call_table).where(provider_call_table.c.provider_call_id == call_id)
            )
            .mappings()
            .one()
        )
    assert call["status"] == "not_sent"
    assert call["started_at_utc"] is None
    assert call["unknown_at_utc"] is None


def test_price_boundary_uses_two_price_versions() -> None:
    """Minor：两个 price_version（7 月 open + 8 月 supersede）边界——按 actual started 选价。"""
    engine, ledger = make_ledger()
    with engine.begin() as connection:
        ledger.calendar.lock_or_verify(connection)
        v1 = ledger.prices.register(
            connection,
            provider="dashscope",
            model="qwen-plus",
            operation="generate",
            currency_code="USD",
            lines=[{"meter": "input_tokens", "unit": "token", "rate": Decimal("0.000020")}],
            effective_from_utc=JULY,
        )
        v2 = ledger.prices.register(
            connection,
            provider="dashscope",
            model="qwen-plus",
            operation="generate",
            currency_code="USD",
            lines=[{"meter": "input_tokens", "unit": "token", "rate": Decimal("0.000200")}],
            effective_from_utc=NOW,  # supersede：8 月起新费率
            supersedes_version_id=v1.id,
        )
    # actual started 在 7 月 → 用 v1（0.000020）：1000 × 0.000020 = 0.02
    call_old = _prepare_provider_call(
        ledger,
        provider="dashscope",
        model="qwen-plus",
        operation="generate",
        execution_kind="generation",
        execution_id="gen_old",
        request_fingerprint="fp-old",
    )
    ledger.mark_dispatching(call_old, started_at_provider=JULY)
    ledger.complete_provider_call(
        provider_call_id=call_old,
        measurement=measurement(),
        ownership=ownership(),
        result="succeeded",
    )
    # actual started 在 8 月 → 用 v2（0.000200）：1000 × 0.000200 = 0.2
    call_new = _prepare_provider_call(
        ledger,
        provider="dashscope",
        model="qwen-plus",
        operation="generate",
        execution_kind="generation",
        execution_id="gen_new",
        request_fingerprint="fp-new",
    )
    ledger.mark_dispatching(call_new, started_at_provider=NOW)
    ledger.complete_provider_call(
        provider_call_id=call_new,
        measurement=measurement(),
        ownership=ownership(),
        result="succeeded",
    )
    with engine.connect() as connection:
        old_row = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.provider_call_id == call_old)
            )
            .mappings()
            .one()
        )
        new_row = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.provider_call_id == call_new)
            )
            .mappings()
            .one()
        )
    assert old_row["price_version_id"] == v1.id
    assert old_row["effective_period"] == "2026-07"
    assert Decimal(str(old_row["estimated_cost_amount"])) == Decimal("0.02")
    assert new_row["price_version_id"] == v2.id
    assert new_row["effective_period"] == "2026-08"
    assert Decimal(str(new_row["estimated_cost_amount"])) == Decimal("0.2")


def test_local_usage_replay_with_moving_clock_reuses_persisted_id() -> None:
    """review agent-11 #5/#13：local fingerprint 含稳定 started，moving clock 重放不冲突。"""
    mutable_clock = MutableClock(now=NOW)
    engine, ledger = make_ledger(clock=mutable_clock)
    with engine.begin() as connection:
        ledger.calendar.lock_or_verify(connection)
    first = ledger.submit_local_usage(
        execution_kind="ingestion",
        execution_id="attempt-mv",
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
    mutable_clock.now = NOW + timedelta(days=1)  # recorded 时间推进
    replay = ledger.submit_local_usage(
        execution_kind="ingestion",
        execution_id="attempt-mv",
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
    assert replay == first
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.usage_event_id == first)
            )
            .mappings()
            .one()
        )
    assert row["recorded_at_utc"] == NOW.replace(
        tzinfo=None
    )  # 重放复用 persisted 行，未改写记录时间


def test_dispatch_relocks_until_dynamic_started_price_is_stable() -> None:
    """started callback 跨三个价格版本时，必须锁定并持久化最终稳定的 V3 样本。"""

    engine, ledger = make_ledger()
    v1_started = JULY + timedelta(days=1)
    v2_started = JULY + timedelta(days=6)
    v3_started = JULY + timedelta(days=11)
    with engine.begin() as connection:
        ledger.calendar.lock_or_verify(connection)
        v1 = ledger.prices.register(
            connection,
            provider="dashscope",
            model="qwen-plus",
            operation="generate",
            currency_code="USD",
            lines=[{"meter": "input_tokens", "unit": "token", "rate": Decimal("0.000010")}],
            effective_from_utc=JULY,
        )
        v2 = ledger.prices.register(
            connection,
            provider="dashscope",
            model="qwen-plus",
            operation="generate",
            currency_code="USD",
            lines=[{"meter": "input_tokens", "unit": "token", "rate": Decimal("0.000020")}],
            effective_from_utc=JULY + timedelta(days=5),
            supersedes_version_id=v1.id,
        )
        v3 = ledger.prices.register(
            connection,
            provider="dashscope",
            model="qwen-plus",
            operation="generate",
            currency_code="USD",
            lines=[{"meter": "input_tokens", "unit": "token", "rate": Decimal("0.000030")}],
            effective_from_utc=JULY + timedelta(days=10),
            supersedes_version_id=v2.id,
        )
    call_id = _prepare_provider_call(
        ledger,
        provider="dashscope",
        model="qwen-plus",
        operation="generate",
        execution_kind="generation",
        execution_id="gen_three_price_samples",
        request_fingerprint="fp-three-price-samples",
    )
    v3_latest = v3_started + timedelta(microseconds=1)
    samples = iter([v1_started, v2_started, v3_started, v3_latest])

    assert ledger.mark_dispatching(
        call_id,
        started_at_provider=lambda: next(samples),
    )
    ledger.complete_provider_call(
        provider_call_id=call_id,
        measurement=measurement(),
        ownership=ownership(),
        result="succeeded",
    )

    with engine.connect() as connection:
        call = (
            connection.execute(
                select(provider_call_table).where(provider_call_table.c.provider_call_id == call_id)
            )
            .mappings()
            .one()
        )
        usage = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.provider_call_id == call_id)
            )
            .mappings()
            .one()
        )
    assert call["started_at_utc"] == v3_latest.replace(tzinfo=None)
    assert usage["price_version_id"] == v3.id
    assert Decimal(str(usage["estimated_cost_amount"])) == Decimal("0.03")


def test_dispatch_accepts_moving_samples_within_one_locked_price() -> None:
    """生产 moving clock 即使每次增加微秒，同一锁定价格区间内也应稳定 dispatch。"""

    engine, ledger = make_ledger()
    seed(engine, ledger)
    call_id = _prepare_provider_call(
        ledger,
        provider="dashscope",
        model="qwen-plus",
        operation="generate",
        execution_kind="generation",
        execution_id="gen_moving_same_price",
        request_fingerprint="fp-moving-same-price",
    )
    samples = [NOW]

    def moving_started() -> datetime:
        sampled = samples[-1] + timedelta(microseconds=1)
        samples.append(sampled)
        return sampled

    assert ledger.mark_dispatching(call_id, started_at_provider=moving_started)
    with engine.connect() as connection:
        call = (
            connection.execute(
                select(provider_call_table).where(provider_call_table.c.provider_call_id == call_id)
            )
            .mappings()
            .one()
        )
    assert len(samples) == 3
    assert call["started_at_utc"] == samples[-1].replace(tzinfo=None)
