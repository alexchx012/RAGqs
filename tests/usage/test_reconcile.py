"""Unknown provider call 对账测试（Task 5，H3 + review agent-11）。

语义（正式 spec 优先于旧 task brief/plan）：
- 对账结果结构化：ConfirmedUsage / ConfirmedNotSent / StillUnknown /
  ReconciliationOnlyAmount；provider 查询在事务外（connection=None）；stale
  dispatching 先幂等转 unknown；unknown 允许 → completed/not_sent；amount-only 写
  usage_reconciliation 稳定事实且保持 unknown，不伪造 usage。
- TOCTOU：应用事务内复检状态，已终态化 → 跳过（不得终态 usage 与 amount-only 并存）。
- starvation 轮转：StillUnknown/amount-only 更新 last_reconcile_attempt_at_utc，
  limit 下后续候选不被饿死。
- amount-only：amount 有限且适配 Numeric(30,10)；currency 正式 ISO 校验；effective
  按 actual started/dispatch time，recorded 用 now；同内容重放幂等、异内容 409。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Connection
from sqlalchemy.pool import StaticPool

from app.platform.database import SqlAlchemyDatabaseClock
from app.platform.errors import PlatformError
from app.usage.calendar import BusinessCalendarService
from app.usage.ledger import OwnershipSnapshot, ProviderMeasurement, UsageLedger
from app.usage.price import PriceCatalogService
from app.usage.reconcile import (
    ConfirmedNotSent,
    ConfirmedUsage,
    ReconciliationOnlyAmount,
    StillUnknown,
    reconcile_unknown_calls,
)
from app.usage.schema import (
    provider_call_table,
    usage_event_table,
    usage_metadata,
    usage_reconciliation_table,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
JULY = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class FixedClock:
    now: datetime

    def now_utc(self, connection: Connection | None = None) -> datetime:
        del connection
        return self.now


@dataclass(slots=True)
class MutableClock:
    now: datetime

    def now_utc(self, connection: Connection | None = None) -> datetime:
        del connection
        return self.now


def make_ledger(clock=None):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    usage_metadata.create_all(engine)
    clock = clock or FixedClock(NOW)
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
        input_tokens=10,
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


def seed_price(
    engine,
    ledger,
    *,
    provider="p",
    model="m",
    operation="o",
    effective_from_utc: datetime = NOW,
) -> None:
    with engine.begin() as connection:
        ledger.calendar.lock_or_verify(connection)
        ledger.prices.register(
            connection,
            provider=provider,
            model=model,
            operation=operation,
            currency_code="USD",
            lines=[{"meter": "input_tokens", "unit": "token", "rate": Decimal("0.000020")}],
            effective_from_utc=effective_from_utc,
        )


def make_unknown_call(
    engine,
    ledger,
    *,
    provider="p",
    model="m",
    operation="o",
    execution_id="gen_r",
    provider_call_id=None,
    started_at_utc: datetime = NOW,
    seed: bool = True,
) -> str:
    if seed:
        seed_price(
            engine,
            ledger,
            provider=provider,
            model=model,
            operation=operation,
            effective_from_utc=started_at_utc,
        )
    call_id = ledger.prepare_provider_call(
        provider=provider,
        model=model,
        operation=operation,
        execution_kind="generation",
        execution_id=execution_id,
        request_fingerprint=f"fp-{execution_id}",
        provider_call_id=provider_call_id,
        attempt_id=f"attempt-{execution_id}",
        # Deadline is an absolute current/future guard, independent of the historical
        # actual started fact used by effective-period reconciliation tests.
        deadline_utc=NOW + timedelta(hours=1),
    )
    ledger.mark_dispatching(call_id, started_at_provider=started_at_utc)
    ledger.mark_unknown(call_id)
    return call_id


class StubReconciliationPort:
    def __init__(self, outcomes: dict[str, object]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []
        self.confirm_connections: list = []

    def confirm(self, *, provider_call_id: str, fingerprint: str, connection) -> object:
        del fingerprint
        self.calls.append(provider_call_id)
        self.confirm_connections.append(connection)
        return self.outcomes[provider_call_id]


class SecretInvalidDecision:
    def __repr__(self) -> str:
        return "provider-secret-must-not-leak"


class TOCTOUCompletingPort:
    """confirm 期间由另一进程完成该调用（模拟网络查询窗口内的并发终态化）。"""

    def __init__(self, ledger: UsageLedger, outcome: object) -> None:
        self.ledger = ledger
        self.outcome = outcome

    def confirm(self, *, provider_call_id: str, fingerprint: str, connection) -> object:
        del fingerprint, connection
        self.ledger.complete_provider_call(
            provider_call_id=provider_call_id,
            measurement=measurement(),
            ownership=ownership(),
            result="succeeded",
        )
        return self.outcome


def test_reconcile_completed_confirms_usage_exactly_once() -> None:
    engine, ledger = make_ledger()
    call_id = make_unknown_call(engine, ledger)
    port = StubReconciliationPort(
        {
            call_id: ConfirmedUsage(
                measurement=measurement(),
                ownership=ownership(),
                result="succeeded",
                started_at_utc=NOW,
            )
        }
    )
    count = reconcile_unknown_calls(engine, ledger, port, older_than_utc=NOW, limit=100)
    assert count == 1
    assert port.confirm_connections == [None]  # provider 查询在事务外
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
    assert calls[0]["status"] == "completed"
    # 再跑：unknown 已清空 → 0 条推进
    assert reconcile_unknown_calls(engine, ledger, port, older_than_utc=NOW, limit=100) == 0


def test_reconcile_not_sent_confirms_and_writes_no_usage() -> None:
    engine, ledger = make_ledger()
    call_id = make_unknown_call(engine, ledger)
    port = StubReconciliationPort({call_id: ConfirmedNotSent()})
    count = reconcile_unknown_calls(engine, ledger, port, older_than_utc=NOW, limit=100)
    assert count == 1
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


@pytest.mark.parametrize(
    "decision",
    [None, {"unexpected": "decision"}, SecretInvalidDecision()],
    ids=["none", "dict", "object"],
)
def test_reconcile_invalid_adapter_decision_fails_closed_without_mutation(decision) -> None:
    engine, ledger = make_ledger()
    call_id = make_unknown_call(engine, ledger, execution_id="gen-invalid-decision")
    port = StubReconciliationPort({call_id: decision})

    with pytest.raises(PlatformError) as failure:
        reconcile_unknown_calls(engine, ledger, port, older_than_utc=NOW, limit=100)

    assert failure.value.code == "provider_reconciliation_contract_error"
    assert failure.value.status_code == 502
    assert failure.value.retryable is True
    assert failure.value.details == {}
    assert "provider-secret-must-not-leak" not in str(failure.value)
    with engine.connect() as connection:
        call = (
            connection.execute(
                select(provider_call_table).where(provider_call_table.c.provider_call_id == call_id)
            )
            .mappings()
            .one()
        )
        usage_rows = connection.execute(select(usage_event_table)).all()
        reconciliation_rows = connection.execute(select(usage_reconciliation_table)).all()
    assert call["status"] == "unknown"
    assert call["last_reconcile_attempt_at_utc"] is None
    assert usage_rows == []
    assert reconciliation_rows == []


def test_reconcile_still_unknown_and_amount_only() -> None:
    engine, ledger = make_ledger()
    call_id = make_unknown_call(engine, ledger)
    port = StubReconciliationPort({call_id: StillUnknown()})
    count = reconcile_unknown_calls(engine, ledger, port, older_than_utc=NOW, limit=100)
    assert count == 0  # 未推进
    with engine.connect() as connection:
        calls = (
            connection.execute(
                select(provider_call_table).where(provider_call_table.c.provider_call_id == call_id)
            )
            .mappings()
            .all()
        )
    assert calls[0]["status"] == "unknown"
    # amount_only：仅金额进对账分组，不伪造 usage
    amount_port = StubReconciliationPort(
        {
            call_id: ReconciliationOnlyAmount(
                amount=Decimal("0.001"),
                currency_code="USD",
                ownership=ownership(),
            )
        }
    )
    count2 = reconcile_unknown_calls(engine, ledger, amount_port, older_than_utc=NOW, limit=100)
    assert count2 == 1
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(usage_reconciliation_table).where(
                    usage_reconciliation_table.c.provider_call_id == call_id
                )
            )
            .mappings()
            .all()
        )
        usage_rows = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.provider_call_id == call_id)
            )
            .mappings()
            .all()
        )
    assert len(rows) == 1
    assert rows[0]["reconciliation_kind"] == "amount_only"
    assert usage_rows == []  # 不伪造 usage


def test_amount_only_then_confirmed_not_sent_deletes_reconciliation() -> None:
    """三轮复审 R6：amount-only 后确认未发送时，同事务升级删除矛盾金额事实。"""
    engine, ledger = make_ledger()
    call_id = make_unknown_call(engine, ledger, execution_id="gen_amt_not_sent")
    amount_port = StubReconciliationPort(
        {
            call_id: ReconciliationOnlyAmount(
                amount=Decimal("0.001"),
                currency_code="USD",
                ownership=ownership(),
            )
        }
    )
    assert reconcile_unknown_calls(engine, ledger, amount_port, older_than_utc=NOW) == 1

    not_sent_port = StubReconciliationPort({call_id: ConfirmedNotSent()})
    assert reconcile_unknown_calls(engine, ledger, not_sent_port, older_than_utc=NOW) == 1

    with engine.connect() as connection:
        call = ledger._require_call(connection, call_id)  # noqa: SLF001 - 断言持久终态
        amount_rows = (
            connection.execute(
                select(usage_reconciliation_table).where(
                    usage_reconciliation_table.c.provider_call_id == call_id
                )
            )
            .mappings()
            .all()
        )
        usage_rows = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.provider_call_id == call_id)
            )
            .mappings()
            .all()
        )
    assert call["status"] == "not_sent"
    assert call["started_at_utc"] is None
    assert amount_rows == []
    assert usage_rows == []


def test_overlapping_amount_only_then_not_sent_deletes_amount(tmp_path) -> None:
    """amount claim 先持锁提交时，not_sent 等待后更新状态并删除刚提交的 amount。"""
    engine, ledger, call_id, call = _make_overlap_env(
        tmp_path / "overlap_amount_then_not_sent.sqlite3",
        execution_id="gen_ov3",
        provider_call_id="pc-ov3",
    )
    amount_write_acquired = threading.Event()
    release_amount = threading.Event()
    not_sent_update_reached = threading.Event()
    errors: list[BaseException] = []
    outcomes: dict[str, object] = {}

    def hold_after_amount_claim(conn, cursor, statement, parameters, context, executemany) -> None:
        del conn, cursor, parameters, context, executemany
        if _normalized_sql(statement).startswith(
            "UPDATE PROVIDER_CALL SET LAST_RECONCILE_ATTEMPT_AT_UTC"
        ):
            amount_write_acquired.set()
            if not release_amount.wait(15):
                raise TimeoutError("amount release timed out")

    def observe_not_sent_update(conn, cursor, statement, parameters, context, executemany) -> None:
        del conn, cursor, parameters, context, executemany
        normalized = _normalized_sql(statement)
        if (
            normalized.startswith("UPDATE PROVIDER_CALL SET STATUS")
            and "NOT_SENT_AT_UTC" in normalized
        ):
            not_sent_update_reached.set()

    event.listen(engine, "after_cursor_execute", hold_after_amount_claim)
    event.listen(engine, "before_cursor_execute", observe_not_sent_update)

    def record_amount_only() -> None:
        try:
            with engine.begin() as connection:
                outcomes["outcome"] = ledger.record_reconciliation_amount_only(
                    connection,
                    call=call,
                    amount=Decimal("0.001"),
                    currency_code="USD",
                    ownership=ownership(),
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def mark_not_sent() -> None:
        try:
            with engine.begin() as connection:
                ledger.mark_not_sent_in_transaction(connection, call_id)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    amount_thread = threading.Thread(target=record_amount_only)
    not_sent_thread = threading.Thread(target=mark_not_sent)
    not_sent_started = False
    amount_thread.start()
    try:
        assert amount_write_acquired.wait(15)
        not_sent_thread.start()
        not_sent_started = True
        assert not_sent_update_reached.wait(15)
        assert not_sent_thread.is_alive()
    finally:
        release_amount.set()
        amount_thread.join(15)
        if not_sent_started:
            not_sent_thread.join(15)
    assert not amount_thread.is_alive()
    assert not not_sent_thread.is_alive()
    assert errors == []
    assert outcomes["outcome"] is not None
    with engine.connect() as connection:
        call_row = ledger._require_call(connection, call_id)  # noqa: SLF001
        amount_rows = (
            connection.execute(
                select(usage_reconciliation_table).where(
                    usage_reconciliation_table.c.provider_call_id == call_id
                )
            )
            .mappings()
            .all()
        )
        usage_rows = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.provider_call_id == call_id)
            )
            .mappings()
            .all()
        )
    assert call_row["status"] == "not_sent"
    assert call_row["started_at_utc"] is None
    assert amount_rows == []
    assert usage_rows == []
    engine.dispose()


def test_reconcile_turns_stale_dispatching_to_unknown_then_completed() -> None:
    """H3：stale dispatching 扫描 → mark_unknown → confirm completed。"""
    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    call_id = ledger.prepare_provider_call(
        provider="p",
        model="m",
        operation="o",
        execution_kind="generation",
        execution_id="gen_s",
        request_fingerprint="fp-s",
        attempt_id="attempt-gen_s",
        deadline_utc=NOW + timedelta(hours=1),
    )
    ledger.mark_dispatching(call_id, started_at_provider=NOW)
    # 模拟恢复：stale dispatching → unknown
    ledger.mark_unknown(call_id)
    port = StubReconciliationPort(
        {
            call_id: ConfirmedUsage(
                measurement=measurement(),
                ownership=ownership(),
                result="succeeded",
                started_at_utc=NOW,
            )
        }
    )
    count = reconcile_unknown_calls(engine, ledger, port, older_than_utc=NOW, limit=100)
    assert count == 1
    with engine.connect() as connection:
        calls = (
            connection.execute(
                select(provider_call_table).where(provider_call_table.c.provider_call_id == call_id)
            )
            .mappings()
            .all()
        )
    assert calls[0]["status"] == "completed"


def test_reconcile_auto_converts_stale_dispatching_before_confirm() -> None:
    """纠偏：stale dispatching 由 reconcile 幂等转 unknown 后再确认（调用方无需手动转）。"""
    clock = MutableClock(NOW)
    engine, ledger = make_ledger(clock)
    seed_price(engine, ledger)
    call_id = ledger.prepare_provider_call(
        provider="p",
        model="m",
        operation="o",
        execution_kind="generation",
        execution_id="gen_s2",
        request_fingerprint="fp-s2",
        attempt_id="attempt-gen_s2",
        deadline_utc=NOW + timedelta(seconds=1),
    )
    ledger.mark_dispatching(call_id, started_at_provider=NOW)  # 保持 dispatching（stale）
    clock.now = NOW + timedelta(seconds=2)
    port = StubReconciliationPort(
        {
            call_id: ConfirmedUsage(
                measurement=measurement(),
                ownership=ownership(),
                result="succeeded",
                started_at_utc=NOW,
            )
        }
    )
    count = reconcile_unknown_calls(engine, ledger, port, older_than_utc=NOW, limit=100)
    assert count == 1
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
    assert calls[0]["status"] == "completed"


def test_reconcile_stale_dispatching_waits_for_persisted_deadline() -> None:
    started = NOW - timedelta(minutes=10)
    clock = MutableClock(started)
    engine, ledger = make_ledger(clock)
    seed_price(engine, ledger, effective_from_utc=started)
    calls: dict[str, str] = {}
    for name, deadline in (
        ("expired", NOW - timedelta(seconds=1)),
        ("future", NOW + timedelta(minutes=5)),
    ):
        call_id = ledger.prepare_provider_call(
            provider="p",
            model="m",
            operation="o",
            execution_kind="generation",
            execution_id=f"gen-{name}",
            provider_call_id=f"pc-{name}",
            attempt_id=f"attempt-{name}",
            deadline_utc=deadline,
            request_fingerprint=f"fp-{name}",
        )
        ledger.mark_dispatching(call_id, started_at_provider=started)
        calls[name] = call_id

    clock.now = NOW
    port = StubReconciliationPort({calls["expired"]: ConfirmedNotSent()})
    assert (
        reconcile_unknown_calls(
            engine,
            ledger,
            port,
            older_than_utc=NOW,
            limit=100,
        )
        == 1
    )
    assert port.calls == [calls["expired"]]
    with engine.connect() as connection:
        statuses = dict(
            connection.execute(
                select(provider_call_table.c.provider_call_id, provider_call_table.c.status)
            ).all()
        )
    assert statuses == {calls["expired"]: "not_sent", calls["future"]: "dispatching"}


def test_reconcile_amount_only_is_idempotent_on_replay() -> None:
    """纠偏：同 amount 重放不重复写对账事实；异 amount 是账本不变量错误。"""
    engine, ledger = make_ledger()
    call_id = make_unknown_call(engine, ledger)
    amount_port = StubReconciliationPort(
        {
            call_id: ReconciliationOnlyAmount(
                amount=Decimal("0.001"),
                currency_code="USD",
                ownership=ownership(),
            )
        }
    )
    first = reconcile_unknown_calls(engine, ledger, amount_port, older_than_utc=NOW, limit=100)
    replay = reconcile_unknown_calls(engine, ledger, amount_port, older_than_utc=NOW, limit=100)
    assert first == 1
    assert replay == 0  # 已入组的稳定事实不重复写
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(usage_reconciliation_table).where(
                    usage_reconciliation_table.c.provider_call_id == call_id
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 1
    # 异 amount → ledger_invariant_conflict
    conflict_port = StubReconciliationPort(
        {
            call_id: ReconciliationOnlyAmount(
                amount=Decimal("0.999"),
                currency_code="USD",
                ownership=ownership(),
            )
        }
    )
    with pytest.raises(PlatformError) as exc:
        reconcile_unknown_calls(engine, ledger, conflict_port, older_than_utc=NOW, limit=100)
    assert exc.value.code == "ledger_invariant_conflict"


def test_reconcile_toctou_skips_completed_call() -> None:
    """review agent-11 #4/#13：confirm 期间调用被并发完成 → 应用事务复检后跳过。"""
    engine, ledger = make_ledger()
    call_id = make_unknown_call(engine, ledger)
    port = TOCTOUCompletingPort(
        ledger,
        ConfirmedUsage(
            measurement=measurement(),
            ownership=ownership(),
            result="succeeded",
            started_at_utc=NOW,
        ),
    )
    count = reconcile_unknown_calls(engine, ledger, port, older_than_utc=NOW, limit=100)
    assert count == 0  # 状态已 completed → 跳过，不重复写 usage
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.provider_call_id == call_id)
            )
            .mappings()
            .all()
        )
    assert len(rows) == 1  # 仅 confirm 窗口内写入的一条


