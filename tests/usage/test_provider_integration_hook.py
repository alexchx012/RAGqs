"""生产 provider 集成 wrapper 测试（Task 5，review agent-11 #2/#13）。

使用 `app/usage/provider_integration.py::run_provider_call_with_usage` +
`UsageLedgerLifecycle` 包装 `call_with_policy`：真实 operation + usage extractor +
lifecycle port；每 attempt 使用 context 注入的新 provider_call_id 执行完整生命周期。
验证：
- 每次物理发送使用新的 64 字符 `pc_` canonical hash ID，每个 attempt 独立 prepare/dispatch 短事务；
- 三分支：sent=False → not_sent 无 usage；sent=True 有确定响应 → completed + failed
  usage；sent=True 无法确认 → unknown；成功 → completed + usage；
- started_at_utc 在 dispatch 入口持久化，completion 使用之；
- 业务事务回滚后 usage 保留、按原 ID 幂等补记（禁止重发）。
"""

from __future__ import annotations

import asyncio
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
from app.platform.ports import ProviderPort
from app.platform.provider import (
    CircuitBreakerRegistry,
    CircuitOpen,
    InMemoryProviderTelemetry,
    ProviderCallContext,
    ProviderFailure,
    ProviderResult,
    RetryPolicy,
    _physical_provider_call_id,
    call_with_policy,
)
from app.usage.calendar import BusinessCalendarService
from app.usage.ledger import OwnershipSnapshot, ProviderMeasurement, UsageLedger
from app.usage.price import PriceCatalogService
from app.usage.provider_integration import (
    UsageLedgerLifecycle,
    run_provider_call_with_usage,
)
from app.usage.schema import provider_call_table, usage_event_table, usage_metadata

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


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
        fence_token=1,
    )


def measurement() -> ProviderMeasurement:
    return ProviderMeasurement(
        input_tokens=100,
        output_tokens=50,
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


def seed_price(engine, ledger) -> None:
    with engine.begin() as connection:
        ledger.calendar.lock_or_verify(connection)
        ledger.prices.register(
            connection,
            provider="test-provider",
            model="m",
            operation="generate",
            currency_code="USD",
            lines=[
                {"meter": "input_tokens", "unit": "token", "rate": Decimal("0.000020")},
                {"meter": "output_tokens", "unit": "token", "rate": Decimal("0.000060")},
            ],
            effective_from_utc=NOW,
        )


def make_runner(ledger, operation, *, current_time, advance, telemetry=None):
    """构造 run_provider_call_with_usage 参数（extractor/ownership 固定桩）。"""

    def extractor(value, context, failure):
        del value, context, failure
        return measurement()

    def ownership_provider(context):
        del context
        return ownership()

    lifecycle = UsageLedgerLifecycle(ledger)

    def run(context):
        return run_provider_call_with_usage(
            operation=operation,
            context=context,
            model="m",
            lifecycle=lifecycle,
            measurement_extractor=extractor,
            ownership_provider=ownership_provider,
            execution_kind="generation",
            execution_id="gen_1",
            request_fingerprint="fp-prod",
            circuits=CircuitBreakerRegistry(),  # 每测试独立 circuit（共享默认会被前序失败打开）
            now=lambda: current_time[0],
            sleep=advance,
            jitter=lambda _d: 0,
            telemetry=telemetry,
        )

    return run


def _run_with_lifecycle(operation, lifecycle, clock: MutableClock, deadline: datetime):
    return run_provider_call_with_usage(
        operation=operation,
        context=ProviderCallContext(
            provider="test-provider",
            operation="generate",
            provider_call_id="pc-deadline-root",
            attempt_id="attempt-deadline",
            deadline_utc=deadline,
            resource_id="resource-deadline",
        ),
        model="m",
        lifecycle=lifecycle,
        measurement_extractor=lambda value, context, failure: measurement(),
        ownership_provider=lambda context: ownership(),
        execution_kind="generation",
        execution_id="gen-deadline",
        request_fingerprint="fp-deadline",
        policy=RetryPolicy(synchronous_attempts=1),
        circuits=CircuitBreakerRegistry(),
        now=lambda: clock.now,
        sleep=lambda _delay: None,
        jitter=lambda _delay: 0,
    )


def test_public_transport_failure_contract_preserves_circuit_and_usage_semantics() -> None:
    engine, ledger = make_ledger()
    seed_price(engine, ledger)

    class FailingTransport:
        def call(self, call_context: ProviderCallContext, request: object) -> object:
            del call_context, request
            raise ProviderFailure(
                "upstream_503",
                status_code=503,
                retryable=True,
                sent=True,
            )

    transport: ProviderPort = FailingTransport()
    circuits = CircuitBreakerRegistry(threshold=1, open_seconds=60)
    telemetry = InMemoryProviderTelemetry()
    result = run_provider_call_with_usage(
        operation=transport.call,
        context=ProviderCallContext(
            provider="test-provider",
            operation="generate",
            provider_call_id="pc-contract-failure-root",
            attempt_id="attempt-contract-failure",
            deadline_utc=NOW + timedelta(minutes=1),
        ),
        model="m",
        lifecycle=UsageLedgerLifecycle(ledger),
        measurement_extractor=lambda _value, _context, _failure: measurement(),
        ownership_provider=lambda _context: ownership(),
        execution_kind="generation",
        execution_id="gen-contract-failure",
        request_fingerprint="fp-contract-failure",
        policy=RetryPolicy(synchronous_attempts=1),
        circuits=circuits,
        now=lambda: NOW,
        telemetry=telemetry,
    )

    assert (result.state, result.error_class, result.attempts) == (
        "failed",
        "upstream_503",
        1,
    )
    assert [(event.state, event.error_class) for event in telemetry.events] == [
        ("failed", "upstream_503")
    ]
    with engine.connect() as connection:
        call = connection.execute(select(provider_call_table)).mappings().one()
        usage = connection.execute(select(usage_event_table)).mappings().one()
    assert call["status"] == "completed"
    assert usage["provider_call_id"] == call["provider_call_id"]
    assert usage["result"] == "failed"

    with pytest.raises(CircuitOpen):
        call_with_policy(
            lambda _call_context, _request: "must-not-run",
            ProviderCallContext(
                provider="test-provider",
                operation="generate",
                provider_call_id="pc-contract-open-root",
                attempt_id="attempt-contract-open",
                deadline_utc=NOW + timedelta(minutes=1),
            ),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: NOW,
        )


def test_transport_returning_policy_result_keeps_usage_unknown_and_circuit_neutral() -> None:
    engine, ledger = make_ledger()
    seed_price(engine, ledger)

    class InvalidEnvelopeTransport:
        def call(self, call_context: ProviderCallContext, request: object) -> ProviderResult:
            del call_context, request
            return ProviderResult(state="failed", error_class="upstream_503")

    transport: ProviderPort = InvalidEnvelopeTransport()
    circuits = CircuitBreakerRegistry(threshold=1, open_seconds=60)
    telemetry = InMemoryProviderTelemetry()
    with pytest.raises(
        TypeError,
        match="provider transport must return a raw value, not ProviderResult",
    ):
        run_provider_call_with_usage(
            operation=transport.call,
            context=ProviderCallContext(
                provider="test-provider",
                operation="generate",
                provider_call_id="pc-contract-envelope-root",
                attempt_id="attempt-contract-envelope",
                deadline_utc=NOW + timedelta(minutes=1),
            ),
            model="m",
            lifecycle=UsageLedgerLifecycle(ledger),
            measurement_extractor=lambda _value, _context, _failure: measurement(),
            ownership_provider=lambda _context: ownership(),
            execution_kind="generation",
            execution_id="gen-contract-envelope",
            request_fingerprint="fp-contract-envelope",
            policy=RetryPolicy(synchronous_attempts=1),
            circuits=circuits,
            now=lambda: NOW,
            telemetry=telemetry,
        )

    assert telemetry.events == []
    with engine.connect() as connection:
        call = connection.execute(select(provider_call_table)).mappings().one()
        usage = connection.execute(select(usage_event_table)).mappings().all()
    assert call["status"] == "unknown"
    assert usage == []

    recovered = call_with_policy(
        lambda _call_context, _request: "ok",
        ProviderCallContext(
            provider="test-provider",
            operation="generate",
            provider_call_id="pc-contract-neutral-root",
            attempt_id="attempt-contract-neutral",
            deadline_utc=NOW + timedelta(minutes=1),
        ),
        circuits=circuits,
        policy=RetryPolicy(synchronous_attempts=1),
        now=lambda: NOW,
    )
    assert recovered.state == "succeeded"


def test_prepare_crossing_deadline_never_dispatches_or_sends() -> None:
    clock = MutableClock(NOW)
    engine, ledger = make_ledger(clock)
    seed_price(engine, ledger)
    deadline = NOW + timedelta(seconds=1)
    sent: list[str] = []

    class AdvanceAfterPrepare(UsageLedgerLifecycle):
        def prepare(self, **kwargs):
            created = super().prepare(**kwargs)
            clock.now = deadline
            return created

    result = _run_with_lifecycle(
        lambda context, request: sent.append(context.provider_call_id) or "sent",
        AdvanceAfterPrepare(ledger),
        clock,
        deadline,
    )

    assert sent == []
    assert (result.state, result.error_class, result.attempts) == (
        "not_sent",
        "deadline_exceeded",
        0,
    )
    assert result.elapsed_ms == 1000
    with engine.connect() as connection:
        row = connection.execute(select(provider_call_table)).mappings().one()
    assert row["status"] == "not_sent"
    assert row["started_at_utc"] is None


def test_dispatch_commit_crossing_deadline_never_sends() -> None:
    clock = MutableClock(NOW)
    engine, ledger = make_ledger(clock)
    seed_price(engine, ledger)
    deadline = NOW + timedelta(seconds=1)
    sent: list[str] = []

    class AdvanceAfterDispatch(UsageLedgerLifecycle):
        def mark_dispatching(self, provider_call_id, *, started_at_provider):
            outcome = super().mark_dispatching(
                provider_call_id, started_at_provider=started_at_provider
            )
            clock.now = deadline
            return outcome

    result = _run_with_lifecycle(
        lambda context, request: sent.append(context.provider_call_id) or "sent",
        AdvanceAfterDispatch(ledger),
        clock,
        deadline,
    )

    assert sent == []
    assert (result.state, result.error_class, result.attempts) == (
        "not_sent",
        "deadline_exceeded",
        0,
    )
    assert result.elapsed_ms == 1000
    with engine.connect() as connection:
        row = connection.execute(select(provider_call_table)).mappings().one()
    assert row["status"] == "not_sent"
    assert row["started_at_utc"] is None


def test_production_wrapper_three_branches_and_fresh_ids() -> None:
    """review agent-11 #2：三分支终态 + 每次重试新 ID + started_at 持久化。"""
    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    outcomes = iter(
        [
            ProviderFailure("timeout", retryable=True, sent=False),
            ProviderFailure("upstream_503", status_code=503, retryable=True, sent=True),
            "ok",
        ]
    )
    current = [NOW]
    call_ids: list[str] = []

    def advance(delay: float) -> None:
        current[0] = current[0] + timedelta(seconds=delay)

    def operation(context: ProviderCallContext, request):
        del request
        call_ids.append(context.provider_call_id)
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    run = make_runner(ledger, operation, current_time=current, advance=advance)
    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id="pc-caller-1",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(seconds=60),
        resource_id="resource-1",
    )
    result = run(context)
    assert result.state == "succeeded"
    assert len(call_ids) == 3
    assert len(set(call_ids)) == 3  # 每次物理发送使用独立 canonical hash ID
    with engine.connect() as connection:
        usage_rows = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.event_kind == "provider_usage")
            )
            .mappings()
            .all()
        )
        call_rows = (
            connection.execute(
                select(provider_call_table).order_by(provider_call_table.c.provider_call_id)
            )
            .mappings()
            .all()
        )
    assert len(usage_rows) == 2  # sent=True 失败 + 成功各一条；sent=False 无 usage
    assert {row["status"] for row in call_rows} == {"completed", "not_sent"}
    rows_by_call_id = {row["provider_call_id"]: row for row in call_rows}
    assert [rows_by_call_id[call_id]["deadline_utc"] for call_id in call_ids] == [
        (NOW + timedelta(seconds=30)).replace(tzinfo=None),
        (NOW + timedelta(seconds=30.25)).replace(tzinfo=None),
        (NOW + timedelta(seconds=31.25)).replace(tzinfo=None),
    ]
    assert {row["provider_call_id"] for row in usage_rows} == {
        call_ids[1],
        call_ids[2],
    }
    for row in call_rows:
        if row["status"] == "not_sent":
            # Important 3：not_sent 清除 started（从未实际发送）
            assert row["started_at_utc"] is None
        else:
            # 每个 attempt 在 dispatch 入口用当时时钟持久化 actual started（SQLite 回读 naive）
            assert (
                NOW.replace(tzinfo=None)
                <= row["started_at_utc"]
                <= (NOW + timedelta(seconds=2)).replace(tzinfo=None)
            )


