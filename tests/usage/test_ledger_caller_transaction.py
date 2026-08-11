"""调用方业务事务与 connection-aware completion 测试（Task 5，H7 + review agent-11 #1）。

语义（正式 spec 优先于旧 task brief/plan）：
- `complete_provider_call_in_transaction(connection, ...)` 在调用方事务内完成：
  锁并校验 dispatching/unknown、完整幂等指纹插入/复用 usage、条件更新 completed。
- fence 有效：completed + usage + business result 原子提交（同一事务，真实条件
  更新/rowcount 判定）。
- 业务失败：三者一起回滚（usage、provider_call 状态、business 全部不落库）。
- fence 失效：调用方事务整体回滚后，只用独立短事务 wrapper 保存 completed+usage，
  业务结果不写。账本写入本身不以当前用户状态或 fence 为前提。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    and_,
    create_engine,
    select,
)
from sqlalchemy.engine import Connection
from sqlalchemy.pool import StaticPool

from app.platform.database import SqlAlchemyDatabaseClock
from app.usage.calendar import BusinessCalendarService
from app.usage.ledger import OwnershipSnapshot, ProviderMeasurement, UsageLedger
from app.usage.price import PriceCatalogService
from app.usage.schema import provider_call_table, usage_event_table, usage_metadata

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)

_meta = MetaData()
tests_business = Table(
    "tests_business",
    _meta,
    Column("id", Integer, primary_key=True),
    Column("status", String(32), nullable=False),
    Column("fence_token", Integer, nullable=False),
)


@dataclass(frozen=True, slots=True)
class FixedClock:
    now: datetime

    def now_utc(self, connection: Connection | None = None) -> datetime:
        del connection
        return self.now


def make_env():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    usage_metadata.create_all(engine)
    _meta.create_all(engine)
    clock = FixedClock(NOW)
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
        fence_token=1,
    )


def measurement() -> ProviderMeasurement:
    return ProviderMeasurement(
        input_tokens=100,
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


def seed_price(engine, ledger) -> None:
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


def test_fence_valid_commits_business_and_usage_together() -> None:
    """review agent-11 #1：有效 fence → completed + usage + business result 原子提交。"""
    engine, ledger = make_env()
    seed_price(engine, ledger)
    call_id = ledger.prepare_provider_call(
        provider="p",
        model="m",
        operation="o",
        execution_kind="ingestion",
        attempt_id="attempt-caller-transaction",
        deadline_utc=NOW + timedelta(hours=1),
        execution_id="job_1",
        provider_call_id="pc-tx-1",
        request_fingerprint="fp-tx",
    )
    ledger.mark_dispatching(call_id, started_at_provider=NOW)
    with engine.begin() as connection:
        connection.execute(tests_business.insert().values(id=1, status="draft", fence_token=1))
    # 调用方业务事务：connection-aware completion + 真实条件 fence 更新（rowcount）
    with engine.begin() as connection:
        event_id = ledger.complete_provider_call_in_transaction(
            connection,
            provider_call_id=call_id,
            measurement=measurement(),
            ownership=ownership(),
            result="succeeded",
        )
        updated = connection.execute(
            tests_business.update()
            .where(and_(tests_business.c.id == 1, tests_business.c.fence_token == 1))
            .values(status="published")
        ).rowcount
        assert updated == 1
    with engine.connect() as connection:
        business = connection.execute(select(tests_business)).mappings().all()
        usage = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.usage_event_id == event_id)
            )
            .mappings()
            .all()
        )
        call = (
            connection.execute(
                select(provider_call_table).where(provider_call_table.c.provider_call_id == call_id)
            )
            .mappings()
            .one()
        )
    assert len(business) == 1
    assert business[0]["status"] == "published"
    assert len(usage) == 1
    assert call["status"] == "completed"