def test_reconcile_toctou_skips_amount_only_when_already_completed() -> None:
    """review agent-11 #4：不得同时存在终态 usage 与 amount-only。"""
    engine, ledger = make_ledger()
    call_id = make_unknown_call(engine, ledger)
    port = TOCTOUCompletingPort(
        ledger,
        ReconciliationOnlyAmount(
            amount=Decimal("0.001"),
            currency_code="USD",
            ownership=ownership(),
        ),
    )
    count = reconcile_unknown_calls(engine, ledger, port, older_than_utc=NOW, limit=100)
    assert count == 0
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(usage_reconciliation_table).where(
                    usage_reconciliation_table.c.provider_call_id == call_id
                )
            )
            .mappings()
            .all()
        )
        usage_rows = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.provider_call_id == call_id)
            )
            .mappings()
            .all()
        )
    assert rows == []  # 终态 usage 已存在 → 不写 amount-only
    assert len(usage_rows) == 1


def test_reconcile_rotates_candidates_and_avoids_starvation() -> None:
    """review agent-11 #8/#13：StillUnknown 更新尝试时间，limit 下轮转不饿死后续候选。"""
    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    call_a = make_unknown_call(
        engine, ledger, execution_id="gen_rot_a", provider_call_id="pc-rot-a", seed=False
    )
    call_b = make_unknown_call(
        engine, ledger, execution_id="gen_rot_b", provider_call_id="pc-rot-b", seed=False
    )
    port = StubReconciliationPort(
        {
            call_a: StillUnknown(),
            call_b: ConfirmedNotSent(),
        }
    )
    # limit=1：第一轮只处理 pc-rot-a（NULL attempt 优先）→ StillUnknown 不推进
    count1 = reconcile_unknown_calls(engine, ledger, port, older_than_utc=NOW, limit=1)
    assert count1 == 0
    with engine.connect() as connection:
        attempt_a = connection.execute(
            select(provider_call_table.c.last_reconcile_attempt_at_utc).where(
                provider_call_table.c.provider_call_id == call_a
            )
        ).scalar_one()
    assert attempt_a == NOW.replace(tzinfo=None)  # StillUnknown 已更新尝试时间
    # limit=1：第二轮按轮转轮到 pc-rot-b → ConfirmedNotSent 推进
    count2 = reconcile_unknown_calls(engine, ledger, port, older_than_utc=NOW, limit=1)
    assert count2 == 1
    with engine.connect() as connection:
        status_a = connection.execute(
            select(provider_call_table.c.status).where(
                provider_call_table.c.provider_call_id == call_a
            )
        ).scalar_one()
        status_b = connection.execute(
            select(provider_call_table.c.status).where(
                provider_call_table.c.provider_call_id == call_b
            )
        ).scalar_one()
    assert status_a == "unknown"  # 仍 unknown，未被饿死后的错误推进
    assert status_b == "not_sent"