def test_late_attempt_success_keeps_usage_and_records_circuit_failure() -> None:
    """A late transport result is rejected without erasing its committed usage fact."""
    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    current = [NOW]
    circuits = CircuitBreakerRegistry(threshold=1, open_seconds=60)
    attempt_deadlines: list[datetime] = []
    operation_calls = 0

    def operation(context: ProviderCallContext, request: object) -> str:
        nonlocal operation_calls
        del request
        operation_calls += 1
        attempt_deadlines.append(context.deadline_utc)
        current[0] += timedelta(seconds=31)
        return "late"

    result = run_provider_call_with_usage(
        operation=operation,
        context=ProviderCallContext(
            provider="test-provider",
            operation="generate",
            provider_call_id="pc-late-success",
            attempt_id="attempt-root",
            deadline_utc=NOW + timedelta(seconds=120),
            resource_id="resource-1",
        ),
        model="m",
        lifecycle=UsageLedgerLifecycle(ledger),
        measurement_extractor=lambda _value, _context, _failure: measurement(),
        ownership_provider=lambda _context: ownership(),
        execution_kind="generation",
        execution_id="gen-late-success",
        request_fingerprint="fp-late-success",
        policy=RetryPolicy(synchronous_attempts=1),
        circuits=circuits,
        now=lambda: current[0],
        sleep=lambda _delay: None,
        jitter=lambda _delay: 0,
    )

    assert (result.state, result.error_class, result.attempts) == (
        "unknown",
        "deadline_exceeded",
        1,
    )
    assert operation_calls == 1
    assert attempt_deadlines == [NOW + timedelta(seconds=30)]
    with engine.connect() as connection:
        call_row = connection.execute(select(provider_call_table)).mappings().one()
        usage_rows = connection.execute(select(usage_event_table)).mappings().all()
    assert call_row["status"] == "completed"
    assert call_row["deadline_utc"] == (NOW + timedelta(seconds=30)).replace(tzinfo=None)
    assert len(usage_rows) == 1
    with pytest.raises(CircuitOpen):
        run_provider_call_with_usage(
            operation=operation,
            context=ProviderCallContext(
                provider="test-provider",
                operation="generate",
                provider_call_id="pc-late-success",
                attempt_id="attempt-root",
                deadline_utc=NOW + timedelta(seconds=120),
                resource_id="resource-1",
            ),
            model="m",
            lifecycle=UsageLedgerLifecycle(ledger),
            measurement_extractor=lambda _value, _context, _failure: measurement(),
            ownership_provider=lambda _context: ownership(),
            execution_kind="generation",
            execution_id="gen-late-success",
            request_fingerprint="fp-late-success",
            policy=RetryPolicy(synchronous_attempts=1),
            circuits=circuits,
            now=lambda: current[0],
        )
    assert operation_calls == 1


def test_production_wrapper_unknown_when_unconfirmable() -> None:
    """review agent-11 #2：sent=True 但结果无法确认（无 status_code）→ unknown，无 usage。"""
    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    outcomes = iter(
        [
            ProviderFailure("timeout", retryable=True, sent=True, status_code=None),
            ProviderFailure("timeout", retryable=True, sent=True, status_code=None),
            ProviderFailure("timeout", retryable=True, sent=True, status_code=None),
        ]
    )
    current = [NOW]
    call_ids: list[str] = []

    def advance(delay: float) -> None:
        current[0] = current[0] + timedelta(seconds=delay)

    def operation(context: ProviderCallContext, request):
        del request
        call_ids.append(context.provider_call_id)
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    run = make_runner(ledger, operation, current_time=current, advance=advance)
    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id="pc-caller-2",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(seconds=60),
        resource_id="resource-1",
    )
    result = run(context)
    assert result.state == "unknown"  # 三次 attempt 全部 sent=True 无法确认
    assert len(call_ids) == 3
    with engine.connect() as connection:
        usage_rows = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.event_kind == "provider_usage")
            )
            .mappings()
            .all()
        )
        call_rows = (
            connection.execute(
                select(provider_call_table).order_by(provider_call_table.c.provider_call_id)
            )
            .mappings()
            .all()
        )
    assert usage_rows == []  # 无法确认 → 不写 usage
    assert {row["status"] for row in call_rows} == {"unknown"}


def test_business_transaction_rollback_keeps_usage_and_reuses_call_id() -> None:
    """C1/C3：业务事务回滚后已提交 usage 保留；重试按原 provider_call_id 幂等补记（禁止重发）。"""
    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    call_id = "pc-stable-1"
    ledger.prepare_provider_call(
        provider="test-provider",
        model="m",
        operation="generate",
        execution_kind="ingestion",
        execution_id="job_1",
        attempt_id="job_1:1",
        provider_call_id=call_id,
        deadline_utc=NOW + timedelta(minutes=5),
        request_fingerprint="fp-stable",
    )
    ledger.mark_dispatching(call_id, started_at_provider=NOW)
    with pytest.raises(RuntimeError):
        with engine.begin() as connection:
            connection.execute(
                provider_call_table.update()
                .where(provider_call_table.c.provider_call_id == call_id)
                .values(resource_id="business-marker")
            )
            raise RuntimeError("business rollback")
    first = ledger.complete_provider_call(
        provider_call_id=call_id,
        measurement=measurement(),
        ownership=ownership(),
        result="succeeded",
    )
    replay = ledger.complete_provider_call(
        provider_call_id=call_id,
        measurement=measurement(),
        ownership=ownership(),
        result="succeeded",
    )
    assert replay == first
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.provider_call_id == call_id)
            )
            .mappings()
            .all()
        )
    assert len(rows) == 1


def test_prepare_hook_failure_propagates() -> None:
    """pre-send 账本 hook 失败原样浮出，且不污染 provider circuit/telemetry。"""
    engine, ledger = make_ledger()

    class ExplodingLifecycle(UsageLedgerLifecycle):
        def prepare(self, **kwargs):
            raise RuntimeError("prepare storage down")

    def operation(context: ProviderCallContext, request):
        del context, request
        return "ok"

    current = [NOW]
    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id="pc-caller-prep",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(seconds=60),
        resource_id="resource-1",
    )
    circuits = CircuitBreakerRegistry(threshold=1)
    telemetry = InMemoryProviderTelemetry()
    with pytest.raises(RuntimeError, match="prepare storage down"):
        run_provider_call_with_usage(
            operation=operation,
            context=context,
            model="m",
            lifecycle=ExplodingLifecycle(ledger),
            measurement_extractor=lambda _v, _c, _f: measurement(),
            ownership_provider=lambda _c: ownership(),
            execution_kind="generation",
            execution_id="gen_prep",
            request_fingerprint="fp-prep",
            now=lambda: current[0],
            circuits=circuits,
            sleep=lambda _d: None,
            jitter=lambda _d: 0,
            telemetry=telemetry,
        )
    # prepare 失败 → 未创建 provider_call，policy 不产生 provider 结果 telemetry。
    with engine.connect() as connection:
        rows = connection.execute(select(provider_call_table)).mappings().all()
    assert rows == []
    assert telemetry.events == []
    # threshold=1 下若前一错误被计为 provider failure，此调用会 CircuitOpen；成功证明未污染。
    result = call_with_policy(
        operation,
        context,
        circuits=circuits,
        telemetry=telemetry,
        now=lambda: current[0],
    )
    assert result.state == "succeeded"


@pytest.mark.parametrize("failure_kind", ["exception", "cancellation"])
def test_prepare_commit_then_failure_terminalizes_not_sent(failure_kind: str) -> None:
    """prepare 独立事务已提交后再失败时，prepared ownership 必须安全回退。"""

    engine, ledger = make_ledger()
    injected: BaseException
    if failure_kind == "cancellation":
        injected = asyncio.CancelledError("prepare cancelled after commit")
    else:
        injected = RuntimeError("prepare failed after commit")
    operation_calls: list[str] = []
    circuits = CircuitBreakerRegistry(threshold=1)
    telemetry = InMemoryProviderTelemetry()

    class CommitThenFailPrepareLifecycle(UsageLedgerLifecycle):
        def prepare(self, **kwargs):
            super().prepare(**kwargs)
            raise injected

    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id=f"pc-caller-prepare-after-commit-{failure_kind}",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(minutes=5),
        resource_id="resource-1",
    )
    with pytest.raises(type(injected)) as caught:
        run_provider_call_with_usage(
            operation=lambda ctx, _request: operation_calls.append(ctx.provider_call_id),
            context=context,
            model="m",
            lifecycle=CommitThenFailPrepareLifecycle(ledger),
            measurement_extractor=lambda _value, _ctx, _failure: measurement(),
            ownership_provider=lambda _ctx: ownership(),
            execution_kind="generation",
            execution_id=f"gen_prepare_after_commit_{failure_kind}",
            request_fingerprint=f"fp-prepare-after-commit-{failure_kind}",
            policy=RetryPolicy(synchronous_attempts=1),
            circuits=circuits,
            telemetry=telemetry,
            now=lambda: NOW,
        )
    assert caught.value is injected
    assert operation_calls == []
    assert telemetry.events == []
    with engine.connect() as connection:
        row = connection.execute(select(provider_call_table)).mappings().one()
        usage_rows = connection.execute(select(usage_event_table)).mappings().all()
    assert row["status"] == "not_sent"
    assert row["started_at_utc"] is None
    assert usage_rows == []
    assert (
        call_with_policy(
            lambda _ctx, _request: "ok",
            context,
            policy=RetryPolicy(synchronous_attempts=1),
            circuits=circuits,
            now=lambda: NOW,
        ).state
        == "succeeded"
    )


def test_prepare_commit_terminalization_error_takes_precedence() -> None:
    """prepare after-commit 取消后的 prepared-only 终态写错误拥有传播优先级。"""

    engine, ledger = make_ledger()
    cancelled = asyncio.CancelledError("prepare cancelled after commit")
    terminal_error = OSError("prepare terminalization failed")
    circuits = CircuitBreakerRegistry(threshold=1)
    telemetry = InMemoryProviderTelemetry()

    class CommitThenCancelAndFailTerminalLifecycle(UsageLedgerLifecycle):
        def prepare(self, **kwargs):
            super().prepare(**kwargs)
            raise cancelled

        def mark_not_sent_if_prepared(self, provider_call_id: str) -> None:
            del provider_call_id
            raise terminal_error

    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id="pc-caller-prepare-terminal-priority",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(minutes=5),
        resource_id="resource-1",
    )
    with pytest.raises(OSError) as caught:
        run_provider_call_with_usage(
            operation=lambda _ctx, _request: "unexpected",
            context=context,
            model="m",
            lifecycle=CommitThenCancelAndFailTerminalLifecycle(ledger),
            measurement_extractor=lambda _value, _ctx, _failure: measurement(),
            ownership_provider=lambda _ctx: ownership(),
            execution_kind="generation",
            execution_id="gen_prepare_terminal_priority",
            request_fingerprint="fp-prepare-terminal-priority",
            policy=RetryPolicy(synchronous_attempts=1),
            circuits=circuits,
            telemetry=telemetry,
            now=lambda: NOW,
        )
    assert caught.value is terminal_error
    assert telemetry.events == []
    with engine.connect() as connection:
        row = connection.execute(select(provider_call_table)).mappings().one()
    assert row["status"] == "prepared"
    assert (
        call_with_policy(
            lambda _ctx, _request: "ok",
            context,
            policy=RetryPolicy(synchronous_attempts=1),
            circuits=circuits,
            now=lambda: NOW,
        ).state
        == "succeeded"
    )


@pytest.mark.parametrize(
    "dispatch_committed",
    [False, True],
    ids=["before-dispatch-commit", "after-dispatch-commit"],
)
def test_dispatch_hook_failure_marks_not_sent_without_policy_side_effects(
    dispatch_committed: bool,
) -> None:
    """prepare 后 dispatch hook 失败确定未发送，并中性终止 provider policy。"""
    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    operation_calls: list[str] = []

    class DispatchExplodes(UsageLedgerLifecycle):
        def mark_dispatching(self, provider_call_id, *, started_at_provider):
            if dispatch_committed:
                super().mark_dispatching(
                    provider_call_id,
                    started_at_provider=started_at_provider,
                )
            raise RuntimeError("dispatch storage down")

    def operation(context: ProviderCallContext, request):
        del request
        operation_calls.append(context.provider_call_id)
        return "ok"

    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id="pc-caller-dispatch",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(seconds=60),
        resource_id="resource-1",
    )
    circuits = CircuitBreakerRegistry(threshold=1)
    telemetry = InMemoryProviderTelemetry()
    with pytest.raises(RuntimeError, match="dispatch storage down"):
        run_provider_call_with_usage(
            operation=operation,
            context=context,
            model="m",
            lifecycle=DispatchExplodes(ledger),
            measurement_extractor=lambda _v, _c, _f: measurement(),
            ownership_provider=lambda _c: ownership(),
            execution_kind="generation",
            execution_id="gen_dispatch",
            request_fingerprint="fp-dispatch",
            now=lambda: NOW,
            circuits=circuits,
            sleep=lambda _d: None,
            jitter=lambda _d: 0,
            telemetry=telemetry,
        )

    assert operation_calls == []
    with engine.connect() as connection:
        call_rows = connection.execute(select(provider_call_table)).mappings().all()
        usage_rows = connection.execute(select(usage_event_table)).mappings().all()
    assert len(call_rows) == 1
    assert call_rows[0]["provider_call_id"] == _physical_provider_call_id(
        context.provider_call_id, 1
    )
    assert call_rows[0]["status"] == "not_sent"
    assert call_rows[0]["started_at_utc"] is None
    assert usage_rows == []
    assert telemetry.events == []
    # threshold=1 下若 dispatch hook 被误计为 provider failure，此调用会 CircuitOpen。
    result = call_with_policy(
        lambda _ctx, _request: "ok",
        context,
        circuits=circuits,
        telemetry=telemetry,
        now=lambda: NOW,
    )
    assert result.state == "succeeded"


