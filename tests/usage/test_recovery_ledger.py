"""Task 13: 恢复与账本原子性验收（SQLite，串行）。

覆盖正式 spec §6 验收边界中 SQLite 可串行证明的部分：
- L56：价格版本切换不重写历史解释（新事件选新版本，旧事件保持旧版本且拒绝 close）；
- L56/L18：首条分录后业务时区不可变（503 calendar_timezone_conflict）；
- L52：unknown provider call 能恢复，且恢复幂等（exactly-once，重放安全）；
- L58：账本分录与投影同事务，异常回滚无残留。

与 plan/brief 的差异（真实生产 API 钉住）：
- ``mark_dispatching`` 必须显式传入 ``started_at_provider``（actual send time 由
  mark_dispatching 持久化，选价/归期一律用它）；
- ``submit_local_usage`` 必须显式传入 ``started_at_utc``；
- 计价：100 tokens x 0.000020 = 0.002（billing_granularity 默认 1）；
- usage_event 行断言按 provider_call_id 分别查询（FixedClock 下 created_at 相同，
  不能依赖插入顺序）；
- SELECT 无 rowcount 语义，rollback 断言用行数。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import and_, create_engine, select
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
from app.usage.schema import (
    quota_debit_table,
    quota_projection_table,
    usage_event_table,
    usage_metadata,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class FixedClock:
    now: datetime

    def now_utc(self, connection: Connection | None = None) -> datetime:
        del connection
        return self.now


@pytest.fixture()
def env():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    usage_metadata.create_all(engine)
    clock = FixedClock(NOW)
    calendar = BusinessCalendarService(engine, SqlAlchemyDatabaseClock(engine), "Asia/Shanghai")
    prices = PriceCatalogService(engine, clock)
    ledger = UsageLedger(engine, clock, calendar, prices)
    try:
        yield engine, ledger
    finally:
        engine.dispose()


def ownership() -> OwnershipSnapshot:
    return OwnershipSnapshot(
        actor_user_id="u1",
        actor_role_snapshot="user",
        actor_department_id_snapshot=None,
        quota_subject_user_id="u1",
        cost_center_key="user:u1",
    )


def measurement(tokens: int = 100) -> ProviderMeasurement:
    return ProviderMeasurement(
        input_tokens=tokens,
        output_tokens=None,
        prompt_cache_hit_tokens=None,
        prompt_cache_miss_tokens=None,
        reasoning_tokens=None,
        image_count=None,
        visual_input_tokens=None,
        embedding_input_tokens=None,
        vector_count=None,
        measurement_sources={"input_tokens": "provider_reported"},
    )


def _provider_usage_rows(engine, provider_call_id: str) -> list[dict]:
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(usage_event_table).where(
                    and_(
                        usage_event_table.c.event_kind == "provider_usage",
                        usage_event_table.c.provider_call_id == provider_call_id,
                    )
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def test_price_version_switch_does_not_rewrite_history(env) -> None:
    """L56：价格版本切换不重写历史解释。"""
    engine, ledger = env
    with engine.begin() as connection:
        ledger.calendar.lock_or_verify(connection)
        v1 = ledger.prices.register(
            connection,
            provider="p",
            model="m",
            operation="o",
            currency_code="USD",
            lines=[{"meter": "input_tokens", "unit": "token", "rate": Decimal("0.000020")}],
            effective_from_utc=NOW,
        )
    call1 = ledger.prepare_provider_call(
        provider="p",
        model="m",
        operation="o",
        execution_kind="generation",
        execution_id="gen_a",
        generation_id="generation-gen_a",
        deadline_utc=NOW + timedelta(days=10),
        request_fingerprint="fp-a",
    )
    ledger.mark_dispatching(call1, started_at_provider=NOW)
    ledger.complete_provider_call(
        provider_call_id=call1,
        measurement=measurement(),
        ownership=ownership(),
        result="succeeded",
    )
    with engine.begin() as connection:
        # 追加 superseding 版本（旧版本被 usage 引用，禁止 standalone close）
        with pytest.raises(PlatformError) as close_err:
            ledger.prices.close_version(connection, v1.id, NOW + timedelta(days=1))
        assert close_err.value.code == "price_close_conflict"
        assert close_err.value.status_code == 409
        v2 = ledger.prices.register(
            connection,
            provider="p",
            model="m",
            operation="o",
            currency_code="USD",
            lines=[{"meter": "input_tokens", "unit": "token", "rate": Decimal("0.000040")}],
            effective_from_utc=NOW + timedelta(days=2),
            supersedes_version_id=v1.id,
        )
    # call2 的 actual send 落在 v2 区间 [NOW+2d, ∞)：select_for 必须选 v2
    call2 = ledger.prepare_provider_call(
        provider="p",
        model="m",
        operation="o",
        execution_kind="generation",
        execution_id="gen_b",
        generation_id="generation-gen_b",
        deadline_utc=NOW + timedelta(days=10),
        request_fingerprint="fp-b",
    )
    ledger.mark_dispatching(call2, started_at_provider=NOW + timedelta(days=3))
    ledger.complete_provider_call(
        provider_call_id=call2,
        measurement=measurement(),
        ownership=ownership(),
        result="succeeded",
    )
    rows1 = _provider_usage_rows(engine, call1)
    rows2 = _provider_usage_rows(engine, call2)
    assert len(rows1) == 1 and len(rows2) == 1
    assert rows1[0]["price_version_id"] == v1.id  # 旧事件保持 v1
    assert rows2[0]["price_version_id"] == v2.id  # 新事件用 v2
    assert Decimal(str(rows1[0]["estimated_cost_amount"])) == Decimal("0.002")
    assert Decimal(str(rows2[0]["estimated_cost_amount"])) == Decimal("0.004")


def test_calendar_immutable_after_first_entry(env) -> None:
    """L56/L18：首条分录后时区配置不一致 → 503 calendar_timezone_conflict。"""
    engine, ledger = env
    with engine.begin() as connection:
        ledger.calendar.lock_or_verify(connection)
    ledger.submit_local_usage(
        execution_kind="ingestion",
        execution_id="attempt-1",
        stage="ocr",
        resource_kind="gpu",
        measurement=LocalMeasurement(
            page_count=1,
            input_bytes=1,
            item_count=None,
            gpu_milliseconds=None,
            cpu_milliseconds=None,
            peak_vram_bytes=None,
        ),
        ownership=ownership(),
        result="succeeded",
        started_at_utc=NOW,
    )
    other_calendar = BusinessCalendarService(engine, SqlAlchemyDatabaseClock(engine), "UTC")
    with engine.begin() as connection:
        with pytest.raises(PlatformError) as exc:
            other_calendar.lock_or_verify(connection)
    assert exc.value.code == "calendar_timezone_conflict"
    assert exc.value.status_code == 503


def test_unknown_recovery_exactly_once_and_replay_safe(env) -> None:
    """L52：unknown provider call 幂等恢复；重放复用同一 usage 事实。"""
    engine, ledger = env
    with engine.begin() as connection:
        ledger.calendar.lock_or_verify(connection)
        ledger.prices.register(
            connection,
            provider="p",
            model="m",
            operation="o",
            currency_code="USD",
            lines=[{"meter": "input_tokens", "unit": "token", "rate": Decimal("0.000020")}],
            effective_from_utc=NOW,
        )
    call_id = ledger.prepare_provider_call(
        provider="p",
        model="m",
        operation="o",
        execution_kind="generation",
        execution_id="gen_c",
        generation_id="generation-gen_c",
        deadline_utc=NOW + timedelta(days=10),
        request_fingerprint="fp-c",
    )
    ledger.mark_dispatching(call_id, started_at_provider=NOW)
    ledger.mark_unknown(call_id)
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
    rows = _provider_usage_rows(engine, call_id)
    assert len(rows) == 1


def test_ledger_atomicity_rollback_leaves_no_partial_rows(env) -> None:
    """L58：账本分录与投影同事务；异常回滚无残留。"""
    from app.usage.quota import QuotaService

    engine, ledger = env
    quota = QuotaService(engine, FixedClock(NOW), ledger.calendar)
    with pytest.raises(RuntimeError):
        with engine.begin() as connection:
            lock = ledger.calendar.lock_or_verify(connection)
            quota.append_debit(
                connection,
                quota_operation_id="job_r",
                publication_id="pub_r",
                quota_subject_user_id="u1",
                pages=100,
                ownership=ownership(),
                calendar_lock=lock,
                role="user",
                effective_at_utc=NOW,
            )
            raise RuntimeError("simulated crash")
    with engine.connect() as connection:
        assert len(connection.execute(select(quota_debit_table)).all()) == 0
        assert len(connection.execute(select(quota_projection_table)).all()) == 0