def test_reconcile_amount_only_uses_actual_started_for_effective_period() -> None:
    """review agent-11 #4/#13：amount-only effective 用 actual started，recorded 用 now。"""
    engine, ledger = make_ledger()  # clock NOW = 2026-08-05（Asia/Shanghai → 2026-08）
    call_id = make_unknown_call(
        engine,
        ledger,
        execution_id="gen_amt_period",
        provider_call_id="pc-amt-period",
        started_at_utc=JULY,
    )
    port = StubReconciliationPort(
        {
            call_id: ReconciliationOnlyAmount(
                amount=Decimal("0.001"),
                currency_code="USD",
                ownership=ownership(),
            )
        }
    )
    count = reconcile_unknown_calls(engine, ledger, port, older_than_utc=NOW, limit=100)
    assert count == 1
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(usage_reconciliation_table).where(
                    usage_reconciliation_table.c.provider_call_id == call_id
                )
            )
            .mappings()
            .one()
        )
    assert row["effective_period"] == "2026-07"
    assert row["recorded_period"] == "2026-08"
    assert row["effective_at_utc"] == JULY.replace(tzinfo=None)


def test_reconcile_amount_only_validates_amount_and_currency() -> None:
    """review agent-11 #4：amount 有限且适配 Numeric(30,10)；currency 正式 ISO 校验。"""
    engine, ledger = make_ledger()
    call_id = make_unknown_call(engine, ledger, execution_id="gen_amt_bad")
    bad_amount_port = StubReconciliationPort(
        {
            call_id: ReconciliationOnlyAmount(
                amount=Decimal("1e100"),
                currency_code="USD",
                ownership=ownership(),
            )
        }
    )
    with pytest.raises(PlatformError) as exc:
        reconcile_unknown_calls(engine, ledger, bad_amount_port, older_than_utc=NOW, limit=100)
    assert exc.value.code == "validation_error"
    bad_currency_port = StubReconciliationPort(
        {
            call_id: ReconciliationOnlyAmount(
                amount=Decimal("0.001"),
                currency_code="NOTACODE",
                ownership=ownership(),
            )
        }
    )
    with pytest.raises(PlatformError) as exc:
        reconcile_unknown_calls(engine, ledger, bad_currency_port, older_than_utc=NOW, limit=100)
    assert exc.value.code == "validation_error"