def test_dispatch_hook_terminalization_error_takes_precedence() -> None:
    """dispatch 失败后的 not_sent 写入失败必须优先于原 dispatch 错误传播。"""
    engine, ledger = make_ledger()
    operation_calls: list[str] = []
    not_sent_calls: list[str] = []

    class DispatchAndTerminalizationExplode(UsageLedgerLifecycle):
        def mark_dispatching(self, provider_call_id, *, started_at_provider):
            del provider_call_id, started_at_provider
            raise RuntimeError("dispatch storage down")

        def mark_not_sent(self, provider_call_id):
            not_sent_calls.append(provider_call_id)
            raise OSError("not sent ledger write failed")

    def operation(context: ProviderCallContext, request):
        del request
        operation_calls.append(context.provider_call_id)
        return "ok"

    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id="pc-caller-dispatch-terminal",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(seconds=60),
        resource_id="resource-1",
    )
    telemetry = InMemoryProviderTelemetry()
    with pytest.raises(OSError, match="not sent ledger write failed"):
        run_provider_call_with_usage(
            operation=operation,
            context=context,
            model="m",
            lifecycle=DispatchAndTerminalizationExplode(ledger),
            measurement_extractor=lambda _v, _c, _f: measurement(),
            ownership_provider=lambda _c: ownership(),
            execution_kind="generation",
            execution_id="gen_dispatch_terminal",
            request_fingerprint="fp-dispatch-terminal",
            now=lambda: NOW,
            circuits=CircuitBreakerRegistry(threshold=1),
            sleep=lambda _d: None,
            jitter=lambda _d: 0,
            telemetry=telemetry,
        )

    expected_call_id = _physical_provider_call_id(context.provider_call_id, 1)
    assert operation_calls == []
    assert not_sent_calls == [expected_call_id]
    with engine.connect() as connection:
        call_rows = connection.execute(select(provider_call_table)).mappings().all()
        usage_rows = connection.execute(select(usage_event_table)).mappings().all()
    assert [(row["provider_call_id"], row["status"]) for row in call_rows] == [
        (expected_call_id, "prepared")
    ]
    assert usage_rows == []
    assert telemetry.events == []


@pytest.mark.parametrize("existing_status", ["dispatching", "unknown"])
def test_replayed_dispatch_conflict_does_not_rewrite_existing_send(
    existing_status: str,
) -> None:
    """复用既有 attempt 时，dispatch 冲突不得将真实发送改写为 not_sent。"""
    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    lifecycle = UsageLedgerLifecycle(ledger)
    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id="pc-caller-replay-dispatch",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(seconds=60),
        resource_id="resource-1",
    )
    call_id = _physical_provider_call_id(context.provider_call_id, 1)
    ledger.prepare_provider_call(
        provider="test-provider",
        model="m",
        operation="generate",
        execution_kind="generation",
        execution_id="gen_replay",
        attempt_id="attempt-root",
        provider_call_id=call_id,
        resource_id="resource-1",
        deadline_utc=NOW + timedelta(seconds=30),
        request_fingerprint="fp-replay",
    )
    ledger.mark_dispatching(call_id, started_at_provider=NOW)
    if existing_status == "unknown":
        ledger.mark_unknown(call_id)

    operation_calls: list[str] = []
    telemetry = InMemoryProviderTelemetry()

    def operation(attempt_context: ProviderCallContext, request):
        del request
        operation_calls.append(attempt_context.provider_call_id)
        return "must not send"

    with pytest.raises(PlatformError) as error:
        run_provider_call_with_usage(
            operation=operation,
            context=context,
            model="m",
            lifecycle=lifecycle,
            measurement_extractor=lambda _v, _c, _f: measurement(),
            ownership_provider=lambda _c: ownership(),
            execution_kind="generation",
            execution_id="gen_replay",
            request_fingerprint="fp-replay",
            now=lambda: NOW,
            circuits=CircuitBreakerRegistry(threshold=1),
            sleep=lambda _d: None,
            jitter=lambda _d: 0,
            telemetry=telemetry,
        )

    assert error.value.code == "provider_call_state_conflict"
    assert operation_calls == []
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(provider_call_table).where(provider_call_table.c.provider_call_id == call_id)
            )
            .mappings()
            .one()
        )
        usage_rows = connection.execute(select(usage_event_table)).mappings().all()
    assert row["status"] == existing_status
    assert row["started_at_utc"] is not None
    assert usage_rows == []
    assert telemetry.events == []


def test_retry_prepare_hook_failure_preserves_prior_provider_failure() -> None:
    """后续 retry 的 pre-send abort 不得抹掉前一真实 provider 失败。"""
    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    operation_calls: list[str] = []

    class SecondPrepareExplodes(UsageLedgerLifecycle):
        prepare_calls = 0

        def prepare(self, **kwargs):
            self.prepare_calls += 1
            if self.prepare_calls == 2:
                raise RuntimeError("retry prepare storage down")
            return super().prepare(**kwargs)

    def operation(context: ProviderCallContext, request):
        del request
        operation_calls.append(context.provider_call_id)
        raise ProviderFailure("upstream_503", status_code=503, retryable=True, sent=True)

    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id="pc-caller-retry-prep",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(seconds=60),
        resource_id="resource-1",
    )
    circuits = CircuitBreakerRegistry(threshold=1)
    telemetry = InMemoryProviderTelemetry()
    with pytest.raises(RuntimeError, match="retry prepare storage down"):
        run_provider_call_with_usage(
            operation=operation,
            context=context,
            model="m",
            lifecycle=SecondPrepareExplodes(ledger),
            measurement_extractor=lambda _v, _c, _f: measurement(),
            ownership_provider=lambda _c: ownership(),
            execution_kind="generation",
            execution_id="gen_retry_prep",
            request_fingerprint="fp-retry-prep",
            now=lambda: NOW,
            circuits=circuits,
            sleep=lambda _d: None,
            jitter=lambda _d: 0,
            telemetry=telemetry,
        )

    assert operation_calls == [_physical_provider_call_id(context.provider_call_id, 1)]
    assert len(telemetry.events) == 1
    assert telemetry.events[0].state == "failed"
    assert telemetry.events[0].error_class == "upstream_503"
    assert telemetry.events[0].attempts == 1
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(provider_call_table).where(
                    provider_call_table.c.provider_call_id
                    == _physical_provider_call_id(context.provider_call_id, 1)
                )
            )
            .mappings()
            .all()
        )
    assert [(row["provider_call_id"], row["status"]) for row in rows] == [
        (_physical_provider_call_id(context.provider_call_id, 1), "completed")
    ]
    with pytest.raises(CircuitOpen):
        call_with_policy(
            lambda _ctx, _request: "ok",
            context,
            circuits=circuits,
            now=lambda: NOW,
        )


def test_generic_operation_exception_terminates_as_unknown() -> None:
    """三轮复审 R2：operation 普通异常 → 原始异常最终向外传播（唯一公开契约），
    已 dispatching 的调用确定性终态 unknown，无 usage；policy 同样观察到 unknown。"""
    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    call_ids: list[str] = []
    telemetry = InMemoryProviderTelemetry()

    def operation(context: ProviderCallContext, request):
        del request
        call_ids.append(context.provider_call_id)
        raise RuntimeError("provider adapter crashed")

    current = [NOW]
    run = make_runner(
        ledger,
        operation,
        current_time=current,
        advance=lambda _d: None,
        telemetry=telemetry,
    )
    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id="pc-caller-gen",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(seconds=60),
        resource_id="resource-1",
    )
    # R2：只接受原始异常向外传播（不再断言 state in (...)）。
    with pytest.raises(RuntimeError, match="provider adapter crashed"):
        run(context)
    assert call_ids == [_physical_provider_call_id(context.provider_call_id, 1)]
    assert [event.state for event in telemetry.events] == ["unknown"]
    with engine.connect() as connection:
        usage_rows = (
            connection.execute(
                select(usage_event_table).where(usage_event_table.c.event_kind == "provider_usage")
            )
            .mappings()
            .all()
        )
        call_rows = (
            connection.execute(
                select(provider_call_table).where(
                    provider_call_table.c.provider_call_id
                    == _physical_provider_call_id(context.provider_call_id, 1)
                )
            )
            .mappings()
            .all()
        )
    assert usage_rows == []
    assert len(call_rows) == 1
    assert call_rows[0]["status"] == "unknown"  # 确定性终态，无 dispatching 残留


@pytest.mark.parametrize("asynchronous", [False, True])
def test_operation_cancellation_terminates_as_unknown_and_reraises(
    asynchronous: bool,
) -> None:
    """已 dispatch 的同步/异步预算 operation 取消均先落 unknown，再原样重抛。"""
    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    cancelled = asyncio.CancelledError("provider operation cancelled")

    def operation(context: ProviderCallContext, request):
        del context, request
        raise cancelled

    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id="pc-caller-cancelled",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(seconds=60),
        resource_id="resource-1",
    )
    with pytest.raises(asyncio.CancelledError) as failure:
        run_provider_call_with_usage(
            operation=operation,
            context=context,
            model="m",
            lifecycle=UsageLedgerLifecycle(ledger),
            measurement_extractor=lambda value, ctx, provider_failure: measurement(),
            ownership_provider=lambda ctx: ownership(),
            execution_kind="generation",
            execution_id=f"gen_cancelled_{asynchronous}",
            request_fingerprint=f"fp-cancelled-{asynchronous}",
            asynchronous=asynchronous,
            policy=RetryPolicy(synchronous_attempts=1, asynchronous_attempts=1),
            now=lambda: NOW,
            circuits=CircuitBreakerRegistry(),
        )
    assert failure.value is cancelled

    with engine.connect() as connection:
        call_row = connection.execute(select(provider_call_table)).mappings().one()
        usage_rows = connection.execute(select(usage_event_table)).mappings().all()
    assert call_row["status"] == "unknown"
    assert call_row["started_at_utc"] is not None
    assert usage_rows == []


def test_operation_cancellation_terminalization_error_takes_precedence() -> None:
    """取消后的 unknown 写失败必须原样浮出，不能被 policy 吞掉或计入 circuit。"""
    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    cancelled = asyncio.CancelledError("provider operation cancelled")

    class ExplodingUnknownLifecycle(UsageLedgerLifecycle):
        def mark_unknown(self, provider_call_id: str) -> None:
            del provider_call_id
            raise OSError("cancelled unknown ledger write failed")

    def operation(context: ProviderCallContext, request):
        del context, request
        raise cancelled

    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id="pc-caller-cancelled-terminal-error",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(seconds=60),
        resource_id="resource-1",
    )
    circuits = CircuitBreakerRegistry(threshold=1)
    with pytest.raises(OSError, match="cancelled unknown ledger write failed"):
        run_provider_call_with_usage(
            operation=operation,
            context=context,
            model="m",
            lifecycle=ExplodingUnknownLifecycle(ledger),
            measurement_extractor=lambda value, ctx, provider_failure: measurement(),
            ownership_provider=lambda ctx: ownership(),
            execution_kind="generation",
            execution_id="gen_cancelled_terminal_error",
            request_fingerprint="fp-cancelled-terminal-error",
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: NOW,
            circuits=circuits,
        )

    with engine.connect() as connection:
        call_row = connection.execute(select(provider_call_table)).mappings().one()
        usage_rows = connection.execute(select(usage_event_table)).mappings().all()
    assert call_row["status"] == "dispatching"
    assert usage_rows == []
    assert (
        call_with_policy(
            lambda _ctx, _request: "ok",
            context,
            policy=RetryPolicy(synchronous_attempts=1),
            circuits=circuits,
            now=lambda: NOW,
        ).state
        == "succeeded"
    )


def test_pre_send_cancellation_releases_half_open_probe() -> None:
    """发送前取消是本地中性结果：half-open probe 释放且不重新打开 circuit。"""
    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    current = [NOW]
    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id="pc-caller-half-open-cancelled",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(minutes=10),
        resource_id="resource-1",
    )
    circuits = CircuitBreakerRegistry(threshold=1, open_seconds=60)

    def fail_provider(_context: ProviderCallContext, request):
        del request
        raise ProviderFailure("upstream_503", status_code=503, retryable=True, sent=True)

    assert (
        call_with_policy(
            fail_provider,
            context,
            policy=RetryPolicy(synchronous_attempts=1),
            circuits=circuits,
            now=lambda: current[0],
        ).state
        == "failed"
    )
    current[0] = NOW + timedelta(seconds=61)
    cancelled = asyncio.CancelledError("half-open pre-send cancelled")

    class CancelledPrepareLifecycle(UsageLedgerLifecycle):
        def prepare(self, **kwargs):
            raise cancelled

    with pytest.raises(asyncio.CancelledError) as failure:
        run_provider_call_with_usage(
            operation=lambda _ctx, _request: "unexpected",
            context=context,
            model="m",
            lifecycle=CancelledPrepareLifecycle(ledger),
            measurement_extractor=lambda value, ctx, provider_failure: measurement(),
            ownership_provider=lambda ctx: ownership(),
            execution_kind="generation",
            execution_id="gen_half_open_cancelled",
            request_fingerprint="fp-half-open-cancelled",
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
            circuits=circuits,
        )
    assert failure.value is cancelled
    assert (
        call_with_policy(
            lambda _ctx, _request: "ok",
            context,
            policy=RetryPolicy(synchronous_attempts=1),
            circuits=circuits,
            now=lambda: current[0],
        ).state
        == "succeeded"
    )


def test_dispatch_hook_cancellation_terminalizes_not_sent() -> None:
    """dispatch hook 提交后但 operation 前取消，必须回退 not_sent 而非残留 dispatching。"""
    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    cancelled = asyncio.CancelledError("dispatch hook cancelled")
    sent: list[str] = []

    class CancelledAfterDispatchLifecycle(UsageLedgerLifecycle):
        def mark_dispatching(self, provider_call_id, *, started_at_provider):
            assert super().mark_dispatching(
                provider_call_id, started_at_provider=started_at_provider
            )
            raise cancelled

    def operation(context: ProviderCallContext, request):
        del request
        sent.append(context.provider_call_id)
        return "unexpected"

    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id="pc-caller-dispatch-cancelled",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(seconds=60),
        resource_id="resource-1",
    )
    with pytest.raises(asyncio.CancelledError) as failure:
        run_provider_call_with_usage(
            operation=operation,
            context=context,
            model="m",
            lifecycle=CancelledAfterDispatchLifecycle(ledger),
            measurement_extractor=lambda value, ctx, provider_failure: measurement(),
            ownership_provider=lambda ctx: ownership(),
            execution_kind="generation",
            execution_id="gen_dispatch_cancelled",
            request_fingerprint="fp-dispatch-cancelled",
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: NOW,
            circuits=CircuitBreakerRegistry(),
        )
    assert failure.value is cancelled
    assert sent == []
    with engine.connect() as connection:
        call_row = connection.execute(select(provider_call_table)).mappings().one()
        usage_rows = connection.execute(select(usage_event_table)).mappings().all()
    assert call_row["status"] == "not_sent"
    assert call_row["started_at_utc"] is None
    assert usage_rows == []