def test_business_failure_rolls_back_usage_and_status_together() -> None:
    """review agent-11 #1：业务失败 → completed + usage + business 三者一起回滚。"""
    engine, ledger = make_env()
    seed_price(engine, ledger)
    call_id = ledger.prepare_provider_call(
        provider="p",
        model="m",
        operation="o",
        execution_kind="ingestion",
        attempt_id="attempt-caller-transaction",
        deadline_utc=NOW + timedelta(hours=1),
        execution_id="job_2",
        provider_call_id="pc-tx-2",
        request_fingerprint="fp-tx2",
    )
    ledger.mark_dispatching(call_id, started_at_provider=NOW)
    with pytest.raises(RuntimeError):
        with engine.begin() as connection:
            ledger.complete_provider_call_in_transaction(
                connection,
                provider_call_id=call_id,
                measurement=measurement(),
                ownership=ownership(),
                result="succeeded",
            )
            connection.execute(
                tests_business.insert().values(id=2, status="published", fence_token=1)
            )
            raise RuntimeError("business failure after usage write")
    with engine.connect() as connection:
        usage = connection.execute(select(usage_event_table)).mappings().all()
        business = connection.execute(select(tests_business)).mappings().all()
        call = (
            connection.execute(
                select(provider_call_table).where(provider_call_table.c.provider_call_id == call_id)
            )
            .mappings()
            .one()
        )
    assert usage == []  # usage 一并回滚
    assert business == []  # business 回滚
    assert call["status"] == "dispatching"  # 状态回滚


def test_fence_invalid_keeps_usage_via_independent_wrapper_only() -> None:
    """review agent-11 #1：失效 fence → 业务事务回滚；只用独立短事务 wrapper 保存 completed+usage。"""
    engine, ledger = make_env()
    seed_price(engine, ledger)
    call_id = ledger.prepare_provider_call(
        provider="p",
        model="m",
        operation="o",
        execution_kind="ingestion",
        attempt_id="attempt-caller-transaction",
        deadline_utc=NOW + timedelta(hours=1),
        execution_id="job_3",
        provider_call_id="pc-tx-3",
        request_fingerprint="fp-tx3",
    )
    ledger.mark_dispatching(call_id, started_at_provider=NOW)
    with engine.begin() as connection:
        connection.execute(tests_business.insert().values(id=3, status="draft", fence_token=1))
    # 调用方持有的 fence_token=2 已失效：事务内条件更新 rowcount=0 → 回滚全部
    with pytest.raises(RuntimeError):
        with engine.begin() as connection:
            ledger.complete_provider_call_in_transaction(
                connection,
                provider_call_id=call_id,
                measurement=measurement(),
                ownership=ownership(),
                result="succeeded",
            )
            updated = connection.execute(
                tests_business.update()
                .where(and_(tests_business.c.id == 3, tests_business.c.fence_token == 2))
                .values(status="published")
            ).rowcount
            if updated != 1:
                raise RuntimeError("fence violation: business rollback")
    with engine.connect() as connection:
        usage_after_rollback = connection.execute(select(usage_event_table)).mappings().all()
        call_after_rollback = (
            connection.execute(
                select(provider_call_table).where(provider_call_table.c.provider_call_id == call_id)
            )
            .mappings()
            .one()
        )
    assert usage_after_rollback == []
    assert call_after_rollback["status"] == "dispatching"
    # fence 失效后：仅独立短事务 wrapper 保存 completed + usage；业务结果不写
    event_id = ledger.complete_provider_call(
        provider_call_id=call_id,
        measurement=measurement(),
        ownership=ownership(),
        result="succeeded",
    )
    with engine.connect() as connection:
        usage = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.usage_event_id == event_id)
            )
            .mappings()
            .all()
        )
        business = connection.execute(select(tests_business)).mappings().all()
        call = (
            connection.execute(
                select(provider_call_table).where(provider_call_table.c.provider_call_id == call_id)
            )
            .mappings()
            .one()
        )
    assert len(usage) == 1  # usage 保留
    assert call["status"] == "completed"
    assert business[0]["status"] == "draft"  # 业务结果不写