def test_reconcile_amount_only_rejects_negative_amount() -> None:
    """Minor：amount-only 拒绝负值（成本原始金额非负，负差异由 adjustment 表达）。"""
    engine, ledger = make_ledger()
    call_id = make_unknown_call(engine, ledger, execution_id="gen_amt_neg")
    negative_port = StubReconciliationPort(
        {
            call_id: ReconciliationOnlyAmount(
                amount=Decimal("-0.001"),
                currency_code="USD",
                ownership=ownership(),
            )
        }
    )
    with pytest.raises(PlatformError) as exc:
        reconcile_unknown_calls(engine, ledger, negative_port, older_than_utc=NOW, limit=100)
    assert exc.value.code == "validation_error"
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(usage_reconciliation_table).where(
                    usage_reconciliation_table.c.provider_call_id == call_id
                )
            )
            .mappings()
            .all()
        )
    assert rows == []  # 校验失败 → 无残留事实


def _file_engine(path, *, timeout: float = 60.0):
    engine = create_engine(
        f"sqlite+pysqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": timeout},
    )
    usage_metadata.create_all(engine)
    return engine


def _make_overlap_env(db_path, *, execution_id: str, provider_call_id: str):
    """构建文件 DB 对账环境并返回 (engine, ledger, call_id, call)。"""
    engine = _file_engine(db_path)
    clock = FixedClock(NOW)
    calendar = BusinessCalendarService(engine, SqlAlchemyDatabaseClock(engine), "Asia/Shanghai")
    prices = PriceCatalogService(engine, clock)
    ledger = UsageLedger(engine, clock, calendar, prices)
    seed_price(engine, ledger)
    call_id = make_unknown_call(
        engine, ledger, execution_id=execution_id, provider_call_id=provider_call_id, seed=False
    )
    with engine.connect() as snapshot_connection:
        call = ledger._require_call(snapshot_connection, call_id)  # noqa: SLF001
    return engine, ledger, call_id, call