def test_final_pre_send_clock_cancellation_terminalizes_not_sent() -> None:
    """dispatch 已提交后的最终 clock 取消：确定未发送，先 not_sent 再原样重抛。"""
    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    cancelled = asyncio.CancelledError("final pre-send clock cancelled")
    dispatch_returned = [False]
    sent: list[str] = []

    class TrackingDispatchLifecycle(UsageLedgerLifecycle):
        def mark_dispatching(self, provider_call_id, *, started_at_provider):
            committed = super().mark_dispatching(
                provider_call_id, started_at_provider=started_at_provider
            )
            dispatch_returned[0] = True
            return committed

    def clock_now() -> datetime:
        if dispatch_returned[0]:
            raise cancelled
        return NOW

    def operation(context: ProviderCallContext, request):
        del request
        sent.append(context.provider_call_id)
        return "unexpected"

    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id="pc-caller-final-clock-cancelled",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(seconds=60),
        resource_id="resource-1",
    )
    with pytest.raises(asyncio.CancelledError) as failure:
        run_provider_call_with_usage(
            operation=operation,
            context=context,
            model="m",
            lifecycle=TrackingDispatchLifecycle(ledger),
            measurement_extractor=lambda value, ctx, provider_failure: measurement(),
            ownership_provider=lambda ctx: ownership(),
            execution_kind="generation",
            execution_id="gen_final_clock_cancelled",
            request_fingerprint="fp-final-clock-cancelled",
            policy=RetryPolicy(synchronous_attempts=1),
            now=clock_now,
            circuits=CircuitBreakerRegistry(),
        )
    assert failure.value is cancelled
    assert sent == []
    with engine.connect() as connection:
        call_row = connection.execute(select(provider_call_table)).mappings().one()
        usage_rows = connection.execute(select(usage_event_table)).mappings().all()
    assert call_row["status"] == "not_sent"
    assert call_row["started_at_utc"] is None
    assert usage_rows == []


def test_final_pre_send_clock_cancellation_terminalization_error_wins() -> None:
    """最终 clock 取消后的 not_sent 写失败：ledger 错误优先且 circuit 中性。"""
    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    cancelled = asyncio.CancelledError("final pre-send clock cancelled")
    dispatch_returned = [False]
    sent: list[str] = []

    class ExplodingNotSentLifecycle(UsageLedgerLifecycle):
        def mark_dispatching(self, provider_call_id, *, started_at_provider):
            committed = super().mark_dispatching(
                provider_call_id, started_at_provider=started_at_provider
            )
            dispatch_returned[0] = True
            return committed

        def mark_not_sent(self, provider_call_id: str) -> None:
            del provider_call_id
            raise OSError("cancelled not-sent ledger write failed")

    def clock_now() -> datetime:
        if dispatch_returned[0]:
            raise cancelled
        return NOW

    def operation(context: ProviderCallContext, request):
        del request
        sent.append(context.provider_call_id)
        return "unexpected"

    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id="pc-caller-final-clock-terminal-error",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(seconds=60),
        resource_id="resource-1",
    )
    circuits = CircuitBreakerRegistry(threshold=1)
    with pytest.raises(OSError, match="cancelled not-sent ledger write failed"):
        run_provider_call_with_usage(
            operation=operation,
            context=context,
            model="m",
            lifecycle=ExplodingNotSentLifecycle(ledger),
            measurement_extractor=lambda value, ctx, provider_failure: measurement(),
            ownership_provider=lambda ctx: ownership(),
            execution_kind="generation",
            execution_id="gen_final_clock_terminal_error",
            request_fingerprint="fp-final-clock-terminal-error",
            policy=RetryPolicy(synchronous_attempts=1),
            now=clock_now,
            circuits=circuits,
        )
    assert sent == []
    with engine.connect() as connection:
        call_row = connection.execute(select(provider_call_table)).mappings().one()
        usage_rows = connection.execute(select(usage_event_table)).mappings().all()
    assert call_row["status"] == "dispatching"
    assert call_row["started_at_utc"] is not None
    assert usage_rows == []
    assert (
        call_with_policy(
            lambda _ctx, _request: "ok",
            context,
            policy=RetryPolicy(synchronous_attempts=1),
            circuits=circuits,
            now=lambda: NOW,
        ).state
        == "succeeded"
    )


@pytest.mark.parametrize("broken", ["extractor", "ownership", "complete"])
def test_post_operation_hook_cancellation_marks_unknown_and_reraises(broken: str) -> None:
    """operation 已发送成功：三个同步 usage hook 取消均落 unknown 且无 usage。"""
    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    cancelled = asyncio.CancelledError(f"{broken} cancelled")

    def operation(context: ProviderCallContext, request):
        del context, request
        return "ok"

    def extractor(value, context, failure):
        del value, context, failure
        if broken == "extractor":
            raise cancelled
        return measurement()

    def ownership_provider(context):
        del context
        if broken == "ownership":
            raise cancelled
        return ownership()

    class CancelledCompleteLifecycle(UsageLedgerLifecycle):
        def complete(self, **kwargs):
            if broken == "complete":
                raise cancelled
            return super().complete(**kwargs)

    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id=f"pc-caller-{broken}-cancelled",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(seconds=60),
        resource_id="resource-1",
    )
    with pytest.raises(asyncio.CancelledError) as failure:
        run_provider_call_with_usage(
            operation=operation,
            context=context,
            model="m",
            lifecycle=CancelledCompleteLifecycle(ledger),
            measurement_extractor=extractor,
            ownership_provider=ownership_provider,
            execution_kind="generation",
            execution_id=f"gen_{broken}_cancelled",
            request_fingerprint=f"fp-{broken}-cancelled",
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: NOW,
            circuits=CircuitBreakerRegistry(),
        )
    assert failure.value is cancelled
    with engine.connect() as connection:
        call_row = connection.execute(select(provider_call_table)).mappings().one()
        usage_rows = connection.execute(select(usage_event_table)).mappings().all()
    assert call_row["status"] == "unknown"
    assert call_row["started_at_utc"] is not None
    assert usage_rows == []


def test_post_operation_hook_cancellation_terminalization_error_wins() -> None:
    """post-operation hook 取消后的 unknown 写失败时，ledger 错误优先且 circuit 中性。"""
    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    cancelled = asyncio.CancelledError("extractor cancelled")

    class ExplodingUnknownLifecycle(UsageLedgerLifecycle):
        def mark_unknown(self, provider_call_id: str) -> None:
            del provider_call_id
            raise OSError("post-hook unknown ledger write failed")

    def operation(context: ProviderCallContext, request):
        del context, request
        return "ok"

    def extractor(value, context, failure):
        del value, context, failure
        raise cancelled

    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id="pc-caller-post-hook-terminal-error",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(seconds=60),
        resource_id="resource-1",
    )
    circuits = CircuitBreakerRegistry(threshold=1)
    with pytest.raises(OSError, match="post-hook unknown ledger write failed"):
        run_provider_call_with_usage(
            operation=operation,
            context=context,
            model="m",
            lifecycle=ExplodingUnknownLifecycle(ledger),
            measurement_extractor=extractor,
            ownership_provider=lambda ctx: ownership(),
            execution_kind="generation",
            execution_id="gen_post_hook_terminal_error",
            request_fingerprint="fp-post-hook-terminal-error",
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: NOW,
            circuits=circuits,
        )
    with engine.connect() as connection:
        call_row = connection.execute(select(provider_call_table)).mappings().one()
        usage_rows = connection.execute(select(usage_event_table)).mappings().all()
    assert call_row["status"] == "dispatching"
    assert usage_rows == []
    assert (
        call_with_policy(
            lambda _ctx, _request: "ok",
            context,
            policy=RetryPolicy(synchronous_attempts=1),
            circuits=circuits,
            now=lambda: NOW,
        ).state
        == "succeeded"
    )


def test_policy_clock_cancellation_after_completion_preserves_usage() -> None:
    """completed 已提交后 policy clock 取消不能回写 unknown 或删除 usage。"""
    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    cancelled = asyncio.CancelledError("post-completion policy clock cancelled")
    completed = [False]

    class TrackingCompleteLifecycle(UsageLedgerLifecycle):
        def complete(self, **kwargs):
            event_id = super().complete(**kwargs)
            completed[0] = True
            return event_id

    def clock_now() -> datetime:
        if completed[0]:
            raise cancelled
        return NOW

    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id="pc-caller-post-completion-clock-cancelled",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(seconds=60),
        resource_id="resource-1",
    )
    with pytest.raises(asyncio.CancelledError) as failure:
        run_provider_call_with_usage(
            operation=lambda _ctx, _request: "ok",
            context=context,
            model="m",
            lifecycle=TrackingCompleteLifecycle(ledger),
            measurement_extractor=lambda value, ctx, provider_failure: measurement(),
            ownership_provider=lambda ctx: ownership(),
            execution_kind="generation",
            execution_id="gen_post_completion_clock_cancelled",
            request_fingerprint="fp-post-completion-clock-cancelled",
            policy=RetryPolicy(synchronous_attempts=1),
            now=clock_now,
            circuits=CircuitBreakerRegistry(),
        )
    assert failure.value is cancelled
    with engine.connect() as connection:
        call_row = connection.execute(select(provider_call_table)).mappings().one()
        usage_rows = connection.execute(select(usage_event_table)).mappings().all()
    assert call_row["status"] == "completed"
    assert len(usage_rows) == 1
    assert usage_rows[0]["event_kind"] == "provider_usage"


@pytest.mark.parametrize("persistent_clock_failure", [False, True])
def test_completed_policy_clock_cancellation_releases_half_open_probe(
    persistent_clock_failure: bool,
) -> None:
    """completed 后 clock 取消不应重开或永久占用 half-open probe。"""
    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    current = [NOW]
    completed = [False]
    cancellation_calls = [0]
    cancelled = asyncio.CancelledError("post-completion policy clock cancelled")
    circuits = CircuitBreakerRegistry(threshold=1, open_seconds=60)
    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id="pc-caller-completed-half-open-cancelled",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(minutes=10),
        resource_id="resource-1",
    )

    def unavailable(_ctx: ProviderCallContext, request):
        del request
        raise ProviderFailure("upstream_503", status_code=503, retryable=True, sent=True)

    assert (
        call_with_policy(
            unavailable,
            context,
            policy=RetryPolicy(synchronous_attempts=1),
            circuits=circuits,
            now=lambda: current[0],
        ).state
        == "failed"
    )
    current[0] += timedelta(seconds=61)

    class TrackingCompleteLifecycle(UsageLedgerLifecycle):
        def complete(self, **kwargs):
            event_id = super().complete(**kwargs)
            completed[0] = True
            return event_id

    def clock_now() -> datetime:
        if completed[0]:
            cancellation_calls[0] += 1
            if persistent_clock_failure or cancellation_calls[0] == 1:
                raise cancelled
        return current[0]

    with pytest.raises(asyncio.CancelledError) as failure:
        run_provider_call_with_usage(
            operation=lambda _ctx, _request: "ok",
            context=context,
            model="m",
            lifecycle=TrackingCompleteLifecycle(ledger),
            measurement_extractor=lambda value, ctx, provider_failure: measurement(),
            ownership_provider=lambda ctx: ownership(),
            execution_kind="generation",
            execution_id=f"gen_completed_half_open_{persistent_clock_failure}",
            request_fingerprint=f"fp-completed-half-open-{persistent_clock_failure}",
            policy=RetryPolicy(synchronous_attempts=1),
            now=clock_now,
            circuits=circuits,
        )
    assert failure.value is cancelled
    with engine.connect() as connection:
        call_row = connection.execute(select(provider_call_table)).mappings().one()
        usage_rows = connection.execute(select(usage_event_table)).mappings().all()
    assert call_row["status"] == "completed"
    assert len(usage_rows) == 1
    assert (
        call_with_policy(
            lambda _ctx, _request: "ok",
            context,
            policy=RetryPolicy(synchronous_attempts=1),
            circuits=circuits,
            now=lambda: current[0],
        ).state
        == "succeeded"
    )


def test_pre_send_cancellation_does_not_mark_unknown() -> None:
    """prepare 阶段尚未发送：取消原样传播，且不能伪造 unknown 或 usage。"""
    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    cancelled = asyncio.CancelledError("pre-send cancelled")
    unknown_calls: list[str] = []
    sent: list[str] = []

    class CancelledPrepareLifecycle(UsageLedgerLifecycle):
        def prepare(self, **kwargs):
            raise cancelled

        def mark_unknown(self, provider_call_id: str) -> None:
            unknown_calls.append(provider_call_id)
            super().mark_unknown(provider_call_id)

    def operation(context: ProviderCallContext, request):
        del request
        sent.append(context.provider_call_id)
        return "unexpected"

    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id="pc-caller-pre-send-cancelled",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(seconds=60),
        resource_id="resource-1",
    )
    with pytest.raises(asyncio.CancelledError) as failure:
        run_provider_call_with_usage(
            operation=operation,
            context=context,
            model="m",
            lifecycle=CancelledPrepareLifecycle(ledger),
            measurement_extractor=lambda value, ctx, provider_failure: measurement(),
            ownership_provider=lambda ctx: ownership(),
            execution_kind="generation",
            execution_id="gen_pre_send_cancelled",
            request_fingerprint="fp-pre-send-cancelled",
            policy=RetryPolicy(synchronous_attempts=1, asynchronous_attempts=1),
            now=lambda: NOW,
            circuits=CircuitBreakerRegistry(),
        )
    assert failure.value is cancelled
    assert unknown_calls == []
    assert sent == []
    with engine.connect() as connection:
        assert connection.execute(select(provider_call_table)).mappings().all() == []
        assert connection.execute(select(usage_event_table)).mappings().all() == []


