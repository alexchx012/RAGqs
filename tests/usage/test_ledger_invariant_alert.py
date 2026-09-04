"""UsageLedger 唯一键异指纹冲突触发高优先级不变量告警（A45）。

- 既有 409 ledger_invariant_conflict 与数据回滚语义保持不变；
- 冲突后（事务已回滚）best-effort 发布 outbox 告警事件（ops 可感知）；
- 无冲突（同指纹幂等重放）与 state/id-reuse 类冲突不产生告警；
- 告警通道故障绝不掩盖原 409。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Connection

from app.outbox.publisher import SqlAlchemyUsageInvariantAlertAdapter
from app.outbox.schema import outbox_event_table
from app.platform.database import SqlAlchemyDatabaseClock
from app.platform.errors import PlatformError
from app.usage.calendar import BusinessCalendarService
from app.usage.ledger import OwnershipSnapshot, ProviderMeasurement, UsageLedger
from app.usage.price import PriceCatalogService
from app.usage.schema import provider_call_table, usage_event_table
from tests._support import build_engine, make_publisher

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class FixedClock:
    now: datetime

    def now_utc(self, connection: Connection | None = None) -> datetime:
        del connection
        return self.now


class _ExplodingAlertPort:
    def publish_usage_ledger_invariant_conflict(self, **kwargs: object) -> str:
        raise RuntimeError("alert channel down")


def make_ledger(*, alert_port: object | None = "adapter"):
    """build_engine 已建 usage/outbox/identity 全部表；alert_port=None 关闭告警通道。"""
    engine = build_engine()
    clock = FixedClock(NOW)
    calendar = BusinessCalendarService(engine, SqlAlchemyDatabaseClock(engine), "Asia/Shanghai")
    prices = PriceCatalogService(engine, clock)
    if alert_port == "adapter":
        alert_port = SqlAlchemyUsageInvariantAlertAdapter(engine, make_publisher(engine))
    ledger = UsageLedger(engine, clock, calendar, prices, invariant_alert_port=alert_port)
    return engine, ledger


def seed(engine, ledger) -> None:
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


def measurement(*, input_tokens: int = 1000) -> ProviderMeasurement:
    return ProviderMeasurement(
        input_tokens=input_tokens,
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


def prepare_and_dispatch(ledger: UsageLedger, execution_id: str) -> str:
    call_id = ledger.prepare_provider_call(
        provider="dashscope",
        model="qwen-plus",
        operation="generate",
        execution_kind="generation",
        execution_id=execution_id,
        generation_id=f"generation:{execution_id}",
        request_fingerprint=f"fp-{execution_id}",
        deadline_utc=NOW + timedelta(hours=1),
    )
    ledger.mark_dispatching(call_id, started_at_provider=NOW)
    return call_id


def invariant_alert_events(engine) -> list:
    with engine.connect() as connection:
        return (
            connection.execute(
                select(outbox_event_table).where(
                    outbox_event_table.c.event_type == "usage_ledger_invariant_conflict"
                )
            )
            .mappings()
            .all()
        )


def test_fingerprint_conflict_keeps_409_and_publishes_alert() -> None:
    engine, ledger = make_ledger()
    seed(engine, ledger)
    call_id = prepare_and_dispatch(ledger, "gen_conflict")
    ledger.complete_provider_call(
        provider_call_id=call_id,
        measurement=measurement(),
        ownership=ownership(),
        result="succeeded",
    )

    with pytest.raises(PlatformError) as exc:
        ledger.complete_provider_call(
            provider_call_id=call_id,
            measurement=measurement(input_tokens=999),
            ownership=ownership(),
            result="succeeded",
        )

    assert exc.value.code == "ledger_invariant_conflict"
    assert exc.value.status_code == 409
    events = invariant_alert_events(engine)
    assert len(events) == 1
    assert events[0]["aggregate_id"] == call_id
    assert events[0]["payload_json"] == {
        "unique_key_fields": ["provider_call_id"],
        "provider_call_id": call_id,
    }
    # 回滚语义保持不变：冲突事务不留下任何部分写入。
    with engine.connect() as connection:
        status = connection.execute(
            select(provider_call_table.c.status).where(
                provider_call_table.c.provider_call_id == call_id
            )
        ).scalar_one()
        usage_count = connection.execute(
            select(func.count()).select_from(usage_event_table)
        ).scalar_one()
    assert status == "completed"
    assert usage_count == 1


def test_idempotent_replay_publishes_no_alert() -> None:
    engine, ledger = make_ledger()
    seed(engine, ledger)
    call_id = prepare_and_dispatch(ledger, "gen_replay")
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
    assert invariant_alert_events(engine) == []


def test_alert_failure_does_not_mask_the_409() -> None:
    engine, ledger = make_ledger(alert_port=_ExplodingAlertPort())
    seed(engine, ledger)
    call_id = prepare_and_dispatch(ledger, "gen_explode")
    ledger.complete_provider_call(
        provider_call_id=call_id,
        measurement=measurement(),
        ownership=ownership(),
        result="succeeded",
    )

    with pytest.raises(PlatformError) as exc:
        ledger.complete_provider_call(
            provider_call_id=call_id,
            measurement=measurement(input_tokens=999),
            ownership=ownership(),
            result="succeeded",
        )
    assert exc.value.code == "ledger_invariant_conflict"
    assert invariant_alert_events(engine) == []


def test_ledger_without_alert_port_keeps_conflict_behavior() -> None:
    engine, ledger = make_ledger(alert_port=None)
    seed(engine, ledger)
    call_id = prepare_and_dispatch(ledger, "gen_noport")
    ledger.complete_provider_call(
        provider_call_id=call_id,
        measurement=measurement(),
        ownership=ownership(),
        result="succeeded",
    )

    with pytest.raises(PlatformError) as exc:
        ledger.complete_provider_call(
            provider_call_id=call_id,
            measurement=measurement(input_tokens=999),
            ownership=ownership(),
            result="succeeded",
        )
    assert exc.value.code == "ledger_invariant_conflict"


def test_state_conflict_does_not_publish_alert() -> None:
    engine, ledger = make_ledger()
    seed(engine, ledger)
    call_id = ledger.prepare_provider_call(
        provider="dashscope",
        model="qwen-plus",
        operation="generate",
        execution_kind="generation",
        execution_id="gen_prepared",
        generation_id="generation:gen_prepared",
        request_fingerprint="fp-gen_prepared",
        deadline_utc=NOW + timedelta(hours=1),
    )

    with pytest.raises(PlatformError) as exc:
        ledger.complete_provider_call(
            provider_call_id=call_id,
            measurement=measurement(),
            ownership=ownership(),
            result="succeeded",
        )
    assert exc.value.code == "provider_call_state_conflict"
    assert invariant_alert_events(engine) == []