def _normalized_sql(statement: str) -> str:
    return " ".join(statement.upper().split())


def test_overlapping_completion_wins_over_amount_only_claim(tmp_path) -> None:
    """R4：completion 的首个实际写 SQL 已执行并持有 SQLite 写锁后，claim 的条件
    UPDATE 才到达 DB 执行入口；completion 提交后 claim 重读条件并返回 None。"""
    engine, ledger, call_id, call = _make_overlap_env(
        tmp_path / "overlap_complete_wins.sqlite3",
        execution_id="gen_ov1",
        provider_call_id="pc-ov1",
    )
    completion_write_acquired = threading.Event()
    release_completion = threading.Event()
    claim_update_reached = threading.Event()
    errors: list[BaseException] = []
    outcomes: dict[str, object] = {}

    def hold_after_completion_write(
        conn, cursor, statement, parameters, context, executemany
    ) -> None:
        del conn, cursor, parameters, context, executemany
        if _normalized_sql(statement).startswith("DELETE FROM USAGE_RECONCILIATION"):
            # DELETE 已由 SQLite 执行：即使 0 行，也已进入写事务并持锁至 commit。
            completion_write_acquired.set()
            if not release_completion.wait(15):
                raise TimeoutError("completion release timed out")

    def observe_claim_update(conn, cursor, statement, parameters, context, executemany) -> None:
        del conn, cursor, parameters, context, executemany
        if _normalized_sql(statement).startswith(
            "UPDATE PROVIDER_CALL SET LAST_RECONCILE_ATTEMPT_AT_UTC"
        ):
            claim_update_reached.set()

    event.listen(engine, "after_cursor_execute", hold_after_completion_write)
    event.listen(engine, "before_cursor_execute", observe_claim_update)

    def complete_call() -> None:
        try:
            with engine.begin() as connection:
                ledger.complete_provider_call_in_transaction(
                    connection,
                    provider_call_id=call_id,
                    measurement=measurement(),
                    ownership=ownership(),
                    result="succeeded",
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def claim_amount_only() -> None:
        try:
            with engine.begin() as connection:
                outcomes["outcome"] = ledger.record_reconciliation_amount_only(
                    connection,
                    call=call,
                    amount=Decimal("0.001"),
                    currency_code="USD",
                    ownership=ownership(),
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    completion_thread = threading.Thread(target=complete_call)
    claim_thread = threading.Thread(target=claim_amount_only)
    claim_started = False
    completion_thread.start()
    try:
        assert completion_write_acquired.wait(15)
        claim_thread.start()
        claim_started = True
        assert claim_update_reached.wait(15)
        assert claim_thread.is_alive()  # 条件 UPDATE 尚未返回，正等待先行写事务
    finally:
        release_completion.set()
        completion_thread.join(15)
        if claim_started:
            claim_thread.join(15)
    assert not completion_thread.is_alive()
    assert not claim_thread.is_alive()
    assert errors == []
    assert outcomes == {"outcome": None}
    with engine.connect() as connection:
        usage_rows = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.provider_call_id == call_id)
            )
            .mappings()
            .all()
        )
        amount_rows = (
            connection.execute(
                select(usage_reconciliation_table).where(
                    usage_reconciliation_table.c.provider_call_id == call_id
                )
            )
            .mappings()
            .all()
        )
        status = connection.execute(
            select(provider_call_table.c.status).where(
                provider_call_table.c.provider_call_id == call_id
            )
        ).scalar_one()
    assert len(usage_rows) == 1
    assert amount_rows == []
    assert status == "completed"
    engine.dispose()