def test_generic_operation_terminalization_error_takes_precedence() -> None:
    """R2：mark_unknown 持久化失败不能被原 operation 错误掩盖。"""
    engine, ledger = make_ledger()
    seed_price(engine, ledger)

    class ExplodingUnknownLifecycle(UsageLedgerLifecycle):
        def mark_unknown(self, provider_call_id: str) -> None:
            del provider_call_id
            raise OSError("unknown ledger write failed")

    def operation(context: ProviderCallContext, request):
        del context, request
        raise RuntimeError("provider adapter crashed")

    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id="pc-caller-terminal",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(seconds=60),
        resource_id="resource-1",
    )
    with pytest.raises(OSError, match="unknown ledger write failed"):
        run_provider_call_with_usage(
            operation=operation,
            context=context,
            model="m",
            lifecycle=ExplodingUnknownLifecycle(ledger),
            measurement_extractor=lambda value, ctx, failure: measurement(),
            ownership_provider=lambda ctx: ownership(),
            execution_kind="generation",
            execution_id="gen_terminal_error",
            request_fingerprint="fp-terminal-error",
            now=lambda: NOW,
            circuits=CircuitBreakerRegistry(),
        )
    with engine.connect() as connection:
        status = connection.execute(
            select(provider_call_table.c.status).where(
                provider_call_table.c.provider_call_id
                == _physical_provider_call_id(context.provider_call_id, 1)
            )
        ).scalar_one()
    assert status == "dispatching"  # ledger 写故障已显式传播，不能伪称终态化成功


def test_hook_error_in_completion_is_unknown_and_reraises() -> None:
    """二轮复审 Important 1：operation 成功但 lifecycle.complete（账本）失败 →
    调用终态 unknown + hook 错误原样浮出（不伪装成普通 provider failure）。"""
    engine, ledger = make_ledger()
    seed_price(engine, ledger)

    class ExplodingLifecycle(UsageLedgerLifecycle):
        def complete(self, **kwargs):
            raise RuntimeError("ledger persistent storage down")

    def operation(context: ProviderCallContext, request):
        del context, request
        return "ok"

    def extractor(value, context, failure):
        del value, context, failure
        return measurement()

    def ownership_provider(context):
        del context
        return ownership()

    current = [NOW]
    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id="pc-caller-hook",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(seconds=60),
        resource_id="resource-1",
    )
    with pytest.raises(RuntimeError, match="ledger persistent storage down"):
        run_provider_call_with_usage(
            operation=operation,
            context=context,
            model="m",
            lifecycle=ExplodingLifecycle(ledger),
            measurement_extractor=extractor,
            ownership_provider=ownership_provider,
            execution_kind="generation",
            execution_id="gen_hook",
            request_fingerprint="fp-hook",
            now=lambda: current[0],
            circuits=CircuitBreakerRegistry(),
            sleep=lambda _d: None,
            jitter=lambda _d: 0,
        )
    with engine.connect() as connection:
        call_row = (
            connection.execute(
                select(provider_call_table).where(
                    provider_call_table.c.provider_call_id
                    == _physical_provider_call_id(context.provider_call_id, 1)
                )
            )
            .mappings()
            .one()
        )
    assert call_row["status"] == "unknown"  # 事实无法落账 → 终态 unknown


def test_measurement_extractor_failure_terminates_as_unknown() -> None:
    """二轮复审 Important 1：operation 成功但 measurement_extractor 抛异常 →
    unknown + hook 错误浮出（不是 ProviderFailure 包装）。"""
    engine, ledger = make_ledger()
    seed_price(engine, ledger)

    def operation(context: ProviderCallContext, request):
        del context, request
        return "ok"

    def extractor(value, context, failure):
        del value, context, failure
        raise ValueError("cannot extract usage")

    def ownership_provider(context):
        del context
        return ownership()

    current = [NOW]
    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id="pc-caller-ext",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(seconds=60),
        resource_id="resource-1",
    )
    with pytest.raises(ValueError, match="cannot extract usage"):
        run_provider_call_with_usage(
            operation=operation,
            context=context,
            model="m",
            lifecycle=UsageLedgerLifecycle(ledger),
            measurement_extractor=extractor,
            ownership_provider=ownership_provider,
            execution_kind="generation",
            execution_id="gen_ext",
            request_fingerprint="fp-ext",
            now=lambda: current[0],
            circuits=CircuitBreakerRegistry(),
            sleep=lambda _d: None,
            jitter=lambda _d: 0,
        )
    with engine.connect() as connection:
        call_row = (
            connection.execute(
                select(provider_call_table).where(
                    provider_call_table.c.provider_call_id
                    == _physical_provider_call_id(context.provider_call_id, 1)
                )
            )
            .mappings()
            .one()
        )
    assert call_row["status"] == "unknown"


def test_operation_runs_after_dispatch_committed_with_started() -> None:
    """二轮复审 Important 3：operation 在 dispatch 事务提交后执行（发送时序），
    operation 内可见 dispatching 状态与已持久化 started_at_utc。"""
    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    observed: list[tuple[str, object]] = []

    def operation(context: ProviderCallContext, request):
        del request
        with engine.connect() as connection:
            row = (
                connection.execute(
                    select(
                        provider_call_table.c.status,
                        provider_call_table.c.started_at_utc,
                    ).where(provider_call_table.c.provider_call_id == context.provider_call_id)
                )
                .mappings()
                .one()
            )
            observed.append((row["status"], row["started_at_utc"]))
        return "ok"

    current = [NOW]
    run = make_runner(ledger, operation, current_time=current, advance=lambda _d: None)
    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id="pc-caller-timing",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(seconds=60),
        resource_id="resource-1",
    )
    result = run(context)
    assert result.state == "succeeded"
    assert len(observed) == 1
    status, started = observed[0]
    assert status == "dispatching"  # dispatch 短事务已提交（operation 开始时）
    assert started == NOW.replace(tzinfo=None)  # started 在 dispatch 持久化


@pytest.mark.parametrize(
    "broken",
    ["extractor", "ownership", "complete"],
)
def test_known_failure_hook_error_marks_unknown_and_reraises(broken: str) -> None:
    """三轮复审 R1：已知失败（sent=True + status_code）路径中，extractor / ownership /
    complete 任一异常 → 独立安全终态 mark_unknown 回退 + 原 hook 异常传播；该 attempt
    最终 unknown，无 dispatching 残留。"""
    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    call_ids: list[str] = []

    def operation(context: ProviderCallContext, request):
        del request
        call_ids.append(context.provider_call_id)
        raise ProviderFailure("upstream_503", status_code=503, retryable=False, sent=True)

    def extractor(value, context, failure):
        del value, context, failure
        if broken == "extractor":
            raise ValueError("extractor boom")
        return measurement()

    def ownership_provider(context):
        del context
        if broken == "ownership":
            raise KeyError("ownership boom")
        return ownership()

    class ExplodingCompleteLifecycle(UsageLedgerLifecycle):
        def complete(self, **kwargs):
            if broken == "complete":
                raise RuntimeError("complete boom")
            return super().complete(**kwargs)

    lifecycle = ExplodingCompleteLifecycle(ledger)
    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id="pc-caller-kf",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(seconds=60),
        resource_id="resource-1",
    )
    expected = {
        "extractor": ValueError,
        "ownership": KeyError,
        "complete": RuntimeError,
    }[broken]
    with pytest.raises(expected, match=f"{broken} boom"):
        run_provider_call_with_usage(
            operation=operation,
            context=context,
            model="m",
            lifecycle=lifecycle,
            measurement_extractor=extractor,
            ownership_provider=ownership_provider,
            execution_kind="generation",
            execution_id=f"gen_kf_{broken}",
            request_fingerprint=f"fp-kf-{broken}",
            now=lambda: NOW,
            circuits=CircuitBreakerRegistry(),
            sleep=lambda _d: None,
            jitter=lambda _d: 0,
        )
    assert call_ids == [_physical_provider_call_id(context.provider_call_id, 1)]
    with engine.connect() as connection:
        call_rows = (
            connection.execute(
                select(provider_call_table).where(
                    provider_call_table.c.provider_call_id.in_(call_ids)
                )
            )
            .mappings()
            .all()
        )
        usage_rows = connection.execute(select(usage_event_table)).mappings().all()
    assert [row["status"] for row in call_rows] == ["unknown"]
    assert usage_rows == []


def test_started_callback_not_invoked_before_transaction_begins() -> None:
    """三轮复审 R3：started callback 由 ledger 在 dispatch 事务内延迟调用——在事务
    begin 入口阻塞时 callback 不得提前调用；放行后才在条件 UPDATE 前调用并持久化。"""
    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    call_id = ledger.prepare_provider_call(
        provider="test-provider",
        model="m",
        operation="generate",
        execution_kind="generation",
        execution_id="gen_cb",
        generation_id="gen_cb",
        deadline_utc=NOW + timedelta(minutes=5),
        request_fingerprint="fp-cb",
    )
    callback_called = threading.Event()
    began = threading.Event()
    release_begin = threading.Event()
    update_reached = threading.Event()
    marker_state = {"called": False, "called_before_update": False}
    errors: list[BaseException] = []

    def started_provider():
        marker_state["called"] = True
        callback_called.set()
        return NOW

    def block_at_begin(conn):
        del conn
        began.set()
        if not release_begin.wait(15):
            raise TimeoutError("begin release timed out")

    def observe_update(conn, cursor, statement, parameters, context, executemany):
        del conn, cursor, parameters, context, executemany
        normalized = " ".join(statement.upper().split())
        if normalized.startswith("UPDATE PROVIDER_CALL SET"):
            marker_state["called_before_update"] = marker_state["called"]
            update_reached.set()

    event.listen(engine, "begin", block_at_begin)
    event.listen(engine, "before_cursor_execute", observe_update)

    def run_dispatch() -> None:
        try:
            ledger.mark_dispatching(call_id, started_at_provider=started_provider)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=run_dispatch)
    thread.start()
    try:
        assert began.wait(15)
        # begin 入口已触发但仍被阻塞；adapter/ledger 都不得提前求值 callback。
        assert marker_state["called"] is False
        assert not callback_called.is_set()
        release_begin.set()
        assert update_reached.wait(15)
    finally:
        release_begin.set()
        thread.join(15)
    assert not thread.is_alive()
    assert errors == []
    assert marker_state == {"called": True, "called_before_update": True}
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(
                    provider_call_table.c.status,
                    provider_call_table.c.started_at_utc,
                ).where(provider_call_table.c.provider_call_id == call_id)
            )
            .mappings()
            .one()
        )
    assert row["status"] == "dispatching"
    assert row["started_at_utc"] == NOW.replace(tzinfo=None)


@pytest.mark.parametrize("phase", ["post_prepare", "post_dispatch"])
@pytest.mark.parametrize("starting_state", ["closed", "half_open"])
@pytest.mark.parametrize("persistent", [False, True])
def test_pre_send_clock_exception_terminalizes_not_sent_and_is_circuit_neutral(
    phase: str,
    starting_state: str,
    persistent: bool,
) -> None:
    """发送前 direct clock 普通异常必须 not_sent，并保持 closed/half-open 中性。"""

    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    current = [NOW]
    circuits = CircuitBreakerRegistry(threshold=1, open_seconds=60)
    if starting_state == "half_open":
        seeded = call_with_policy(
            lambda _ctx, _request: (_ for _ in ()).throw(
                ProviderFailure("seed_timeout", retryable=True)
            ),
            ProviderCallContext(
                provider="test-provider",
                operation="generate",
                provider_call_id="pc-seed-clock-failure",
                attempt_id="seed-attempt",
                deadline_utc=current[0] + timedelta(minutes=5),
            ),
            policy=RetryPolicy(synchronous_attempts=1),
            circuits=circuits,
            now=lambda: current[0],
        )
        assert seeded.state == "failed"
        current[0] += timedelta(seconds=61)

    active = [False]
    clock_failures = [0]
    injected = RuntimeError(f"{phase} clock failed")
    operation_calls: list[str] = []
    telemetry = InMemoryProviderTelemetry()

    class ActivateClockFailure(UsageLedgerLifecycle):
        def prepare(self, **kwargs):
            created = super().prepare(**kwargs)
            if phase == "post_prepare":
                active[0] = True
            return created

        def mark_dispatching(self, provider_call_id, *, started_at_provider):
            committed = super().mark_dispatching(
                provider_call_id, started_at_provider=started_at_provider
            )
            if phase == "post_dispatch":
                active[0] = True
            return committed

    def clock_now() -> datetime:
        if active[0]:
            clock_failures[0] += 1
            if persistent or clock_failures[0] == 1:
                raise injected
        return current[0]

    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id=f"pc-caller-{phase}-{starting_state}-{persistent}",
        attempt_id="attempt-root",
        deadline_utc=current[0] + timedelta(minutes=5),
        resource_id="resource-1",
    )
    with pytest.raises(RuntimeError) as caught:
        run_provider_call_with_usage(
            operation=lambda ctx, _request: operation_calls.append(ctx.provider_call_id),
            context=context,
            model="m",
            lifecycle=ActivateClockFailure(ledger),
            measurement_extractor=lambda _value, _ctx, _failure: measurement(),
            ownership_provider=lambda _ctx: ownership(),
            execution_kind="generation",
            execution_id=f"gen_{phase}_{starting_state}_{persistent}",
            request_fingerprint=f"fp-{phase}-{starting_state}-{persistent}",
            policy=RetryPolicy(synchronous_attempts=1),
            circuits=circuits,
            telemetry=telemetry,
            now=clock_now,
        )
    assert caught.value is injected
    assert operation_calls == []
    with engine.connect() as connection:
        row = connection.execute(select(provider_call_table)).mappings().one()
        usage_rows = connection.execute(select(usage_event_table)).mappings().all()
    assert row["status"] == "not_sent"
    assert row["started_at_utc"] is None
    assert usage_rows == []
    assert telemetry.events == []

    active[0] = False
    recovered = call_with_policy(
        lambda _ctx, _request: "ok",
        context,
        circuits=circuits,
        policy=RetryPolicy(synchronous_attempts=1),
        now=lambda: current[0],
    )
    assert recovered.state == "succeeded"


@pytest.mark.parametrize("phase", ["post_prepare", "post_dispatch"])
def test_pre_send_clock_invalid_return_terminalizes_not_sent(phase: str) -> None:
    """发送前 clock 非 datetime 返回属于本地合同错误，不得伪装成 provider failure。"""

    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    active = [False]
    operation_calls: list[str] = []
    circuits = CircuitBreakerRegistry(threshold=1)

    class ActivateInvalidClock(UsageLedgerLifecycle):
        def prepare(self, **kwargs):
            created = super().prepare(**kwargs)
            if phase == "post_prepare":
                active[0] = True
            return created

        def mark_dispatching(self, provider_call_id, *, started_at_provider):
            committed = super().mark_dispatching(
                provider_call_id, started_at_provider=started_at_provider
            )
            if phase == "post_dispatch":
                active[0] = True
            return committed

    def invalid_clock():
        return None if active[0] else NOW

    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id=f"pc-caller-{phase}-invalid-clock",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(minutes=5),
        resource_id="resource-1",
    )
    with pytest.raises(TypeError, match="clock callback must return datetime"):
        run_provider_call_with_usage(
            operation=lambda ctx, _request: operation_calls.append(ctx.provider_call_id),
            context=context,
            model="m",
            lifecycle=ActivateInvalidClock(ledger),
            measurement_extractor=lambda _value, _ctx, _failure: measurement(),
            ownership_provider=lambda _ctx: ownership(),
            execution_kind="generation",
            execution_id=f"gen_{phase}_invalid_clock",
            request_fingerprint=f"fp-{phase}-invalid-clock",
            policy=RetryPolicy(synchronous_attempts=1),
            circuits=circuits,
            now=invalid_clock,
        )
    assert operation_calls == []
    with engine.connect() as connection:
        row = connection.execute(select(provider_call_table)).mappings().one()
    assert row["status"] == "not_sent"
    assert row["started_at_utc"] is None
    assert (
        call_with_policy(
            lambda _ctx, _request: "ok",
            context,
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: NOW,
        ).state
        == "succeeded"
    )


@pytest.mark.parametrize("phase", ["post_prepare", "post_dispatch"])
def test_pre_send_clock_terminalization_error_takes_precedence(phase: str) -> None:
    """clock 本地错误后的 not_sent 写失败拥有最高传播优先级，且不污染 circuit。"""

    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    active = [False]
    operation_calls: list[str] = []
    circuits = CircuitBreakerRegistry(threshold=1)

    class BrokenTerminalLifecycle(UsageLedgerLifecycle):
        def prepare(self, **kwargs):
            created = super().prepare(**kwargs)
            if phase == "post_prepare":
                active[0] = True
            return created

        def mark_dispatching(self, provider_call_id, *, started_at_provider):
            committed = super().mark_dispatching(
                provider_call_id, started_at_provider=started_at_provider
            )
            if phase == "post_dispatch":
                active[0] = True
            return committed

        def mark_not_sent(self, provider_call_id: str) -> None:
            del provider_call_id
            raise OSError("clock terminalization failed")

    def clock_now() -> datetime:
        if active[0]:
            raise RuntimeError("clock failed")
        return NOW

    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id=f"pc-caller-{phase}-terminal-priority",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(minutes=5),
        resource_id="resource-1",
    )
    with pytest.raises(OSError, match="clock terminalization failed"):
        run_provider_call_with_usage(
            operation=lambda ctx, _request: operation_calls.append(ctx.provider_call_id),
            context=context,
            model="m",
            lifecycle=BrokenTerminalLifecycle(ledger),
            measurement_extractor=lambda _value, _ctx, _failure: measurement(),
            ownership_provider=lambda _ctx: ownership(),
            execution_kind="generation",
            execution_id=f"gen_{phase}_terminal_priority",
            request_fingerprint=f"fp-{phase}-terminal-priority",
            policy=RetryPolicy(synchronous_attempts=1),
            circuits=circuits,
            now=clock_now,
        )
    assert operation_calls == []
    active[0] = False
    assert (
        call_with_policy(
            lambda _ctx, _request: "ok",
            context,
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: NOW,
        ).state
        == "succeeded"
    )


@pytest.mark.parametrize("broken", ["extractor", "ownership", "complete"])
@pytest.mark.parametrize("starting_state", ["closed", "half_open"])
def test_known_provider_failure_finalize_cancellation_preserves_accounting(
    broken: str,
    starting_state: str,
) -> None:
    """当前真实 ProviderFailure 必须先 exactly-once accounting，再传播 hook cancellation。"""

    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    current = [NOW]
    circuits = CircuitBreakerRegistry(threshold=1, open_seconds=60)
    if starting_state == "half_open":
        assert (
            call_with_policy(
                lambda _ctx, _request: (_ for _ in ()).throw(
                    ProviderFailure("seed_timeout", retryable=True)
                ),
                ProviderCallContext(
                    provider="test-provider",
                    operation="generate",
                    provider_call_id="pc-seed-finalize-cancel",
                    attempt_id="seed-attempt",
                    deadline_utc=current[0] + timedelta(minutes=5),
                ),
                policy=RetryPolicy(synchronous_attempts=1),
                circuits=circuits,
                now=lambda: current[0],
            ).state
            == "failed"
        )
        current[0] += timedelta(seconds=61)
    cancelled = asyncio.CancelledError(f"{broken} cancelled after provider failure")
    operation_calls: list[str] = []
    telemetry = InMemoryProviderTelemetry()

    def operation(ctx: ProviderCallContext, _request):
        operation_calls.append(ctx.provider_call_id)
        raise ProviderFailure("upstream_503", status_code=503, retryable=True, sent=True)

    def extractor(_value, _ctx, _failure):
        if broken == "extractor":
            raise cancelled
        return measurement()

    def ownership_callback(_ctx):
        if broken == "ownership":
            raise cancelled
        return ownership()

    class CancelFinalizeLifecycle(UsageLedgerLifecycle):
        def complete(self, **kwargs):
            if broken == "complete":
                raise cancelled
            return super().complete(**kwargs)

    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id=f"pc-caller-failure-{broken}-{starting_state}",
        attempt_id="attempt-root",
        deadline_utc=current[0] + timedelta(minutes=5),
        resource_id="resource-1",
    )
    with pytest.raises(asyncio.CancelledError) as caught:
        run_provider_call_with_usage(
            operation=operation,
            context=context,
            model="m",
            lifecycle=CancelFinalizeLifecycle(ledger),
            measurement_extractor=extractor,
            ownership_provider=ownership_callback,
            execution_kind="generation",
            execution_id=f"gen_failure_{broken}_{starting_state}",
            request_fingerprint=f"fp-failure-{broken}-{starting_state}",
            policy=RetryPolicy(synchronous_attempts=3),
            circuits=circuits,
            telemetry=telemetry,
            now=lambda: current[0],
            sleep=lambda _delay: None,
            jitter=lambda _delay: 0,
        )
    assert caught.value is cancelled
    assert len(operation_calls) == 1
    assert len(telemetry.events) == 1
    assert (
        telemetry.events[0].state,
        telemetry.events[0].error_class,
        telemetry.events[0].attempts,
    ) == ("failed", "upstream_503", 1)
    with engine.connect() as connection:
        row = connection.execute(select(provider_call_table)).mappings().one()
        usage_rows = connection.execute(select(usage_event_table)).mappings().all()
    assert row["status"] == "unknown"
    assert usage_rows == []
    with pytest.raises(CircuitOpen):
        call_with_policy(
            lambda _ctx, _request: "too soon",
            context,
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        )


def test_known_provider_failure_complete_commit_then_cancellation_preserves_usage() -> None:
    """failed usage 已提交后 complete cancellation 不得重复回写 unknown/state-conflict。"""

    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    circuits = CircuitBreakerRegistry(threshold=1)
    telemetry = InMemoryProviderTelemetry()
    cancelled = asyncio.CancelledError("complete cancelled after commit")

    class CommitThenCancelLifecycle(UsageLedgerLifecycle):
        def complete(self, **kwargs):
            super().complete(**kwargs)
            raise cancelled

    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id="pc-caller-failure-complete-committed",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(minutes=5),
        resource_id="resource-1",
    )
    with pytest.raises(asyncio.CancelledError) as caught:
        run_provider_call_with_usage(
            operation=lambda _ctx, _request: (_ for _ in ()).throw(
                ProviderFailure("upstream_503", status_code=503, retryable=True, sent=True)
            ),
            context=context,
            model="m",
            lifecycle=CommitThenCancelLifecycle(ledger),
            measurement_extractor=lambda _value, _ctx, _failure: measurement(),
            ownership_provider=lambda _ctx: ownership(),
            execution_kind="generation",
            execution_id="gen_failure_complete_committed",
            request_fingerprint="fp-failure-complete-committed",
            policy=RetryPolicy(synchronous_attempts=3),
            circuits=circuits,
            telemetry=telemetry,
            now=lambda: NOW,
        )
    assert caught.value is cancelled
    assert len(telemetry.events) == 1
    assert telemetry.events[0].error_class == "upstream_503"
    with engine.connect() as connection:
        row = connection.execute(select(provider_call_table)).mappings().one()
        usage_rows = connection.execute(select(usage_event_table)).mappings().all()
    assert row["status"] == "completed"
    assert len(usage_rows) == 1
    assert usage_rows[0]["result"] == "failed"
    with pytest.raises(CircuitOpen):
        call_with_policy(
            lambda _ctx, _request: "too soon",
            context,
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: NOW,
        )


@pytest.mark.parametrize("terminal_state", ["unknown", "not_sent"])
def test_provider_failure_terminal_hook_cancellation_preserves_accounting(
    terminal_state: str,
) -> None:
    """safe-terminal ledger cancellation 优先传播，但不能擦除当前 provider failure。"""

    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    cancelled = asyncio.CancelledError(f"{terminal_state} terminal hook cancelled")
    circuits = CircuitBreakerRegistry(threshold=1)
    telemetry = InMemoryProviderTelemetry()

    class CommitThenCancelTerminalLifecycle(UsageLedgerLifecycle):
        def mark_unknown(self, provider_call_id: str) -> None:
            super().mark_unknown(provider_call_id)
            if terminal_state == "unknown":
                raise cancelled

        def mark_not_sent(self, provider_call_id: str) -> None:
            super().mark_not_sent(provider_call_id)
            if terminal_state == "not_sent":
                raise cancelled

    failure = (
        ProviderFailure("connection_lost", retryable=True, sent=True)
        if terminal_state == "unknown"
        else ProviderFailure("connection_lost", retryable=True, sent=False)
    )
    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id=f"pc-caller-terminal-cancel-{terminal_state}",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(minutes=5),
        resource_id="resource-1",
    )
    with pytest.raises(asyncio.CancelledError) as caught:
        run_provider_call_with_usage(
            operation=lambda _ctx, _request: (_ for _ in ()).throw(failure),
            context=context,
            model="m",
            lifecycle=CommitThenCancelTerminalLifecycle(ledger),
            measurement_extractor=lambda _value, _ctx, _failure: measurement(),
            ownership_provider=lambda _ctx: ownership(),
            execution_kind="generation",
            execution_id=f"gen_terminal_cancel_{terminal_state}",
            request_fingerprint=f"fp-terminal-cancel-{terminal_state}",
            policy=RetryPolicy(synchronous_attempts=3),
            circuits=circuits,
            telemetry=telemetry,
            now=lambda: NOW,
        )
    assert caught.value is cancelled
    assert len(telemetry.events) == 1
    assert telemetry.events[0].error_class == failure.error_class
    with engine.connect() as connection:
        row = connection.execute(select(provider_call_table)).mappings().one()
        usage_rows = connection.execute(select(usage_event_table)).mappings().all()
    assert row["status"] == terminal_state
    assert usage_rows == []
    with pytest.raises(CircuitOpen):
        call_with_policy(
            lambda _ctx, _request: "too soon",
            context,
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: NOW,
        )