def test_overlapping_amount_only_then_completion_upgrades(tmp_path) -> None:
    """R4：amount-only 的条件 UPDATE 已由 SQLite 执行并持锁后，completion 的首个
    写 SQL 才到达 DB 执行入口；amount 先提交，completion 随后原子升级删除。"""
    engine, ledger, call_id, call = _make_overlap_env(
        tmp_path / "overlap_amount_then_complete.sqlite3",
        execution_id="gen_ov2",
        provider_call_id="pc-ov2",
    )
    amount_write_acquired = threading.Event()
    release_amount = threading.Event()
    completion_delete_reached = threading.Event()
    errors: list[BaseException] = []
    outcomes: dict[str, object] = {}

    def hold_after_amount_claim(conn, cursor, statement, parameters, context, executemany) -> None:
        del conn, cursor, parameters, context, executemany
        if _normalized_sql(statement).startswith(
            "UPDATE PROVIDER_CALL SET LAST_RECONCILE_ATTEMPT_AT_UTC"
        ):
            # 条件 UPDATE 已执行且 rowcount=1；SQLite 写锁保持到 amount 事务提交。
            amount_write_acquired.set()
            if not release_amount.wait(15):
                raise TimeoutError("amount release timed out")

    def observe_completion_delete(
        conn, cursor, statement, parameters, context, executemany
    ) -> None:
        del conn, cursor, parameters, context, executemany
        if _normalized_sql(statement).startswith("DELETE FROM USAGE_RECONCILIATION"):
            completion_delete_reached.set()

    event.listen(engine, "after_cursor_execute", hold_after_amount_claim)
    event.listen(engine, "before_cursor_execute", observe_completion_delete)

    def record_amount_only() -> None:
        try:
            with engine.begin() as connection:
                outcomes["outcome"] = ledger.record_reconciliation_amount_only(
                    connection,
                    call=call,
                    amount=Decimal("0.001"),
                    currency_code="USD",
                    ownership=ownership(),
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def complete_call() -> None:
        try:
            with engine.begin() as connection:
                ledger.complete_provider_call_in_transaction(
                    connection,
                    provider_call_id=call_id,
                    measurement=measurement(),
                    ownership=ownership(),
                    result="succeeded",
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    amount_thread = threading.Thread(target=record_amount_only)
    completion_thread = threading.Thread(target=complete_call)
    completion_started = False
    amount_thread.start()
    try:
        assert amount_write_acquired.wait(15)
        completion_thread.start()
        completion_started = True
        assert completion_delete_reached.wait(15)
        assert completion_thread.is_alive()  # completion DELETE 尚未返回，等待先行写事务
    finally:
        release_amount.set()
        amount_thread.join(15)
        if completion_started:
            completion_thread.join(15)
    assert not amount_thread.is_alive()
    assert not completion_thread.is_alive()
    assert errors == []
    assert outcomes["outcome"] is not None
    with engine.connect() as connection:
        usage_rows = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.provider_call_id == call_id)
            )
            .mappings()
            .all()
        )
        amount_rows = (
            connection.execute(
                select(usage_reconciliation_table).where(
                    usage_reconciliation_table.c.provider_call_id == call_id
                )
            )
            .mappings()
            .all()
        )
        status = connection.execute(
            select(provider_call_table.c.status).where(
                provider_call_table.c.provider_call_id == call_id
            )
        ).scalar_one()
    assert len(usage_rows) == 1
    assert amount_rows == []
    assert status == "completed"
    engine.dispose()