@pytest.mark.parametrize("persistent", [False, True])
def test_completed_usage_survives_final_policy_clock_exception(persistent: bool) -> None:
    """completed+usage 后 final now 普通异常保持 success circuit fact 且不泄漏 probe。"""

    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    current = [NOW]
    circuits = CircuitBreakerRegistry(threshold=1, open_seconds=60)
    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id=f"pc-caller-completed-clock-error-{persistent}",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(minutes=10),
        resource_id="resource-1",
    )
    assert (
        call_with_policy(
            lambda _ctx, _request: (_ for _ in ()).throw(
                ProviderFailure("seed_timeout", retryable=True)
            ),
            context,
            policy=RetryPolicy(synchronous_attempts=1),
            circuits=circuits,
            now=lambda: current[0],
        ).state
        == "failed"
    )
    current[0] += timedelta(seconds=61)
    completed = [False]
    failure_calls = [0]
    injected = RuntimeError("post-completion policy clock failed")
    telemetry = InMemoryProviderTelemetry()

    class TrackingCompleteLifecycle(UsageLedgerLifecycle):
        def complete(self, **kwargs):
            event_id = super().complete(**kwargs)
            completed[0] = True
            return event_id

    def clock_now() -> datetime:
        if completed[0]:
            failure_calls[0] += 1
            if persistent or failure_calls[0] == 1:
                raise injected
        return current[0]

    with pytest.raises(RuntimeError) as caught:
        run_provider_call_with_usage(
            operation=lambda _ctx, _request: "ok",
            context=context,
            model="m",
            lifecycle=TrackingCompleteLifecycle(ledger),
            measurement_extractor=lambda _value, _ctx, _failure: measurement(),
            ownership_provider=lambda _ctx: ownership(),
            execution_kind="generation",
            execution_id=f"gen_completed_clock_error_{persistent}",
            request_fingerprint=f"fp-completed-clock-error-{persistent}",
            policy=RetryPolicy(synchronous_attempts=1),
            circuits=circuits,
            telemetry=telemetry,
            now=clock_now,
        )
    assert caught.value is injected
    assert len(telemetry.events) == 1
    assert telemetry.events[0].state == "succeeded"
    with engine.connect() as connection:
        row = connection.execute(select(provider_call_table)).mappings().one()
        usage_rows = connection.execute(select(usage_event_table)).mappings().all()
    assert row["status"] == "completed"
    assert len(usage_rows) == 1
    assert (
        call_with_policy(
            lambda _ctx, _request: "recovered",
            context,
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        ).state
        == "succeeded"
    )


@pytest.mark.parametrize("failure_kind", ["exception", "cancellation", "invalid_return"])
def test_dispatch_started_clock_failure_terminalizes_not_sent(failure_kind: str) -> None:
    """ledger 事务内 started clock 的异常/非法返回仍属于确定未发送。"""

    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    calls = [0]
    operation_calls: list[str] = []
    circuits = CircuitBreakerRegistry(threshold=1)
    injected: BaseException
    if failure_kind == "cancellation":
        injected = asyncio.CancelledError("dispatch started clock cancelled")
    else:
        injected = RuntimeError("dispatch started clock failed")

    def clock_now():
        calls[0] += 1
        if calls[0] == 4:
            if failure_kind == "invalid_return":
                return None
            raise injected
        return NOW

    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id=f"pc-caller-dispatch-started-{failure_kind}",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(minutes=5),
        resource_id="resource-1",
    )
    expected_type = TypeError if failure_kind == "invalid_return" else type(injected)
    with pytest.raises(expected_type) as caught:
        run_provider_call_with_usage(
            operation=lambda ctx, _request: operation_calls.append(ctx.provider_call_id),
            context=context,
            model="m",
            lifecycle=UsageLedgerLifecycle(ledger),
            measurement_extractor=lambda _value, _ctx, _failure: measurement(),
            ownership_provider=lambda _ctx: ownership(),
            execution_kind="generation",
            execution_id=f"gen_dispatch_started_{failure_kind}",
            request_fingerprint=f"fp-dispatch-started-{failure_kind}",
            policy=RetryPolicy(synchronous_attempts=1),
            circuits=circuits,
            now=clock_now,
        )
    if failure_kind != "invalid_return":
        assert caught.value is injected
    assert operation_calls == []
    with engine.connect() as connection:
        row = connection.execute(select(provider_call_table)).mappings().one()
        usage_rows = connection.execute(select(usage_event_table)).mappings().all()
    assert row["status"] == "not_sent"
    assert row["started_at_utc"] is None
    assert usage_rows == []
    assert (
        call_with_policy(
            lambda _ctx, _request: "ok",
            context,
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: NOW,
        ).state
        == "succeeded"
    )


def test_failure_finalize_terminal_cancellation_has_priority_after_accounting() -> None:
    """post-failure hook cancellation 后若 unknown hook 也取消，terminal cancellation 优先。"""

    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    hook_cancelled = asyncio.CancelledError("measurement cancelled")
    terminal_cancelled = asyncio.CancelledError("unknown terminal cancelled")
    circuits = CircuitBreakerRegistry(threshold=1)
    telemetry = InMemoryProviderTelemetry()

    class CancelAfterUnknownLifecycle(UsageLedgerLifecycle):
        def mark_unknown(self, provider_call_id: str) -> None:
            super().mark_unknown(provider_call_id)
            raise terminal_cancelled

    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id="pc-caller-failure-terminal-cancel-priority",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(minutes=5),
        resource_id="resource-1",
    )
    with pytest.raises(asyncio.CancelledError) as caught:
        run_provider_call_with_usage(
            operation=lambda _ctx, _request: (_ for _ in ()).throw(
                ProviderFailure("upstream_503", status_code=503, retryable=True, sent=True)
            ),
            context=context,
            model="m",
            lifecycle=CancelAfterUnknownLifecycle(ledger),
            measurement_extractor=lambda _value, _ctx, _failure: (_ for _ in ()).throw(
                hook_cancelled
            ),
            ownership_provider=lambda _ctx: ownership(),
            execution_kind="generation",
            execution_id="gen_failure_terminal_cancel_priority",
            request_fingerprint="fp-failure-terminal-cancel-priority",
            policy=RetryPolicy(synchronous_attempts=3),
            circuits=circuits,
            telemetry=telemetry,
            now=lambda: NOW,
        )
    assert caught.value is terminal_cancelled
    assert len(telemetry.events) == 1
    assert telemetry.events[0].error_class == "upstream_503"
    with engine.connect() as connection:
        row = connection.execute(select(provider_call_table)).mappings().one()
        usage_rows = connection.execute(select(usage_event_table)).mappings().all()
    assert row["status"] == "unknown"
    assert usage_rows == []
    with pytest.raises(CircuitOpen):
        call_with_policy(
            lambda _ctx, _request: "too soon",
            context,
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: NOW,
        )


def test_saved_terminal_error_precedes_final_policy_clock_exception() -> None:
    """已保存的终态错误不得被 operation 成功后的 policy clock 错误覆盖。"""

    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    terminal_error = OSError("unknown terminalization failed after commit")
    measurement_error = RuntimeError("measurement callback failed")
    policy_clock_error = RuntimeError("final policy clock failed")
    terminalized = [False]
    telemetry = InMemoryProviderTelemetry()

    class CommitThenFailUnknownLifecycle(UsageLedgerLifecycle):
        def mark_unknown(self, provider_call_id: str) -> None:
            super().mark_unknown(provider_call_id)
            terminalized[0] = True
            raise terminal_error

    def clock_now() -> datetime:
        if terminalized[0]:
            raise policy_clock_error
        return NOW

    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id="pc-caller-terminal-before-final-clock",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(minutes=5),
        resource_id="resource-1",
    )
    with pytest.raises(OSError) as caught:
        run_provider_call_with_usage(
            operation=lambda _ctx, _request: "ok",
            context=context,
            model="m",
            lifecycle=CommitThenFailUnknownLifecycle(ledger),
            measurement_extractor=lambda _value, _ctx, _failure: (_ for _ in ()).throw(
                measurement_error
            ),
            ownership_provider=lambda _ctx: ownership(),
            execution_kind="generation",
            execution_id="gen_terminal_before_final_clock",
            request_fingerprint="fp-terminal-before-final-clock",
            policy=RetryPolicy(synchronous_attempts=1),
            circuits=CircuitBreakerRegistry(threshold=1),
            telemetry=telemetry,
            now=clock_now,
        )
    assert caught.value is terminal_error
    assert len(telemetry.events) == 1
    assert telemetry.events[0].state == "succeeded"
    with engine.connect() as connection:
        row = connection.execute(select(provider_call_table)).mappings().one()
        usage_rows = connection.execute(select(usage_event_table)).mappings().all()
    assert row["status"] == "unknown"
    assert usage_rows == []


def test_pending_operation_error_precedes_failure_clock_exception() -> None:
    """已保存的原 operation 错误不得被 synthetic failure 后的 policy clock 覆盖。"""

    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    operation_error = RuntimeError("provider adapter original failure")
    policy_clock_error = RuntimeError("provider failure clock failed")
    operation_failed = [False]
    failure_clock_calls = [0]
    telemetry = InMemoryProviderTelemetry()

    def operation(_ctx: ProviderCallContext, _request):
        operation_failed[0] = True
        raise operation_error

    def clock_now() -> datetime:
        if operation_failed[0]:
            failure_clock_calls[0] += 1
            raise policy_clock_error
        return NOW

    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id="pc-caller-operation-before-failure-clock",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(minutes=5),
        resource_id="resource-1",
    )
    with pytest.raises(RuntimeError) as caught:
        run_provider_call_with_usage(
            operation=operation,
            context=context,
            model="m",
            lifecycle=UsageLedgerLifecycle(ledger),
            measurement_extractor=lambda _value, _ctx, _failure: measurement(),
            ownership_provider=lambda _ctx: ownership(),
            execution_kind="generation",
            execution_id="gen_operation_before_failure_clock",
            request_fingerprint="fp-operation-before-failure-clock",
            policy=RetryPolicy(synchronous_attempts=1),
            circuits=CircuitBreakerRegistry(threshold=1),
            telemetry=telemetry,
            now=clock_now,
        )
    assert caught.value is operation_error
    assert failure_clock_calls == [1]
    assert len(telemetry.events) == 1
    assert (telemetry.events[0].state, telemetry.events[0].error_class) == (
        "unknown",
        "provider_error",
    )
    with engine.connect() as connection:
        row = connection.execute(select(provider_call_table)).mappings().one()
        usage_rows = connection.execute(select(usage_event_table)).mappings().all()
    assert row["status"] == "unknown"
    assert usage_rows == []


def test_failure_accounting_abort_uses_post_provider_clock_for_circuit() -> None:
    """finalize 中断仍在 failure 后采时，冷却窗口从真实 outcome 时刻开始。"""

    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    current = [NOW]
    circuits = CircuitBreakerRegistry(threshold=1, open_seconds=60)
    telemetry = InMemoryProviderTelemetry()
    hook_error = RuntimeError("measurement failed after provider outcome")

    def operation(_ctx: ProviderCallContext, _request):
        current[0] += timedelta(seconds=120)
        raise ProviderFailure("upstream_503", status_code=503, retryable=True, sent=True)

    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id="pc-caller-accounting-clock",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(minutes=10),
        resource_id="resource-1",
    )
    with pytest.raises(RuntimeError) as caught:
        run_provider_call_with_usage(
            operation=operation,
            context=context,
            model="m",
            lifecycle=UsageLedgerLifecycle(ledger),
            measurement_extractor=lambda _value, _ctx, _failure: (_ for _ in ()).throw(hook_error),
            ownership_provider=lambda _ctx: ownership(),
            execution_kind="generation",
            execution_id="gen_accounting_clock",
            request_fingerprint="fp-accounting-clock",
            policy=RetryPolicy(synchronous_attempts=3),
            circuits=circuits,
            telemetry=telemetry,
            now=lambda: current[0],
        )
    assert caught.value is hook_error
    assert len(telemetry.events) == 1
    assert (
        telemetry.events[0].state,
        telemetry.events[0].error_class,
        telemetry.events[0].attempts,
    ) == ("failed", "upstream_503", 1)
    assert telemetry.events[0].elapsed_ms == 120_000
    with pytest.raises(CircuitOpen):
        call_with_policy(
            lambda _ctx, _request: "too soon",
            context,
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        )


@pytest.mark.parametrize("starting_state", ["closed", "half_open"])
def test_generic_operation_exception_forces_circuit_failure(starting_state: str) -> None:
    """普通 operation Exception 是已发送 provider failure，closed/half-open 均计一次。"""

    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    current = [NOW]
    circuits = CircuitBreakerRegistry(threshold=1, open_seconds=60)
    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id=f"pc-caller-generic-circuit-{starting_state}",
        attempt_id="attempt-root",
        deadline_utc=current[0] + timedelta(minutes=10),
        resource_id="resource-1",
    )
    if starting_state == "half_open":
        assert (
            call_with_policy(
                lambda _ctx, _request: (_ for _ in ()).throw(
                    ProviderFailure("seed_timeout", retryable=True)
                ),
                context,
                policy=RetryPolicy(synchronous_attempts=1),
                circuits=circuits,
                now=lambda: current[0],
            ).state
            == "failed"
        )
        current[0] += timedelta(seconds=61)
    operation_error = RuntimeError(f"generic operation failure from {starting_state}")
    telemetry = InMemoryProviderTelemetry()

    with pytest.raises(RuntimeError) as caught:
        run_provider_call_with_usage(
            operation=lambda _ctx, _request: (_ for _ in ()).throw(operation_error),
            context=context,
            model="m",
            lifecycle=UsageLedgerLifecycle(ledger),
            measurement_extractor=lambda _value, _ctx, _failure: measurement(),
            ownership_provider=lambda _ctx: ownership(),
            execution_kind="generation",
            execution_id=f"gen_generic_circuit_{starting_state}",
            request_fingerprint=f"fp-generic-circuit-{starting_state}",
            policy=RetryPolicy(synchronous_attempts=1),
            circuits=circuits,
            telemetry=telemetry,
            now=lambda: current[0],
        )
    assert caught.value is operation_error
    assert len(telemetry.events) == 1
    event = telemetry.events[0]
    assert (event.state, event.error_class, event.attempts) == (
        "unknown",
        "provider_error",
        1,
    )
    with engine.connect() as connection:
        row = connection.execute(select(provider_call_table)).mappings().one()
        usage_rows = connection.execute(select(usage_event_table)).mappings().all()
    assert row["status"] == "unknown"
    assert usage_rows == []
    with pytest.raises(CircuitOpen):
        call_with_policy(
            lambda _ctx, _request: "too soon",
            context,
            policy=RetryPolicy(synchronous_attempts=1),
            circuits=circuits,
            now=lambda: current[0],
        )


@pytest.mark.parametrize("phase", ["prepare", "dispatch"])
@pytest.mark.parametrize("invalid_result", [1, None], ids=["truthy", "falsey"])
def test_lifecycle_boolean_callbacks_reject_non_bool(
    phase: str,
    invalid_result,
) -> None:
    """prepare/dispatch 只接受严格 bool；非法返回必须 fail closed 且 circuit 中性。"""

    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    operation_calls: list[str] = []
    circuits = CircuitBreakerRegistry(threshold=1)
    telemetry = InMemoryProviderTelemetry()

    class InvalidBooleanLifecycle(UsageLedgerLifecycle):
        def prepare(self, **kwargs):
            outcome = super().prepare(**kwargs)
            return invalid_result if phase == "prepare" else outcome

        def mark_dispatching(self, provider_call_id, *, started_at_provider):
            outcome = super().mark_dispatching(
                provider_call_id,
                started_at_provider=started_at_provider,
            )
            return invalid_result if phase == "dispatch" else outcome

    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id=f"pc-caller-invalid-{phase}-{invalid_result is None}",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(minutes=5),
        resource_id="resource-1",
    )
    with pytest.raises(TypeError, match=rf"lifecycle {phase} must return bool"):
        run_provider_call_with_usage(
            operation=lambda ctx, _request: operation_calls.append(ctx.provider_call_id),
            context=context,
            model="m",
            lifecycle=InvalidBooleanLifecycle(ledger),
            measurement_extractor=lambda _value, _ctx, _failure: measurement(),
            ownership_provider=lambda _ctx: ownership(),
            execution_kind="generation",
            execution_id=f"gen_invalid_{phase}_{invalid_result is None}",
            request_fingerprint=f"fp-invalid-{phase}-{invalid_result is None}",
            policy=RetryPolicy(synchronous_attempts=1),
            circuits=circuits,
            telemetry=telemetry,
            now=lambda: NOW,
        )
    assert operation_calls == []
    assert telemetry.events == []
    with engine.connect() as connection:
        row = connection.execute(select(provider_call_table)).mappings().one()
        usage_rows = connection.execute(select(usage_event_table)).mappings().all()
    assert row["status"] == "not_sent"
    assert row["started_at_utc"] is None
    assert usage_rows == []
    assert (
        call_with_policy(
            lambda _ctx, _request: "ok",
            context,
            policy=RetryPolicy(synchronous_attempts=1),
            circuits=circuits,
            now=lambda: NOW,
        ).state
        == "succeeded"
    )


@pytest.mark.parametrize("existing_status", ["dispatching", "unknown"])
def test_invalid_prepare_return_does_not_rewrite_sent_replay(
    existing_status: str,
) -> None:
    """prepare 非 bool 的安全补偿只能改写 prepared，不得伪造 replay not_sent。"""

    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id=f"pc-caller-invalid-prepare-replay-{existing_status}",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(minutes=5),
        resource_id="resource-1",
    )
    call_id = _physical_provider_call_id(context.provider_call_id, 1)
    ledger.prepare_provider_call(
        provider="test-provider",
        model="m",
        operation="generate",
        execution_kind="generation",
        execution_id="gen_invalid_prepare_replay",
        provider_call_id=call_id,
        attempt_id="attempt-root",
        resource_id="resource-1",
        deadline_utc=NOW + timedelta(seconds=30),
        request_fingerprint="fp-invalid-prepare-replay",
    )
    assert ledger.mark_dispatching(call_id, started_at_provider=NOW) is True
    if existing_status == "unknown":
        ledger.mark_unknown(call_id)

    class InvalidReplayPrepareLifecycle(UsageLedgerLifecycle):
        def prepare(self, **kwargs):
            assert super().prepare(**kwargs) is False
            return 1

    operation_calls: list[str] = []
    telemetry = InMemoryProviderTelemetry()
    with pytest.raises(TypeError, match="lifecycle prepare must return bool"):
        run_provider_call_with_usage(
            operation=lambda ctx, _request: operation_calls.append(ctx.provider_call_id),
            context=context,
            model="m",
            lifecycle=InvalidReplayPrepareLifecycle(ledger),
            measurement_extractor=lambda _value, _ctx, _failure: measurement(),
            ownership_provider=lambda _ctx: ownership(),
            execution_kind="generation",
            execution_id="gen_invalid_prepare_replay",
            request_fingerprint="fp-invalid-prepare-replay",
            policy=RetryPolicy(synchronous_attempts=1),
            circuits=CircuitBreakerRegistry(threshold=1),
            telemetry=telemetry,
            now=lambda: NOW,
        )
    assert operation_calls == []
    assert telemetry.events == []
    with engine.connect() as connection:
        row = connection.execute(select(provider_call_table)).mappings().one()
        usage_rows = connection.execute(select(usage_event_table)).mappings().all()
    assert row["status"] == existing_status
    assert row["started_at_utc"] is not None
    assert usage_rows == []


def test_dispatch_state_conflict_after_commit_fails_closed() -> None:
    """本次 prepare 后 dispatch callback 即使提交后抛 state-conflict 也确定未发送。"""

    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    state_conflict = PlatformError(
        "provider_call_state_conflict",
        "dispatch callback failed after commit",
        {},
        409,
    )
    operation_calls: list[str] = []
    circuits = CircuitBreakerRegistry(threshold=1)
    telemetry = InMemoryProviderTelemetry()

    class CommitThenConflictLifecycle(UsageLedgerLifecycle):
        def mark_dispatching(self, provider_call_id, *, started_at_provider):
            assert super().mark_dispatching(
                provider_call_id,
                started_at_provider=started_at_provider,
            )
            raise state_conflict

    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id="pc-caller-dispatch-conflict-after-commit",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(minutes=5),
        resource_id="resource-1",
    )
    with pytest.raises(PlatformError) as caught:
        run_provider_call_with_usage(
            operation=lambda ctx, _request: operation_calls.append(ctx.provider_call_id),
            context=context,
            model="m",
            lifecycle=CommitThenConflictLifecycle(ledger),
            measurement_extractor=lambda _value, _ctx, _failure: measurement(),
            ownership_provider=lambda _ctx: ownership(),
            execution_kind="generation",
            execution_id="gen_dispatch_conflict_after_commit",
            request_fingerprint="fp-dispatch-conflict-after-commit",
            policy=RetryPolicy(synchronous_attempts=1),
            circuits=circuits,
            telemetry=telemetry,
            now=lambda: NOW,
        )
    assert caught.value is state_conflict
    assert operation_calls == []
    assert telemetry.events == []
    with engine.connect() as connection:
        row = connection.execute(select(provider_call_table)).mappings().one()
        usage_rows = connection.execute(select(usage_event_table)).mappings().all()
    assert row["status"] == "not_sent"
    assert row["started_at_utc"] is None
    assert usage_rows == []
    assert (
        call_with_policy(
            lambda _ctx, _request: "ok",
            context,
            policy=RetryPolicy(synchronous_attempts=1),
            circuits=circuits,
            now=lambda: NOW,
        ).state
        == "succeeded"
    )


@pytest.mark.parametrize("completion_state", ["uncommitted", "committed"])
def test_complete_callback_rejects_invalid_return_without_losing_committed_usage(
    completion_state: str,
) -> None:
    """complete 非空 str 合同 fail closed；已提交 completed+usage 不得回写 unknown。"""

    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    telemetry = InMemoryProviderTelemetry()

    class InvalidCompleteLifecycle(UsageLedgerLifecycle):
        def complete(self, **kwargs):
            if completion_state == "committed":
                super().complete(**kwargs)
            return None

    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id=f"pc-caller-invalid-complete-{completion_state}",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(minutes=5),
        resource_id="resource-1",
    )
    with pytest.raises(TypeError, match="lifecycle complete must return a non-empty str"):
        run_provider_call_with_usage(
            operation=lambda _ctx, _request: "ok",
            context=context,
            model="m",
            lifecycle=InvalidCompleteLifecycle(ledger),
            measurement_extractor=lambda _value, _ctx, _failure: measurement(),
            ownership_provider=lambda _ctx: ownership(),
            execution_kind="generation",
            execution_id=f"gen_invalid_complete_{completion_state}",
            request_fingerprint=f"fp-invalid-complete-{completion_state}",
            policy=RetryPolicy(synchronous_attempts=1),
            circuits=CircuitBreakerRegistry(threshold=1),
            telemetry=telemetry,
            now=lambda: NOW,
        )
    assert len(telemetry.events) == 1
    assert telemetry.events[0].state == "succeeded"
    with engine.connect() as connection:
        row = connection.execute(select(provider_call_table)).mappings().one()
        usage_rows = connection.execute(select(usage_event_table)).mappings().all()
    assert row["status"] == ("completed" if completion_state == "committed" else "unknown")
    assert len(usage_rows) == (1 if completion_state == "committed" else 0)


@pytest.mark.parametrize("callback_site", ["measurement", "ownership"])
def test_finalize_callbacks_reject_invalid_return(callback_site: str) -> None:
    """measurement/ownership 的非法正常返回必须以稳定合同错误终止为 unknown。"""

    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    invalid_value = object()
    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id=f"pc-caller-invalid-{callback_site}-return",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(minutes=5),
        resource_id="resource-1",
    )

    with pytest.raises(TypeError, match=rf"{callback_site} callback must return"):
        run_provider_call_with_usage(
            operation=lambda _ctx, _request: "ok",
            context=context,
            model="m",
            lifecycle=UsageLedgerLifecycle(ledger),
            measurement_extractor=(
                (lambda _value, _ctx, _failure: invalid_value)
                if callback_site == "measurement"
                else (lambda _value, _ctx, _failure: measurement())
            ),
            ownership_provider=(
                (lambda _ctx: invalid_value)
                if callback_site == "ownership"
                else (lambda _ctx: ownership())
            ),
            execution_kind="generation",
            execution_id=f"gen_invalid_{callback_site}_return",
            request_fingerprint=f"fp-invalid-{callback_site}-return",
            policy=RetryPolicy(synchronous_attempts=1),
            circuits=CircuitBreakerRegistry(threshold=1),
            now=lambda: NOW,
        )
    with engine.connect() as connection:
        row = connection.execute(select(provider_call_table)).mappings().one()
        usage_rows = connection.execute(select(usage_event_table)).mappings().all()
    assert row["status"] == "unknown"
    assert usage_rows == []


@pytest.mark.parametrize(
    "callback_site",
    ["mark_not_sent", "mark_unknown", "mark_unknown_if_unfinished"],
)
def test_terminal_lifecycle_callbacks_reject_invalid_return(callback_site: str) -> None:
    """终态 lifecycle callback 正常返回值必须严格为 None，且既有终态事实不丢失。"""

    engine, ledger = make_ledger()
    seed_price(engine, ledger)
    activate_clock_error = [False]
    clock_error = RuntimeError("final pre-send clock failed")
    operation_cancelled = asyncio.CancelledError("operation cancelled")
    complete_error = RuntimeError("complete failed after commit")

    class InvalidTerminalLifecycle(UsageLedgerLifecycle):
        def mark_dispatching(self, provider_call_id, *, started_at_provider):
            outcome = super().mark_dispatching(
                provider_call_id,
                started_at_provider=started_at_provider,
            )
            if callback_site == "mark_not_sent":
                activate_clock_error[0] = True
            return outcome

        def complete(self, **kwargs):
            event_id = super().complete(**kwargs)
            if callback_site == "mark_unknown_if_unfinished":
                raise complete_error
            return event_id

        def mark_not_sent(self, provider_call_id: str):
            super().mark_not_sent(provider_call_id)
            if callback_site == "mark_not_sent":
                return "invalid"
            return None

        def mark_unknown(self, provider_call_id: str):
            super().mark_unknown(provider_call_id)
            if callback_site == "mark_unknown":
                return "invalid"
            return None

        def mark_unknown_if_unfinished(self, provider_call_id: str):
            super().mark_unknown_if_unfinished(provider_call_id)
            if callback_site == "mark_unknown_if_unfinished":
                return "invalid"
            return None

    def clock_now() -> datetime:
        if activate_clock_error[0]:
            raise clock_error
        return NOW

    def operation(_ctx: ProviderCallContext, _request):
        if callback_site == "mark_unknown":
            raise operation_cancelled
        return "ok"

    context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id=f"pc-caller-invalid-terminal-{callback_site}",
        attempt_id="attempt-root",
        deadline_utc=NOW + timedelta(minutes=5),
        resource_id="resource-1",
    )
    with pytest.raises(TypeError, match="lifecycle terminal callback must return None"):
        run_provider_call_with_usage(
            operation=operation,
            context=context,
            model="m",
            lifecycle=InvalidTerminalLifecycle(ledger),
            measurement_extractor=lambda _value, _ctx, _failure: measurement(),
            ownership_provider=lambda _ctx: ownership(),
            execution_kind="generation",
            execution_id=f"gen_invalid_terminal_{callback_site}",
            request_fingerprint=f"fp-invalid-terminal-{callback_site}",
            policy=RetryPolicy(synchronous_attempts=1),
            circuits=CircuitBreakerRegistry(threshold=1),
            now=clock_now,
        )
    with engine.connect() as connection:
        row = connection.execute(select(provider_call_table)).mappings().one()
        usage_rows = connection.execute(select(usage_event_table)).mappings().all()
    expected_status = {
        "mark_not_sent": "not_sent",
        "mark_unknown": "unknown",
        "mark_unknown_if_unfinished": "completed",
    }[callback_site]
    assert row["status"] == expected_status
    assert len(usage_rows) == (1 if callback_site == "mark_unknown_if_unfinished" else 0)
