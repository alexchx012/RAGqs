from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta

import pytest

from app.platform.ports import ProviderPort
from app.platform.provider import (
    CircuitBreakerRegistry,
    CircuitOpen,
    InMemoryProviderTelemetry,
    ProviderCallContext,
    ProviderFailure,
    ProviderPreSendDeadlineExceeded,
    ProviderResult,
    RetryPolicy,
    call_with_policy,
)


def context(
    seconds: int = 60,
    *,
    now_utc: datetime | None = None,
) -> ProviderCallContext:
    now = now_utc or datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    return ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id="pc-1",
        attempt_id="attempt-root",
        deadline_utc=now + timedelta(seconds=seconds),
        resource_id="resource-1",
    )


def fixed_now() -> datetime:
    return datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


def test_public_transport_contract_preserves_raw_success_value() -> None:
    sentinel = object()

    class RawTransport:
        def call(self, call_context: ProviderCallContext, request: object) -> object:
            del call_context, request
            return sentinel

    transport: ProviderPort = RawTransport()
    result = call_with_policy(
        transport.call,
        context(),
        circuits=CircuitBreakerRegistry(),
        policy=RetryPolicy(synchronous_attempts=1),
        now=fixed_now,
    )

    assert result.state == "succeeded"
    assert result.value is sentinel


@pytest.mark.parametrize("starting_state", ["closed", "half_open"])
def test_transport_returning_policy_result_is_rejected_without_false_success(
    starting_state: str,
) -> None:
    current = [fixed_now()]
    circuits = CircuitBreakerRegistry(threshold=1, open_seconds=60)
    if starting_state == "half_open":
        seeded = call_with_policy(
            lambda _call_context, _request: (_ for _ in ()).throw(
                ProviderFailure("timeout", retryable=True)
            ),
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        )
        assert seeded.state == "failed"
        current[0] += timedelta(seconds=61)

    class InvalidEnvelopeTransport:
        def call(self, call_context: ProviderCallContext, request: object) -> ProviderResult:
            del call_context, request
            return ProviderResult(state="failed", error_class="upstream_503")

    transport: ProviderPort = InvalidEnvelopeTransport()
    telemetry = InMemoryProviderTelemetry()
    with pytest.raises(
        TypeError,
        match="provider transport must return a raw value, not ProviderResult",
    ):
        call_with_policy(
            transport.call,
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            telemetry=telemetry,
            now=lambda: current[0],
        )

    assert telemetry.events == []
    recovered = call_with_policy(
        lambda _call_context, _request: "ok",
        context(now_utc=current[0]),
        circuits=circuits,
        policy=RetryPolicy(synchronous_attempts=1),
        now=lambda: current[0],
    )
    assert recovered.state == "succeeded"


@pytest.mark.parametrize("starting_state", ["closed", "half_open"])
def test_invalid_policy_result_preserves_prior_provider_failure_accounting(
    starting_state: str,
) -> None:
    current = [fixed_now()]
    circuits = CircuitBreakerRegistry(threshold=1, open_seconds=60)
    if starting_state == "half_open":
        seeded = call_with_policy(
            lambda _call_context, _request: (_ for _ in ()).throw(
                ProviderFailure("timeout", retryable=True)
            ),
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        )
        assert seeded.state == "failed"
        current[0] += timedelta(seconds=61)

    outcomes: list[ProviderFailure | ProviderResult] = [
        ProviderFailure(
            "upstream_503",
            status_code=503,
            retryable=True,
            sent=True,
        ),
        ProviderResult(state="failed", error_class="invalid_nested_result"),
    ]

    def invalid_after_failure(
        call_context: ProviderCallContext,
        request: object,
    ) -> object:
        del call_context, request
        outcome = outcomes.pop(0)
        if isinstance(outcome, ProviderFailure):
            raise outcome
        return outcome

    telemetry = InMemoryProviderTelemetry()
    with pytest.raises(
        TypeError,
        match="provider transport must return a raw value, not ProviderResult",
    ):
        call_with_policy(
            invalid_after_failure,
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=2),
            telemetry=telemetry,
            now=lambda: current[0],
            sleep=lambda _delay: None,
            jitter=lambda _delay: 0,
        )

    assert outcomes == []
    assert len(telemetry.events) == 1
    assert (
        telemetry.events[0].state,
        telemetry.events[0].error_class,
        telemetry.events[0].attempts,
    ) == ("failed", "upstream_503", 1)
    with pytest.raises(CircuitOpen):
        call_with_policy(
            lambda _call_context, _request: "must not send",
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        )


def test_idempotency_key_is_unambiguous_for_delimiter_bearing_fields() -> None:
    left = ProviderCallContext(
        provider="test-provider",
        operation="a:b",
        provider_call_id="pc-collision",
        attempt_id="c",
        deadline_utc=fixed_now() + timedelta(minutes=1),
        resource_id="d",
    )
    right = ProviderCallContext(
        provider="test-provider",
        operation="a",
        provider_call_id="pc-collision",
        attempt_id="b:c",
        deadline_utc=fixed_now() + timedelta(minutes=1),
        resource_id="d",
    )

    assert left.idempotency_key != right.idempotency_key
    assert len(left.idempotency_key) == len(right.idempotency_key) == 64
    int(left.idempotency_key, 16)
    int(right.idempotency_key, 16)


def test_idempotency_key_distinguishes_null_from_literal_none() -> None:
    absent = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id="pc-none-collision",
        attempt_id="attempt-none",
        deadline_utc=fixed_now() + timedelta(minutes=1),
        resource_id=None,
    )
    literal = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id="pc-none-collision",
        attempt_id="attempt-none",
        deadline_utc=fixed_now() + timedelta(minutes=1),
        resource_id="none",
    )

    assert absent.idempotency_key != literal.idempotency_key
    assert len(absent.idempotency_key) == len(literal.idempotency_key) == 64


def test_physical_provider_call_ids_are_bounded_distinct_and_deterministic() -> None:
    root_id = "r" * 64
    call_context = ProviderCallContext(
        provider="test-provider",
        operation="generate",
        provider_call_id=root_id,
        attempt_id="attempt-bounded",
        deadline_utc=fixed_now() + timedelta(minutes=1),
    )

    def collect_ids() -> list[str]:
        call_ids: list[str] = []
        outcomes: list[str | ProviderFailure] = [
            ProviderFailure("timeout", retryable=True),
            ProviderFailure("timeout", retryable=True),
            "ok",
        ]
        outcome_iterator = iter(outcomes)

        def operation(call: ProviderCallContext, request: object) -> str:
            call_ids.append(call.provider_call_id)
            outcome = next(outcome_iterator)
            if isinstance(outcome, ProviderFailure):
                raise outcome
            return outcome

        result = call_with_policy(
            operation,
            call_context,
            circuits=CircuitBreakerRegistry(),
            now=fixed_now,
            sleep=lambda _delay: None,
            jitter=lambda _delay: 0,
        )
        assert result.state == "succeeded"
        return call_ids

    first = collect_ids()
    second = collect_ids()
    assert first == second
    assert len(first) == len(set(first)) == 3
    assert all(call_id.startswith("pc_") and len(call_id) == 64 for call_id in first)


def test_retryable_failures_use_new_call_ids_and_stable_attempt_identity() -> None:
    attempts: list[ProviderCallContext] = []
    outcomes: list[str | ProviderFailure] = [
        ProviderFailure("connection_lost", retryable=True, sent=True),
        ProviderFailure("upstream_503", status_code=503, retryable=True),
        "ok",
    ]
    outcome_iterator = iter(outcomes)
    current = [datetime(2026, 8, 5, 12, 0, tzinfo=UTC)]

    def operation(call: ProviderCallContext, request: object) -> str:
        attempts.append(call)
        outcome = next(outcome_iterator)
        if isinstance(outcome, ProviderFailure):
            raise outcome
        return outcome

    result = call_with_policy(
        operation,
        context(),
        now=lambda: current[0],
        sleep=lambda delay: current.__setitem__(0, current[0] + timedelta(seconds=delay)),
        jitter=lambda _delay: 0,
    )

    assert isinstance(result, ProviderResult)
    assert result.state == "succeeded"
    assert result.value == "ok"
    assert result.attempts == 3
    call_ids = [call.provider_call_id for call in attempts]
    assert len(call_ids) == len(set(call_ids)) == 3
    assert all(call_id.startswith("pc_") and len(call_id) == 64 for call_id in call_ids)
    assert {call.attempt_id for call in attempts} == {"attempt-root"}
    idempotency_keys = {call.idempotency_key for call in attempts}
    assert len(idempotency_keys) == 1
    assert len(idempotency_keys.pop()) == 64


def test_deterministic_4xx_is_not_retried() -> None:
    attempts = []

    def operation(call: ProviderCallContext, request: object) -> None:
        attempts.append(call)
        raise ProviderFailure("invalid_request", status_code=400, retryable=False)

    result = call_with_policy(
        operation,
        context(),
        now=fixed_now,
        sleep=lambda _delay: None,
    )

    assert result.state == "failed"
    assert result.attempts == 1
    assert len(attempts) == 1


def test_deterministic_4xx_cannot_be_overridden_to_retry() -> None:
    attempts = []

    def operation(call: ProviderCallContext, request: object) -> None:
        attempts.append(call)
        raise ProviderFailure("invalid_request", status_code=400, retryable=True)

    result = call_with_policy(
        operation,
        context(),
        now=fixed_now,
        sleep=lambda _delay: None,
    )

    assert result.state == "failed"
    assert result.attempts == 1
    assert len(attempts) == 1


@pytest.mark.parametrize("status_code", [429, 502, 503, 504])
def test_retryable_http_status_cannot_be_overridden_to_non_retryable(
    status_code: int,
) -> None:
    attempts = 0

    def operation(call: ProviderCallContext, request: object) -> None:
        nonlocal attempts
        del call, request
        attempts += 1
        raise ProviderFailure(
            f"upstream_{status_code}",
            status_code=status_code,
            retryable=False,
        )

    result = call_with_policy(
        operation,
        context(),
        circuits=CircuitBreakerRegistry(),
        now=fixed_now,
        sleep=lambda _delay: None,
        jitter=lambda _delay: 0,
    )

    assert result.retryable is True
    assert result.attempts == attempts == 3


@pytest.mark.parametrize("error_class", ["timeout", "connection_lost", "network_error"])
def test_retryable_network_class_cannot_be_overridden_to_non_retryable(
    error_class: str,
) -> None:
    attempts = 0

    def operation(call: ProviderCallContext, request: object) -> None:
        nonlocal attempts
        del call, request
        attempts += 1
        raise ProviderFailure(error_class, retryable=False)

    result = call_with_policy(
        operation,
        context(),
        circuits=CircuitBreakerRegistry(),
        now=fixed_now,
        sleep=lambda _delay: None,
        jitter=lambda _delay: 0,
    )

    assert result.retryable is True
    assert result.attempts == attempts == 3


@pytest.mark.parametrize(
    ("error_class", "status_code"),
    [("upstream_500", 500), ("provider_busy", None)],
)
def test_unlisted_failure_cannot_be_overridden_to_retryable(
    error_class: str,
    status_code: int | None,
) -> None:
    attempts = 0

    def operation(call: ProviderCallContext, request: object) -> None:
        nonlocal attempts
        del call, request
        attempts += 1
        raise ProviderFailure(error_class, status_code=status_code, retryable=True)

    result = call_with_policy(
        operation,
        context(),
        circuits=CircuitBreakerRegistry(),
        now=fixed_now,
        sleep=lambda _delay: None,
        jitter=lambda _delay: 0,
    )

    assert result.retryable is False
    assert result.attempts == attempts == 1


@pytest.mark.parametrize(("asynchronous", "hard_limit"), [(False, 3), (True, 5)])
def test_configured_attempt_budget_cannot_exceed_hard_contract_limit(
    asynchronous: bool,
    hard_limit: int,
) -> None:
    attempts = 0

    def operation(call: ProviderCallContext, request: object) -> None:
        nonlocal attempts
        del call, request
        attempts += 1
        raise ProviderFailure("timeout")

    result = call_with_policy(
        operation,
        context(),
        asynchronous=asynchronous,
        policy=RetryPolicy(synchronous_attempts=20, asynchronous_attempts=20),
        circuits=CircuitBreakerRegistry(),
        now=fixed_now,
        sleep=lambda _delay: None,
        jitter=lambda _delay: 0,
    )

    assert result.retryable is True
    assert result.attempts == attempts == hard_limit


def test_sent_but_unknown_failure_returns_unknown_after_retry_budget() -> None:
    def operation(call: ProviderCallContext, request: object) -> None:
        raise ProviderFailure("connection_lost", retryable=True, sent=True)

    result = call_with_policy(
        operation,
        context(),
        now=fixed_now,
        sleep=lambda _delay: None,
        jitter=lambda _delay: 0,
    )

    assert result.state == "unknown"
    assert result.attempts == 3


def test_known_sent_failures_are_failed_after_retry_budget() -> None:
    """每次发送都有确定 status_code 时，重试耗尽仍是 failed 而非 unknown。"""

    def operation(call: ProviderCallContext, request: object) -> None:
        raise ProviderFailure("upstream_503", status_code=503, retryable=True, sent=True)

    result = call_with_policy(
        operation,
        context(),
        circuits=CircuitBreakerRegistry(),
        now=fixed_now,
        sleep=lambda _delay: None,
        jitter=lambda _delay: 0,
    )

    assert result.state == "failed"
    assert result.attempts == 3
    assert result.retryable is True


def test_non_retryable_unconfirmed_sent_failure_is_unknown() -> None:
    """已发送但无 status_code 的非重试失败也必须暴露 unknown 事实语义。"""

    def operation(call: ProviderCallContext, request: object) -> None:
        raise ProviderFailure("provider_error", retryable=False, sent=True)

    result = call_with_policy(operation, context(), now=fixed_now)

    assert result.state == "unknown"
    assert result.attempts == 1
    assert result.retryable is False


def test_circuit_opens_after_five_retryable_failures_and_recovers() -> None:
    calls = [0]

    def operation(call: ProviderCallContext, request: object) -> None:
        calls[0] += 1
        raise ProviderFailure("timeout", retryable=True)

    circuits = CircuitBreakerRegistry()
    for _ in range(5):
        result = call_with_policy(
            operation,
            context(),
            circuits=circuits,
            now=fixed_now,
            sleep=lambda _delay: None,
            jitter=lambda _delay: 0,
        )
        assert result.state == "failed"

    with pytest.raises(CircuitOpen):
        call_with_policy(
            operation,
            context(),
            circuits=circuits,
            now=fixed_now,
            sleep=lambda _delay: None,
            jitter=lambda _delay: 0,
        )
    assert calls[0] == 15


def test_deadline_stops_before_sending_next_attempt() -> None:
    now = [datetime(2026, 8, 5, 12, 0, tzinfo=UTC)]
    sent = [0]

    def operation(call: ProviderCallContext, request: object) -> None:
        sent[0] += 1
        raise ProviderFailure("timeout", retryable=True)

    result = call_with_policy(
        operation,
        context(seconds=1),
        now=lambda: now[0],
        sleep=lambda delay: now.__setitem__(0, now[0] + timedelta(seconds=delay)),
        jitter=lambda _delay: 0,
    )

    assert result.attempts == 2
    assert sent[0] == 2


def test_backoff_does_not_sleep_past_the_absolute_deadline() -> None:
    now = [datetime(2026, 8, 5, 12, 0, tzinfo=UTC)]
    sleeps: list[float] = []

    def operation(call: ProviderCallContext, request: object) -> None:
        raise ProviderFailure("timeout", retryable=True)

    def advance(delay: float) -> None:
        sleeps.append(delay)
        now[0] += timedelta(seconds=delay)

    result = call_with_policy(
        operation,
        context(seconds=1),
        now=lambda: now[0],
        sleep=advance,
        jitter=lambda _delay: 0,
    )

    assert result.state == "failed"
    assert result.attempts == 2
    assert sleeps == [0.25]
    assert now[0] == datetime(2026, 8, 5, 12, 0, 0, 250000, tzinfo=UTC)


def test_attempt_context_deadline_is_capped_at_thirty_seconds() -> None:
    current = [fixed_now()]
    total_context = context(seconds=120, now_utc=current[0])
    attempt_deadlines: list[datetime] = []

    def operation(call: ProviderCallContext, request: object) -> str:
        del request
        attempt_deadlines.append(call.deadline_utc)
        return "ok"

    result = call_with_policy(
        operation,
        total_context,
        circuits=CircuitBreakerRegistry(),
        policy=RetryPolicy(synchronous_attempts=1),
        now=lambda: current[0],
    )

    assert result.state == "succeeded"
    assert attempt_deadlines == [current[0] + timedelta(seconds=30)]
    assert total_context.deadline_utc == current[0] + timedelta(seconds=120)


def test_attempt_context_deadline_uses_shorter_absolute_deadline() -> None:
    current = [fixed_now()]
    total_context = context(seconds=10, now_utc=current[0])
    attempt_deadlines: list[datetime] = []

    def operation(call: ProviderCallContext, request: object) -> str:
        del request
        attempt_deadlines.append(call.deadline_utc)
        return "ok"

    result = call_with_policy(
        operation,
        total_context,
        circuits=CircuitBreakerRegistry(),
        policy=RetryPolicy(synchronous_attempts=1),
        now=lambda: current[0],
    )

    assert result.state == "succeeded"
    assert attempt_deadlines == [total_context.deadline_utc]


def test_retries_receive_fresh_capped_attempt_deadlines() -> None:
    current = [fixed_now()]
    deadlines: list[datetime] = []
    calls = 0

    def operation(call: ProviderCallContext, request: object) -> str:
        nonlocal calls
        del request
        deadlines.append(call.deadline_utc)
        calls += 1
        if calls == 1:
            current[0] += timedelta(seconds=2)
            raise ProviderFailure("timeout", retryable=True)
        if calls == 2:
            current[0] += timedelta(seconds=4)
            raise ProviderFailure("timeout", retryable=True)
        return "ok"

    result = call_with_policy(
        operation,
        context(seconds=120, now_utc=current[0]),
        circuits=CircuitBreakerRegistry(),
        now=lambda: current[0],
        sleep=lambda delay: current.__setitem__(0, current[0] + timedelta(seconds=delay)),
        jitter=lambda _delay: 0,
    )

    assert result.state == "succeeded"
    assert deadlines == [
        fixed_now() + timedelta(seconds=30),
        fixed_now() + timedelta(seconds=32.25),
        fixed_now() + timedelta(seconds=37.25),
    ]


def test_late_attempt_success_is_rejected_and_opens_circuit() -> None:
    current = [fixed_now()]
    circuits = CircuitBreakerRegistry(threshold=1, open_seconds=60)
    telemetry = InMemoryProviderTelemetry()
    sent = 0

    def late_success(call: ProviderCallContext, request: object) -> str:
        nonlocal sent
        del request
        sent += 1
        assert call.deadline_utc == fixed_now() + timedelta(seconds=30)
        current[0] += timedelta(seconds=31)
        return "late"

    result = call_with_policy(
        late_success,
        context(seconds=120, now_utc=current[0]),
        circuits=circuits,
        telemetry=telemetry,
        policy=RetryPolicy(synchronous_attempts=1),
        now=lambda: current[0],
    )

    assert (result.state, result.error_class, result.attempts) == (
        "unknown",
        "deadline_exceeded",
        1,
    )
    assert sent == 1
    assert len(telemetry.events) == 1
    assert (
        telemetry.events[0].state,
        telemetry.events[0].error_class,
        telemetry.events[0].attempts,
    ) == ("unknown", "deadline_exceeded", 1)
    with pytest.raises(CircuitOpen):
        call_with_policy(
            lambda call, request: "must not send",
            context(seconds=120, now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        )


def test_expired_half_open_probe_does_not_permanently_lock_the_circuit() -> None:
    current = [datetime(2026, 8, 5, 12, 0, tzinfo=UTC)]
    circuits = CircuitBreakerRegistry(threshold=1, open_seconds=60)

    def unavailable(call: ProviderCallContext, request: object) -> None:
        raise ProviderFailure("timeout", retryable=True)

    assert (
        call_with_policy(
            unavailable,
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        ).state
        == "failed"
    )
    current[0] += timedelta(seconds=60)

    expired = call_with_policy(
        unavailable,
        context(seconds=-1, now_utc=current[0]),
        circuits=circuits,
        policy=RetryPolicy(synchronous_attempts=1),
        now=lambda: current[0],
    )
    assert expired.error_class == "deadline_exceeded"

    # The local deadline result did not send, so it must release the half-open
    # probe without reopening the circuit for another recovery interval.
    recovered = call_with_policy(
        lambda call, request: "ok",
        context(now_utc=current[0]),
        circuits=circuits,
        policy=RetryPolicy(synchronous_attempts=1),
        now=lambda: current[0],
    )

    assert recovered.state == "succeeded"


def test_half_open_retry_failure_before_deadline_reopens_circuit() -> None:
    """A real half-open attempt remains a circuit failure when retry sleep crosses deadline."""
    current = [datetime(2026, 8, 5, 12, 0, tzinfo=UTC)]
    circuits = CircuitBreakerRegistry(threshold=1, open_seconds=60)

    def unavailable(call: ProviderCallContext, request: object) -> None:
        raise ProviderFailure("timeout", retryable=True)

    assert (
        call_with_policy(
            unavailable,
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        ).state
        == "failed"
    )
    current[0] += timedelta(seconds=60)

    failed_probe = call_with_policy(
        unavailable,
        context(seconds=1, now_utc=current[0]),
        circuits=circuits,
        now=lambda: current[0],
        sleep=lambda _delay: current.__setitem__(0, current[0] + timedelta(seconds=2)),
        jitter=lambda _delay: 0,
    )
    assert (failed_probe.state, failed_probe.attempts) == ("failed", 1)

    with pytest.raises(CircuitOpen):
        call_with_policy(
            lambda call, request: "too-soon",
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        )

    current[0] += timedelta(seconds=60)
    recovered = call_with_policy(
        lambda call, request: "ok",
        context(now_utc=current[0]),
        circuits=circuits,
        policy=RetryPolicy(synchronous_attempts=1),
        now=lambda: current[0],
    )
    assert recovered.state == "succeeded"


@pytest.mark.parametrize("starting_state", ["closed", "half_open"])
def test_retry_sleep_clock_jump_records_one_failure_and_opens_circuit(
    starting_state: str,
) -> None:
    """A real retryable failure is recorded once when sleep jumps past deadline."""

    class CountingCircuits(CircuitBreakerRegistry):
        def __init__(self) -> None:
            super().__init__(threshold=1, open_seconds=60)
            self.failure_times: list[datetime] = []

        def failure(self, provider: str, operation: str, failed_at: datetime) -> None:
            self.failure_times.append(failed_at)
            super().failure(provider, operation, failed_at)

    current = [datetime(2026, 8, 5, 12, 0, tzinfo=UTC)]
    circuits = CountingCircuits()

    def unavailable(call: ProviderCallContext, request: object) -> None:
        raise ProviderFailure("timeout", retryable=True)

    if starting_state == "half_open":
        assert (
            call_with_policy(
                unavailable,
                context(now_utc=current[0]),
                circuits=circuits,
                policy=RetryPolicy(synchronous_attempts=1),
                now=lambda: current[0],
            ).state
            == "failed"
        )
        current[0] += timedelta(seconds=60)
        circuits.failure_times.clear()

    result = call_with_policy(
        unavailable,
        context(seconds=1, now_utc=current[0]),
        circuits=circuits,
        now=lambda: current[0],
        sleep=lambda _delay: current.__setitem__(0, current[0] + timedelta(seconds=2)),
        jitter=lambda _delay: 0,
    )

    assert (result.state, result.error_class, result.attempts) == (
        "failed",
        "deadline_exceeded",
        1,
    )
    assert circuits.failure_times == [current[0]]
    with pytest.raises(CircuitOpen):
        call_with_policy(
            lambda call, request: "too-soon",
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        )


@pytest.mark.parametrize("starting_state", ["closed", "half_open"])
def test_retry_cancellation_records_prior_failure_once_and_opens_circuit(
    starting_state: str,
) -> None:
    """Later cancellation is neutral itself but cannot erase an earlier real failure."""

    class CountingCircuits(CircuitBreakerRegistry):
        def __init__(self) -> None:
            super().__init__(threshold=1, open_seconds=60)
            self.failure_times: list[datetime] = []

        def failure(self, provider: str, operation: str, failed_at: datetime) -> None:
            self.failure_times.append(failed_at)
            super().failure(provider, operation, failed_at)

    current = [datetime(2026, 8, 5, 12, 0, tzinfo=UTC)]
    circuits = CountingCircuits()

    def unavailable(call: ProviderCallContext, request: object) -> None:
        del call, request
        raise ProviderFailure("timeout", retryable=True)

    if starting_state == "half_open":
        assert (
            call_with_policy(
                unavailable,
                context(now_utc=current[0]),
                circuits=circuits,
                policy=RetryPolicy(synchronous_attempts=1),
                now=lambda: current[0],
            ).state
            == "failed"
        )
        current[0] += timedelta(seconds=60)
        circuits.failure_times.clear()

    cancelled = asyncio.CancelledError("retry cancelled")
    calls = 0

    def fail_then_cancel(call: ProviderCallContext, request: object) -> None:
        nonlocal calls
        del call, request
        calls += 1
        if calls == 1:
            raise ProviderFailure("timeout", retryable=True)
        raise cancelled

    with pytest.raises(asyncio.CancelledError) as failure:
        call_with_policy(
            fail_then_cancel,
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=2),
            now=lambda: current[0],
            sleep=lambda _delay: None,
            jitter=lambda _delay: 0,
        )
    assert failure.value is cancelled
    assert calls == 2
    assert circuits.failure_times == [current[0]]
    with pytest.raises(CircuitOpen):
        call_with_policy(
            lambda call, request: "too-soon",
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        )


def test_late_provider_success_is_rejected_at_the_absolute_deadline() -> None:
    current = [datetime(2026, 8, 5, 12, 0, tzinfo=UTC)]

    def late_success(call: ProviderCallContext, request: object) -> str:
        current[0] += timedelta(seconds=2)
        return "late"

    result = call_with_policy(
        late_success,
        context(seconds=1, now_utc=current[0]),
        now=lambda: current[0],
    )

    assert result.state == "unknown"
    assert result.error_class == "deadline_exceeded"
    assert result.attempts == 1


def test_pre_send_deadline_is_not_an_attempt_or_circuit_failure() -> None:
    current = [datetime(2026, 8, 5, 12, 0, tzinfo=UTC)]
    circuits = CircuitBreakerRegistry(threshold=1, open_seconds=60)

    failed = call_with_policy(
        lambda call, request: (_ for _ in ()).throw(ProviderFailure("timeout", retryable=True)),
        context(now_utc=current[0]),
        circuits=circuits,
        policy=RetryPolicy(synchronous_attempts=1),
        now=lambda: current[0],
    )
    assert failed.state == "failed"
    current[0] += timedelta(seconds=60)
    telemetry = InMemoryProviderTelemetry()

    def expire_before_send(call: ProviderCallContext, request: object) -> None:
        raise ProviderPreSendDeadlineExceeded

    result = call_with_policy(
        expire_before_send,
        context(now_utc=current[0]),
        circuits=circuits,
        telemetry=telemetry,
        policy=RetryPolicy(synchronous_attempts=1),
        now=lambda: current[0],
    )

    assert (result.state, result.error_class, result.attempts, result.retryable) == (
        "not_sent",
        "deadline_exceeded",
        0,
        True,
    )
    assert len(telemetry.events) == 1
    event = telemetry.events[0]
    assert (event.state, event.error_class, event.attempts, event.retry_count) == (
        "not_sent",
        "deadline_exceeded",
        0,
        0,
    )
    recovered = call_with_policy(
        lambda call, request: "ok",
        context(now_utc=current[0]),
        circuits=circuits,
        policy=RetryPolicy(synchronous_attempts=1),
        now=lambda: current[0],
    )
    assert recovered.state == "succeeded"


def test_provider_policy_emits_structured_metric_and_log_fields() -> None:
    telemetry = InMemoryProviderTelemetry()
    circuits = CircuitBreakerRegistry(threshold=1)

    def unavailable(call: ProviderCallContext, request: object) -> None:
        raise ProviderFailure("timeout", retryable=True)

    result = call_with_policy(
        unavailable,
        context(),
        circuits=circuits,
        telemetry=telemetry,
        policy=RetryPolicy(synchronous_attempts=1),
        now=fixed_now,
    )

    assert result.state == "failed"
    assert len(telemetry.events) == 1
    event = telemetry.events[0]
    assert event.provider == "test-provider"
    assert event.operation == "generate"
    assert event.error_class == "timeout"
    assert event.attempts == 1
    assert event.retry_count == 0
    assert event.elapsed_ms >= 0
    assert event.recovery_after_seconds == 60


class _CountingCircuits(CircuitBreakerRegistry):
    def __init__(self) -> None:
        super().__init__(threshold=1, open_seconds=60)
        self.failure_times: list[datetime] = []

    def failure(self, provider: str, operation: str, failed_at: datetime) -> None:
        self.failure_times.append(failed_at)
        super().failure(provider, operation, failed_at)


def _open_half_open_probe(circuits: _CountingCircuits, current: list[datetime]) -> None:
    failed = call_with_policy(
        lambda _ctx, _request: (_ for _ in ()).throw(ProviderFailure("timeout", retryable=True)),
        context(now_utc=current[0]),
        circuits=circuits,
        policy=RetryPolicy(synchronous_attempts=1),
        now=lambda: current[0],
    )
    assert failed.state == "failed"
    current[0] += timedelta(seconds=61)
    circuits.failure_times.clear()


@pytest.mark.parametrize("starting_state", ["closed", "half_open"])
@pytest.mark.parametrize("failure_kind", ["exception", "cancellation"])
@pytest.mark.parametrize("persistent", [False, True])
def test_loop_clock_failure_before_first_attempt_is_circuit_neutral(
    starting_state: str,
    failure_kind: str,
    persistent: bool,
) -> None:
    """allow 后 zero-attempt clock 故障原样传播，且 half-open 无误记/泄漏。"""

    current = [fixed_now()]
    circuits = _CountingCircuits()
    if starting_state == "half_open":
        _open_half_open_probe(circuits, current)
    injected: BaseException
    if failure_kind == "cancellation":
        injected = asyncio.CancelledError("loop clock cancelled")
    else:
        injected = RuntimeError("loop clock failed")
    enabled = [True]
    clock_calls = [0]
    operation_calls = [0]
    telemetry = InMemoryProviderTelemetry()

    def unstable_now() -> datetime:
        clock_calls[0] += 1
        if enabled[0] and clock_calls[0] >= 2 and (persistent or clock_calls[0] == 2):
            raise injected
        return current[0]

    def operation(_ctx: ProviderCallContext, _request: object) -> str:
        operation_calls[0] += 1
        return "unexpected"

    with pytest.raises(type(injected)) as caught:
        call_with_policy(
            operation,
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            telemetry=telemetry,
            now=unstable_now,
        )
    assert caught.value is injected
    assert operation_calls == [0]
    assert circuits.failure_times == []
    assert telemetry.events == []

    enabled[0] = False
    recovered = call_with_policy(
        lambda _ctx, _request: "ok",
        context(now_utc=current[0]),
        circuits=circuits,
        policy=RetryPolicy(synchronous_attempts=1),
        now=lambda: current[0],
    )
    assert recovered.state == "succeeded"


@pytest.mark.parametrize("starting_state", ["closed", "half_open"])
@pytest.mark.parametrize("callback_site", ["now", "delay", "jitter", "sleep"])
@pytest.mark.parametrize("failure_kind", ["exception", "cancellation"])
def test_retry_callback_failure_preserves_provider_failure_accounting(
    starting_state: str,
    callback_site: str,
    failure_kind: str,
) -> None:
    """真实 retryable failure 后的本地 callback 故障不能擦除 circuit/telemetry。"""

    current = [fixed_now()]
    circuits = _CountingCircuits()
    if starting_state == "half_open":
        _open_half_open_probe(circuits, current)
    injected: BaseException
    if failure_kind == "cancellation":
        injected = asyncio.CancelledError(f"{callback_site} cancelled")
    else:
        injected = RuntimeError(f"{callback_site} failed")
    provider_failed = [False]
    operation_calls = [0]
    telemetry = InMemoryProviderTelemetry()

    class UnstableDelayPolicy(RetryPolicy):
        def delay(self, attempt_number: int) -> float:
            if callback_site == "delay":
                raise injected
            return super().delay(attempt_number)

    def unstable_now() -> datetime:
        if callback_site == "now" and provider_failed[0]:
            raise injected
        return current[0]

    def unstable_jitter(_delay: float) -> float:
        if callback_site == "jitter":
            raise injected
        return 0

    def unstable_sleep(_delay: float) -> None:
        if callback_site == "sleep":
            raise injected

    def unavailable(_ctx: ProviderCallContext, _request: object) -> None:
        operation_calls[0] += 1
        provider_failed[0] = True
        raise ProviderFailure("upstream_503", status_code=503, retryable=True, sent=True)

    with pytest.raises(type(injected)) as caught:
        call_with_policy(
            unavailable,
            context(now_utc=current[0]),
            circuits=circuits,
            policy=UnstableDelayPolicy(synchronous_attempts=2),
            telemetry=telemetry,
            now=unstable_now,
            jitter=unstable_jitter,
            sleep=unstable_sleep,
        )
    assert caught.value is injected
    assert operation_calls == [1]
    assert circuits.failure_times == [current[0]]
    assert len(telemetry.events) == 1
    event = telemetry.events[0]
    assert (event.state, event.error_class, event.attempts, event.retry_count) == (
        "failed",
        "upstream_503",
        1,
        0,
    )
    with pytest.raises(CircuitOpen):
        call_with_policy(
            lambda _ctx, _request: "too soon",
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        )


@pytest.mark.parametrize("starting_state", ["closed", "half_open"])
@pytest.mark.parametrize("persistent", [False, True])
def test_final_success_clock_exception_records_success_and_releases_probe(
    starting_state: str,
    persistent: bool,
) -> None:
    """operation success 后 final now 普通异常保留 success accounting，绝不误记 failure。"""

    current = [fixed_now()]
    circuits = _CountingCircuits()
    if starting_state == "half_open":
        _open_half_open_probe(circuits, current)
    injected = RuntimeError("final success clock failed")
    operation_succeeded = [False]
    failure_calls = [0]
    telemetry = InMemoryProviderTelemetry()

    def unstable_now() -> datetime:
        if operation_succeeded[0]:
            failure_calls[0] += 1
            if persistent or failure_calls[0] == 1:
                raise injected
        return current[0]

    def operation(_ctx: ProviderCallContext, _request: object) -> str:
        operation_succeeded[0] = True
        return "ok"

    with pytest.raises(RuntimeError) as caught:
        call_with_policy(
            operation,
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            telemetry=telemetry,
            now=unstable_now,
        )
    assert caught.value is injected
    assert circuits.failure_times == []
    assert len(telemetry.events) == 1
    assert (telemetry.events[0].state, telemetry.events[0].attempts) == ("succeeded", 1)

    recovered = call_with_policy(
        lambda _ctx, _request: "recovered",
        context(now_utc=current[0]),
        circuits=circuits,
        policy=RetryPolicy(synchronous_attempts=1),
        now=lambda: current[0],
    )
    assert recovered.state == "succeeded"


@pytest.mark.parametrize("starting_state", ["closed", "half_open"])
@pytest.mark.parametrize("failure_kind", ["exception", "cancellation"])
def test_initial_clock_failure_before_circuit_allow_is_neutral(
    starting_state: str,
    failure_kind: str,
) -> None:
    """started clock 在 allow 前失败：没有 probe ownership，也没有 provider outcome。"""

    current = [fixed_now()]
    circuits = _CountingCircuits()
    if starting_state == "half_open":
        _open_half_open_probe(circuits, current)
    injected: BaseException
    if failure_kind == "cancellation":
        injected = asyncio.CancelledError("initial clock cancelled")
    else:
        injected = RuntimeError("initial clock failed")
    operation_calls = [0]

    def operation(_ctx: ProviderCallContext, _request: object) -> str:
        operation_calls[0] += 1
        return "unexpected"

    with pytest.raises(type(injected)) as caught:
        call_with_policy(
            operation,
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: (_ for _ in ()).throw(injected),
        )
    assert caught.value is injected
    assert operation_calls == [0]
    assert circuits.failure_times == []

    recovered = call_with_policy(
        lambda _ctx, _request: "ok",
        context(now_utc=current[0]),
        circuits=circuits,
        policy=RetryPolicy(synchronous_attempts=1),
        now=lambda: current[0],
    )
    assert recovered.state == "succeeded"


@pytest.mark.parametrize("failure_kind", ["exception", "cancellation", "invalid_return"])
def test_half_open_allow_callback_failure_releases_probe(failure_kind: str) -> None:
    """allow 在取得 probe 后失败/非法返回时必须中性释放，不得永久卡住 half-open。"""

    current = [fixed_now()]
    injected: BaseException
    if failure_kind == "cancellation":
        injected = asyncio.CancelledError("allow callback cancelled")
    else:
        injected = RuntimeError("allow callback failed")

    class UnstableAllowCircuits(CircuitBreakerRegistry):
        enabled = False

        def allow(self, provider: str, operation: str, now: datetime):
            acquired = super().allow(provider, operation, now)
            if self.enabled:
                if failure_kind == "invalid_return":
                    return None
                raise injected
            return acquired

    circuits = UnstableAllowCircuits(threshold=1, open_seconds=60)
    assert (
        call_with_policy(
            lambda _ctx, _request: (_ for _ in ()).throw(
                ProviderFailure("timeout", retryable=True)
            ),
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        ).state
        == "failed"
    )
    current[0] += timedelta(seconds=61)
    circuits.enabled = True
    expected_type = TypeError if failure_kind == "invalid_return" else type(injected)
    with pytest.raises(expected_type) as caught:
        call_with_policy(
            lambda _ctx, _request: "must not run",
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        )
    if failure_kind != "invalid_return":
        assert caught.value is injected
    circuits.enabled = False
    assert (
        call_with_policy(
            lambda _ctx, _request: "recovered",
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        ).state
        == "succeeded"
    )


@pytest.mark.parametrize("starting_state", ["closed", "half_open"])
@pytest.mark.parametrize("outcome", ["success", "failure"])
@pytest.mark.parametrize("failure_kind", ["exception", "cancellation"])
@pytest.mark.parametrize("timing", ["before", "after"])
def test_circuit_mutation_failure_keeps_aggregate_telemetry(
    starting_state: str,
    outcome: str,
    failure_kind: str,
    timing: str,
) -> None:
    """success/failure callback 失败仍保留 outcome telemetry，且 mutation 至多调用一次。"""

    current = [fixed_now()]
    injected: BaseException
    if failure_kind == "cancellation":
        injected = asyncio.CancelledError(f"circuit {outcome} cancelled {timing}")
    else:
        injected = RuntimeError(f"circuit {outcome} failed {timing}")

    class UnstableMutationCircuits(CircuitBreakerRegistry):
        enabled = False
        mutation_calls = 0

        def _raise_if_enabled(self, position: str) -> None:
            if self.enabled and timing == position:
                raise injected

        def success(self, provider: str, operation: str) -> None:
            if self.enabled and outcome == "success":
                self.mutation_calls += 1
                self._raise_if_enabled("before")
                super().success(provider, operation)
                self._raise_if_enabled("after")
                return
            super().success(provider, operation)

        def failure(self, provider: str, operation: str, failed_at: datetime) -> None:
            if self.enabled and outcome == "failure":
                self.mutation_calls += 1
                self._raise_if_enabled("before")
                super().failure(provider, operation, failed_at)
                self._raise_if_enabled("after")
                return
            super().failure(provider, operation, failed_at)

    circuits = UnstableMutationCircuits(threshold=1, open_seconds=60)
    if starting_state == "half_open":
        assert (
            call_with_policy(
                lambda _ctx, _request: (_ for _ in ()).throw(
                    ProviderFailure("timeout", retryable=True)
                ),
                context(now_utc=current[0]),
                circuits=circuits,
                policy=RetryPolicy(synchronous_attempts=1),
                now=lambda: current[0],
            ).state
            == "failed"
        )
        current[0] += timedelta(seconds=61)
    circuits.enabled = True
    telemetry = InMemoryProviderTelemetry()

    def operation(_ctx: ProviderCallContext, _request: object) -> str:
        if outcome == "failure":
            raise ProviderFailure("upstream_503", status_code=503, retryable=True, sent=True)
        return "ok"

    with pytest.raises(type(injected)) as caught:
        call_with_policy(
            operation,
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            telemetry=telemetry,
            now=lambda: current[0],
        )
    assert caught.value is injected
    assert circuits.mutation_calls == 1
    assert len(telemetry.events) == 1
    assert (telemetry.events[0].state, telemetry.events[0].attempts) == (
        "succeeded" if outcome == "success" else "failed",
        1,
    )
    circuits.enabled = False
    current[0] += timedelta(seconds=61)
    assert (
        call_with_policy(
            lambda _ctx, _request: "recovered",
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        ).state
        == "succeeded"
    )


@pytest.mark.parametrize("result_kind", ["provider_result", "circuit_open"])
@pytest.mark.parametrize("failure_kind", ["exception", "cancellation"])
def test_recovery_callback_failure_still_records_telemetry(
    result_kind: str,
    failure_kind: str,
) -> None:
    """recovery_after_seconds 与 telemetry 独立；前者失败不能让后者零调用。"""

    current = [fixed_now()]
    injected: BaseException
    if failure_kind == "cancellation":
        injected = asyncio.CancelledError("recovery callback cancelled")
    else:
        injected = RuntimeError("recovery callback failed")

    class UnstableRecoveryCircuits(CircuitBreakerRegistry):
        enabled = False

        def recovery_after_seconds(self, provider: str, operation: str, now: datetime):
            if self.enabled:
                raise injected
            return super().recovery_after_seconds(provider, operation, now)

    circuits = UnstableRecoveryCircuits(threshold=1, open_seconds=60)
    if result_kind == "circuit_open":
        assert (
            call_with_policy(
                lambda _ctx, _request: (_ for _ in ()).throw(
                    ProviderFailure("timeout", retryable=True)
                ),
                context(now_utc=current[0]),
                circuits=circuits,
                policy=RetryPolicy(synchronous_attempts=1),
                now=lambda: current[0],
            ).state
            == "failed"
        )
    circuits.enabled = True
    telemetry = InMemoryProviderTelemetry()
    if result_kind == "circuit_open":
        with pytest.raises(CircuitOpen):
            call_with_policy(
                lambda _ctx, _request: "must not run",
                context(now_utc=current[0]),
                circuits=circuits,
                policy=RetryPolicy(synchronous_attempts=1),
                telemetry=telemetry,
                now=lambda: current[0],
            )
        assert len(telemetry.events) == 1
        assert telemetry.events[0].error_class == "circuit_open"
    else:
        with pytest.raises(type(injected)) as caught:
            call_with_policy(
                lambda _ctx, _request: "ok",
                context(now_utc=current[0]),
                circuits=circuits,
                policy=RetryPolicy(synchronous_attempts=1),
                telemetry=telemetry,
                now=lambda: current[0],
            )
        assert caught.value is injected
        assert len(telemetry.events) == 1
        assert telemetry.events[0].state == "succeeded"


@pytest.mark.parametrize("invalid_recovery", [True, -1, "soon"])
def test_recovery_callback_rejects_invalid_return_but_records_telemetry(
    invalid_recovery,
) -> None:
    """recovery_after_seconds 只接受非负 int（排除 bool）或 None。"""

    class InvalidRecoveryCircuits(CircuitBreakerRegistry):
        def recovery_after_seconds(self, provider: str, operation: str, now: datetime):
            del provider, operation, now
            return invalid_recovery

    telemetry = InMemoryProviderTelemetry()
    with pytest.raises(TypeError, match="recovery_after_seconds must return"):
        call_with_policy(
            lambda _ctx, _request: "ok",
            context(),
            circuits=InvalidRecoveryCircuits(),
            policy=RetryPolicy(synchronous_attempts=1),
            telemetry=telemetry,
            now=fixed_now,
        )
    assert len(telemetry.events) == 1
    assert telemetry.events[0].state == "succeeded"
    assert telemetry.events[0].recovery_after_seconds is None


@pytest.mark.parametrize("callback_site", ["delay", "jitter", "sleep"])
def test_retry_callback_rejects_invalid_normal_return(callback_site: str) -> None:
    """delay/jitter 必须返回非负有限数，sleep 必须正常返回 None。"""

    class InvalidDelayPolicy(RetryPolicy):
        def delay(self, attempt_number: int):
            del attempt_number
            return True

    current = [fixed_now()]
    circuits = CircuitBreakerRegistry(threshold=1)
    telemetry = InMemoryProviderTelemetry()
    policy = (
        InvalidDelayPolicy(synchronous_attempts=2)
        if callback_site == "delay"
        else RetryPolicy(synchronous_attempts=2)
    )
    jitter = (lambda _delay: True) if callback_site == "jitter" else (lambda _delay: 0)
    sleep = (lambda _delay: "invalid") if callback_site == "sleep" else (lambda _delay: None)

    with pytest.raises(TypeError, match=rf"{callback_site} callback must return"):
        call_with_policy(
            lambda _ctx, _request: (_ for _ in ()).throw(
                ProviderFailure("upstream_503", status_code=503, retryable=True, sent=True)
            ),
            context(now_utc=current[0]),
            circuits=circuits,
            policy=policy,
            telemetry=telemetry,
            jitter=jitter,
            sleep=sleep,
            now=lambda: current[0],
        )
    assert len(telemetry.events) == 1
    assert (telemetry.events[0].state, telemetry.events[0].error_class) == (
        "failed",
        "upstream_503",
    )
    with pytest.raises(CircuitOpen):
        call_with_policy(
            lambda _ctx, _request: "too soon",
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        )


@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_circuit_mutation_rejects_invalid_normal_return(outcome: str) -> None:
    """circuit success/failure callback 必须返回 None，非法返回仍保留 aggregate telemetry。"""

    class InvalidMutationCircuits(CircuitBreakerRegistry):
        def success(self, provider: str, operation: str):
            super().success(provider, operation)
            return "invalid"

        def failure(self, provider: str, operation: str, failed_at: datetime):
            super().failure(provider, operation, failed_at)
            return "invalid"

    telemetry = InMemoryProviderTelemetry()

    def operation(_ctx: ProviderCallContext, _request: object) -> str:
        if outcome == "failure":
            raise ProviderFailure("upstream_503", status_code=503, retryable=True, sent=True)
        return "ok"

    with pytest.raises(TypeError, match=rf"circuit {outcome} must return None"):
        call_with_policy(
            operation,
            context(),
            circuits=InvalidMutationCircuits(threshold=1),
            policy=RetryPolicy(synchronous_attempts=1),
            telemetry=telemetry,
            now=fixed_now,
        )
    assert len(telemetry.events) == 1
    assert telemetry.events[0].state == ("succeeded" if outcome == "success" else "failed")


@pytest.mark.parametrize(
    "callback_site",
    [
        "loop_now",
        "failure_now",
        "delay",
        "jitter",
        "sleep",
        "success_final_now",
        "circuit_success",
        "circuit_failure",
    ],
)
def test_non_allow_circuit_open_is_not_reclassified(callback_site: str) -> None:
    """只有 allow 拒绝可生成 circuit_open；其他 callback 的同类异常仍是本地错误。"""

    injected = CircuitOpen(f"{callback_site} callback failed")
    telemetry = InMemoryProviderTelemetry()
    now_calls = 0
    provider_failed = False
    provider_succeeded = False

    class InjectedCircuitCallbacks(CircuitBreakerRegistry):
        def success(self, provider: str, operation: str) -> None:
            if callback_site == "circuit_success":
                raise injected
            super().success(provider, operation)

        def failure(self, provider: str, operation: str, failed_at: datetime) -> None:
            if callback_site == "circuit_failure":
                raise injected
            super().failure(provider, operation, failed_at)

    class InjectedDelayPolicy(RetryPolicy):
        def delay(self, attempt_number: int) -> float:
            if callback_site == "delay":
                raise injected
            return super().delay(attempt_number)

    def injected_now() -> datetime:
        nonlocal now_calls
        now_calls += 1
        if callback_site == "loop_now" and now_calls == 2:
            raise injected
        if callback_site == "failure_now" and provider_failed:
            raise injected
        if callback_site == "success_final_now" and provider_succeeded:
            raise injected
        return fixed_now()

    def injected_jitter(_delay: float) -> float:
        if callback_site == "jitter":
            raise injected
        return 0

    def injected_sleep(_delay: float) -> None:
        if callback_site == "sleep":
            raise injected

    failure_sites = {"failure_now", "delay", "jitter", "sleep", "circuit_failure"}

    def operation(_ctx: ProviderCallContext, _request: object) -> str:
        nonlocal provider_failed, provider_succeeded
        if callback_site in failure_sites:
            provider_failed = True
            raise ProviderFailure(
                "upstream_503",
                status_code=503,
                retryable=True,
                sent=True,
            )
        provider_succeeded = True
        return "ok"

    attempts = 1 if callback_site == "circuit_failure" else 2
    with pytest.raises(CircuitOpen) as caught:
        call_with_policy(
            operation,
            context(),
            circuits=InjectedCircuitCallbacks(threshold=1),
            policy=InjectedDelayPolicy(synchronous_attempts=attempts),
            telemetry=telemetry,
            now=injected_now,
            jitter=injected_jitter,
            sleep=injected_sleep,
        )
    assert caught.value is injected
    assert all(event.error_class != "circuit_open" for event in telemetry.events)
    if callback_site == "loop_now":
        assert telemetry.events == []
    elif callback_site in {"success_final_now", "circuit_success"}:
        assert [(event.state, event.error_class) for event in telemetry.events] == [
            ("succeeded", None)
        ]
    else:
        assert [(event.state, event.error_class) for event in telemetry.events] == [
            ("failed", "upstream_503")
        ]


@pytest.mark.parametrize(
    "callback_site",
    ["initial_now", "loop_now", "failure_now", "success_final_now"],
)
def test_policy_clock_rejects_non_datetime_return(callback_site: str) -> None:
    """所有 policy clock 采样点共享稳定的 datetime 返回合同。"""

    now_calls = 0
    provider_failed = False
    provider_succeeded = False
    telemetry = InMemoryProviderTelemetry()

    def invalid_now():
        nonlocal now_calls
        now_calls += 1
        if callback_site == "initial_now" and now_calls == 1:
            return None
        if callback_site == "loop_now" and now_calls == 2:
            return None
        if callback_site == "failure_now" and provider_failed:
            return None
        if callback_site == "success_final_now" and provider_succeeded:
            return None
        return fixed_now()

    def operation(_ctx: ProviderCallContext, _request: object) -> str:
        nonlocal provider_failed, provider_succeeded
        if callback_site == "failure_now":
            provider_failed = True
            raise ProviderFailure(
                "upstream_503",
                status_code=503,
                retryable=True,
                sent=True,
            )
        provider_succeeded = True
        return "ok"

    with pytest.raises(TypeError, match="now callback must return datetime"):
        call_with_policy(
            operation,
            context(),
            circuits=CircuitBreakerRegistry(threshold=1),
            policy=RetryPolicy(synchronous_attempts=1),
            telemetry=telemetry,
            now=invalid_now,
        )
    expected = {
        "initial_now": [],
        "loop_now": [],
        "failure_now": [("failed", "upstream_503")],
        "success_final_now": [("succeeded", None)],
    }[callback_site]
    assert [(event.state, event.error_class) for event in telemetry.events] == expected


@pytest.mark.parametrize("invalid_attempts", [0, -1, True, 1.5, "3"])
def test_max_attempts_callback_requires_positive_int(invalid_attempts) -> None:
    """非法 retry budget 必须在 allow/operation 前稳定失败，不能伪造 deadline 结果。"""

    class InvalidAttemptsPolicy(RetryPolicy):
        def max_attempts(self, asynchronous: bool):
            del asynchronous
            return invalid_attempts

    operation_calls = 0

    def operation(_ctx: ProviderCallContext, _request: object) -> str:
        nonlocal operation_calls
        operation_calls += 1
        return "unexpected"

    with pytest.raises(TypeError, match="max_attempts callback must return a positive int"):
        call_with_policy(
            operation,
            context(),
            policy=InvalidAttemptsPolicy(),
            circuits=CircuitBreakerRegistry(),
            now=fixed_now,
        )
    assert operation_calls == 0


@pytest.mark.parametrize("failure_kind", ["exception", "cancellation", "invalid_return"])
def test_telemetry_callback_contract_preserves_completed_outcome(
    failure_kind: str,
) -> None:
    """result/circuit 已完成后 telemetry 故障传播，但 callback 只调用一次。"""

    injected: BaseException
    if failure_kind == "cancellation":
        injected = asyncio.CancelledError("telemetry cancelled")
    else:
        injected = RuntimeError("telemetry failed")

    class BrokenTelemetry:
        def __init__(self) -> None:
            self.events: list[object] = []

        def record(self, observation):
            self.events.append(observation)
            if failure_kind == "invalid_return":
                return "invalid"
            raise injected

    telemetry = BrokenTelemetry()
    expected_type = TypeError if failure_kind == "invalid_return" else type(injected)
    with pytest.raises(expected_type) as caught:
        call_with_policy(
            lambda _ctx, _request: "ok",
            context(),
            circuits=CircuitBreakerRegistry(),
            policy=RetryPolicy(synchronous_attempts=1),
            telemetry=telemetry,
            now=fixed_now,
        )
    if failure_kind != "invalid_return":
        assert caught.value is injected
    assert len(telemetry.events) == 1
    assert telemetry.events[0].state == "succeeded"


@pytest.mark.parametrize("failure_kind", ["exception", "cancellation", "invalid_return"])
def test_half_open_abort_probe_callback_contract(failure_kind: str) -> None:
    """中性 half-open 清理不依赖 now；callback 故障传播且实际 probe 可恢复。"""

    current = [fixed_now()]
    injected: BaseException
    if failure_kind == "cancellation":
        injected = asyncio.CancelledError("abort probe cancelled")
    else:
        injected = RuntimeError("abort probe failed")

    class BrokenAbortCircuits(CircuitBreakerRegistry):
        enabled = False
        abort_calls = 0

        def abort_probe(self, provider: str, operation: str):
            super().abort_probe(provider, operation)
            if self.enabled:
                self.abort_calls += 1
                if failure_kind == "invalid_return":
                    return "invalid"
                raise injected
            return None

    circuits = BrokenAbortCircuits(threshold=1, open_seconds=60)
    assert (
        call_with_policy(
            lambda _ctx, _request: (_ for _ in ()).throw(
                ProviderFailure("timeout", retryable=True)
            ),
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        ).state
        == "failed"
    )
    current[0] += timedelta(seconds=61)
    circuits.enabled = True
    telemetry = InMemoryProviderTelemetry()
    expected_type = TypeError if failure_kind == "invalid_return" else type(injected)
    with pytest.raises(expected_type) as caught:
        call_with_policy(
            lambda _ctx, _request: (_ for _ in ()).throw(ProviderPreSendDeadlineExceeded()),
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            telemetry=telemetry,
            now=lambda: current[0],
        )
    if failure_kind != "invalid_return":
        assert caught.value is injected
    assert circuits.abort_calls == 1
    assert [(event.state, event.attempts) for event in telemetry.events] == [("not_sent", 0)]
    circuits.enabled = False
    assert (
        call_with_policy(
            lambda _ctx, _request: "recovered",
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        ).state
        == "succeeded"
    )


@pytest.mark.parametrize("fatal_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("callback_site", ["loop_now", "operation", "telemetry"])
def test_fatal_base_exceptions_are_not_swallowed_and_release_probe(
    fatal_type,
    callback_site: str,
) -> None:
    """只处理 Exception/精确 cancellation；进程级异常原样穿透并完成 probe 清理。"""

    current = [fixed_now()]
    circuits = _CountingCircuits()
    _open_half_open_probe(circuits, current)
    injected = fatal_type(f"fatal {callback_site}")
    now_calls = 0

    class FatalTelemetry:
        def record(self, observation) -> None:
            del observation
            if callback_site == "telemetry":
                raise injected

    def fatal_now() -> datetime:
        nonlocal now_calls
        now_calls += 1
        if callback_site == "loop_now" and now_calls == 2:
            raise injected
        return current[0]

    def operation(_ctx: ProviderCallContext, _request: object) -> str:
        if callback_site == "operation":
            raise injected
        return "ok"

    with pytest.raises(fatal_type) as caught:
        call_with_policy(
            operation,
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            telemetry=FatalTelemetry(),
            now=fatal_now,
        )
    assert caught.value is injected
    assert circuits.failure_times == []
    assert (
        call_with_policy(
            lambda _ctx, _request: "recovered",
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        ).state
        == "succeeded"
    )


def test_allow_circuit_open_after_probe_acquire_releases_its_own_probe() -> None:
    """allow 取得 token 后再抛 CircuitOpen，只能释放本次 ownership。"""

    current = [fixed_now()]
    injected = CircuitOpen("allow failed after acquiring probe")

    class CircuitOpenAfterAcquire(CircuitBreakerRegistry):
        enabled = False

        def allow(self, provider: str, operation: str, now: datetime) -> bool:
            acquired = super().allow(provider, operation, now)
            if self.enabled and acquired:
                raise injected
            return acquired

    circuits = CircuitOpenAfterAcquire(threshold=1, open_seconds=60)
    assert (
        call_with_policy(
            lambda _ctx, _request: (_ for _ in ()).throw(
                ProviderFailure("timeout", retryable=True)
            ),
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        ).state
        == "failed"
    )
    current[0] += timedelta(seconds=61)
    circuits.enabled = True
    with pytest.raises(CircuitOpen) as caught:
        call_with_policy(
            lambda _ctx, _request: "unexpected",
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        )
    assert caught.value is injected
    circuits.enabled = False
    assert (
        call_with_policy(
            lambda _ctx, _request: "recovered",
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        ).state
        == "succeeded"
    )


def test_allow_failure_before_acquire_does_not_release_another_probe() -> None:
    """无 ownership 的 allow 错误不得清除另一调用已持有的 half-open probe。"""

    current = [fixed_now()]
    injected = RuntimeError("allow failed before acquiring probe")

    class FailureBeforeAcquire(CircuitBreakerRegistry):
        enabled = False

        def allow(self, provider: str, operation: str, now: datetime) -> bool:
            if self.enabled:
                raise injected
            return super().allow(provider, operation, now)

    circuits = FailureBeforeAcquire(threshold=1, open_seconds=60)
    assert (
        call_with_policy(
            lambda _ctx, _request: (_ for _ in ()).throw(
                ProviderFailure("timeout", retryable=True)
            ),
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        ).state
        == "failed"
    )
    current[0] += timedelta(seconds=61)
    assert circuits.allow("test-provider", "generate", current[0]) is True
    circuits.enabled = True
    with pytest.raises(RuntimeError) as caught:
        call_with_policy(
            lambda _ctx, _request: "unexpected",
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        )
    assert caught.value is injected
    circuits.enabled = False
    try:
        with pytest.raises(CircuitOpen, match="probe already in flight"):
            call_with_policy(
                lambda _ctx, _request: "must remain blocked",
                context(now_utc=current[0]),
                circuits=circuits,
                policy=RetryPolicy(synchronous_attempts=1),
                now=lambda: current[0],
            )
    finally:
        circuits.abort_probe("test-provider", "generate")


@pytest.mark.parametrize("direct_callback", ["abort", "success", "failure"])
def test_unowned_circuit_callback_cannot_mutate_token_owned_probe(
    direct_callback: str,
) -> None:
    """legacy direct callback 只能完成 legacy owner=None probe，不能越权改写 token owner。"""

    current = [fixed_now()]
    circuits = CircuitBreakerRegistry(threshold=1, open_seconds=60)
    assert (
        call_with_policy(
            lambda _ctx, _request: (_ for _ in ()).throw(
                ProviderFailure("timeout", retryable=True)
            ),
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        ).state
        == "failed"
    )
    current[0] += timedelta(seconds=61)
    owner = object()
    contender = object()
    assert (
        circuits.allow_with_probe_token(
            "test-provider",
            "generate",
            current[0],
            owner,
        )
        is True
    )
    if direct_callback == "abort":
        circuits.abort_probe("test-provider", "generate")
    elif direct_callback == "success":
        circuits.success("test-provider", "generate")
    else:
        circuits.failure("test-provider", "generate", current[0])
    try:
        with pytest.raises(CircuitOpen, match="probe already in flight"):
            circuits.allow_with_probe_token(
                "test-provider",
                "generate",
                current[0],
                contender,
            )
    finally:
        circuits.abort_probe_with_token("test-provider", "generate", owner)


@pytest.mark.parametrize("fatal_type", [KeyboardInterrupt, SystemExit])
def test_allow_fatal_after_probe_acquire_releases_probe_without_swallowing(
    fatal_type,
) -> None:
    """allow after-acquire fatal 经 finally 安全补偿，原 BaseException 对象不被捕获。"""

    current = [fixed_now()]
    injected = fatal_type("fatal allow after acquire")

    class FatalAfterAcquire(CircuitBreakerRegistry):
        enabled = False

        def allow(self, provider: str, operation: str, now: datetime) -> bool:
            acquired = super().allow(provider, operation, now)
            if self.enabled and acquired:
                raise injected
            return acquired

    circuits = FatalAfterAcquire(threshold=1, open_seconds=60)
    assert (
        call_with_policy(
            lambda _ctx, _request: (_ for _ in ()).throw(
                ProviderFailure("timeout", retryable=True)
            ),
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        ).state
        == "failed"
    )
    current[0] += timedelta(seconds=61)
    circuits.enabled = True
    with pytest.raises(fatal_type) as caught:
        call_with_policy(
            lambda _ctx, _request: "unexpected",
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        )
    assert caught.value is injected
    circuits.enabled = False
    assert (
        call_with_policy(
            lambda _ctx, _request: "recovered",
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        ).state
        == "succeeded"
    )


def test_async_exception_during_allow_ownership_handoff_releases_probe() -> None:
    """registry token 与局部 half_open 标志交接期间的 fatal 也必须有唯一清理方。"""

    current = [fixed_now()]
    circuits = CircuitBreakerRegistry(threshold=1, open_seconds=60)
    assert (
        call_with_policy(
            lambda _ctx, _request: (_ for _ in ()).throw(
                ProviderFailure("timeout", retryable=True)
            ),
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        ).state
        == "failed"
    )
    current[0] += timedelta(seconds=61)
    injected = KeyboardInterrupt("fatal during allow ownership handoff")

    def interrupt_handoff(frame, event, arg):
        del arg
        if frame.f_code is call_with_policy.__code__ and event == "line":
            local_state = frame.f_locals
            if local_state.get("allowed") is True and (
                (
                    local_state.get("allow_completed") is True
                    and local_state.get("half_open_probe") is False
                )
                or (
                    local_state.get("allow_completed") is False
                    and local_state.get("half_open_probe") is True
                )
            ):
                sys.settrace(None)
                raise injected
        return interrupt_handoff

    sys.settrace(interrupt_handoff)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            call_with_policy(
                lambda _ctx, _request: "unexpected",
                context(now_utc=current[0]),
                circuits=circuits,
                policy=RetryPolicy(synchronous_attempts=1),
                now=lambda: current[0],
            )
    finally:
        sys.settrace(None)
    assert caught.value is injected
    assert (
        call_with_policy(
            lambda _ctx, _request: "recovered",
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        ).state
        == "succeeded"
    )


@pytest.mark.parametrize(
    ("original_type", "cleanup_type"),
    [(KeyboardInterrupt, SystemExit), (SystemExit, KeyboardInterrupt)],
)
def test_active_operation_fatal_wins_over_pre_super_abort_fatal_and_releases_probe(
    original_type,
    cleanup_type,
) -> None:
    """cleanup 隔离层保留 active fatal 对象，并以内部 fallback 释放 probe。"""

    current = [fixed_now()]
    original = original_type("original operation fatal")
    cleanup = cleanup_type("abort cleanup fatal")

    class FatalBeforeAbortCircuits(CircuitBreakerRegistry):
        enabled = False
        abort_calls = 0

        def abort_probe(self, provider: str, operation: str):
            if self.enabled:
                self.abort_calls += 1
                raise cleanup
            return super().abort_probe(provider, operation)

    circuits = FatalBeforeAbortCircuits(threshold=1, open_seconds=60)
    assert (
        call_with_policy(
            lambda _ctx, _request: (_ for _ in ()).throw(
                ProviderFailure("timeout", retryable=True)
            ),
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        ).state
        == "failed"
    )
    current[0] += timedelta(seconds=61)
    circuits.enabled = True

    caught: BaseException | None = None
    try:
        call_with_policy(
            lambda _ctx, _request: (_ for _ in ()).throw(original),
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        )
    except BaseException as exc:
        caught = exc
    assert caught is original
    assert circuits.abort_calls == 1

    circuits.enabled = False
    assert (
        call_with_policy(
            lambda _ctx, _request: "recovered",
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        ).state
        == "succeeded"
    )


def test_allow_fatal_wins_over_pre_super_abort_fatal_and_releases_probe() -> None:
    """allow ownership handoff 前的 cleanup fatal 不得替换原 allow fatal。"""

    current = [fixed_now()]
    original = KeyboardInterrupt("original allow fatal")
    cleanup = SystemExit("allow cleanup fatal")

    class FatalAllowAndAbortCircuits(CircuitBreakerRegistry):
        enabled = False
        abort_calls = 0

        def allow(self, provider: str, operation: str, now: datetime) -> bool:
            acquired = super().allow(provider, operation, now)
            if self.enabled and acquired:
                raise original
            return acquired

        def abort_probe(self, provider: str, operation: str):
            if self.enabled:
                self.abort_calls += 1
                raise cleanup
            return super().abort_probe(provider, operation)

    circuits = FatalAllowAndAbortCircuits(threshold=1, open_seconds=60)
    assert (
        call_with_policy(
            lambda _ctx, _request: (_ for _ in ()).throw(
                ProviderFailure("timeout", retryable=True)
            ),
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        ).state
        == "failed"
    )
    current[0] += timedelta(seconds=61)
    circuits.enabled = True

    caught: BaseException | None = None
    try:
        call_with_policy(
            lambda _ctx, _request: "unexpected",
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        )
    except BaseException as exc:
        caught = exc
    assert caught is original
    assert circuits.abort_calls == 1

    circuits.enabled = False
    assert (
        call_with_policy(
            lambda _ctx, _request: "recovered",
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        ).state
        == "succeeded"
    )


@pytest.mark.parametrize("active_circuit_fatal", [False, True])
def test_cleanup_fatals_preserve_active_error_or_raise_first_cleanup_object(
    active_circuit_fatal: bool,
) -> None:
    """所有 cleanup 都运行一次；active error 胜出，否则传播第一个 cleanup fatal。"""

    current = [fixed_now()]
    original = KeyboardInterrupt("original circuit mutation fatal")
    first_cleanup = SystemExit("first recovery cleanup fatal")
    second_cleanup = KeyboardInterrupt("second telemetry cleanup fatal")

    class FatalCleanupCircuits(CircuitBreakerRegistry):
        enabled = False
        success_calls = 0
        recovery_calls = 0

        def success(self, provider: str, operation: str) -> None:
            if self.enabled:
                self.success_calls += 1
                if active_circuit_fatal:
                    raise original
            super().success(provider, operation)

        def recovery_after_seconds(
            self, provider: str, operation: str, now: datetime
        ) -> int | None:
            if self.enabled:
                self.recovery_calls += 1
                raise first_cleanup
            return super().recovery_after_seconds(provider, operation, now)

    class FatalTelemetry:
        enabled = False
        calls = 0

        def record(self, observation) -> None:
            del observation
            if self.enabled:
                self.calls += 1
                raise second_cleanup

    circuits = FatalCleanupCircuits(threshold=1, open_seconds=60)
    telemetry = FatalTelemetry()
    if active_circuit_fatal:
        assert (
            call_with_policy(
                lambda _ctx, _request: (_ for _ in ()).throw(
                    ProviderFailure("timeout", retryable=True)
                ),
                context(now_utc=current[0]),
                circuits=circuits,
                telemetry=telemetry,
                policy=RetryPolicy(synchronous_attempts=1),
                now=lambda: current[0],
            ).state
            == "failed"
        )
        current[0] += timedelta(seconds=61)
    circuits.enabled = True
    telemetry.enabled = True
    expected = original if active_circuit_fatal else first_cleanup

    caught: BaseException | None = None
    try:
        call_with_policy(
            lambda _ctx, _request: "ok",
            context(now_utc=current[0]),
            circuits=circuits,
            telemetry=telemetry,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        )
    except BaseException as exc:
        caught = exc
    assert caught is expected
    assert circuits.success_calls == 1
    assert circuits.recovery_calls == 1
    assert telemetry.calls == 1

    circuits.enabled = False
    telemetry.enabled = False
    assert (
        call_with_policy(
            lambda _ctx, _request: "recovered",
            context(now_utc=current[0]),
            circuits=circuits,
            telemetry=telemetry,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        ).state
        == "succeeded"
    )


@pytest.mark.parametrize(
    "failure_kind",
    ["exception", "cancellation", "keyboard_interrupt", "system_exit", "invalid_return"],
)
def test_pre_super_abort_probe_failure_uses_token_guarded_fallback(
    failure_kind: str,
) -> None:
    """abort hook 在 super 前失败也只调用一次，并由不可覆写 fallback 释放本 token。"""

    current = [fixed_now()]
    errors: dict[str, BaseException] = {
        "exception": RuntimeError("abort failed before super"),
        "cancellation": asyncio.CancelledError("abort cancelled before super"),
        "keyboard_interrupt": KeyboardInterrupt("abort interrupted before super"),
        "system_exit": SystemExit("abort exited before super"),
    }
    injected = errors.get(failure_kind)

    class PreSuperBrokenAbortCircuits(CircuitBreakerRegistry):
        enabled = False
        abort_calls = 0

        def abort_probe(self, provider: str, operation: str):
            if self.enabled:
                self.abort_calls += 1
                if failure_kind == "invalid_return":
                    return "invalid"
                assert injected is not None
                raise injected
            return super().abort_probe(provider, operation)

    circuits = PreSuperBrokenAbortCircuits(threshold=1, open_seconds=60)
    assert (
        call_with_policy(
            lambda _ctx, _request: (_ for _ in ()).throw(
                ProviderFailure("timeout", retryable=True)
            ),
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        ).state
        == "failed"
    )
    current[0] += timedelta(seconds=61)
    circuits.enabled = True
    telemetry = InMemoryProviderTelemetry()
    expected_type = TypeError if failure_kind == "invalid_return" else type(injected)

    with pytest.raises(expected_type) as caught:
        call_with_policy(
            lambda _ctx, _request: (_ for _ in ()).throw(ProviderPreSendDeadlineExceeded()),
            context(now_utc=current[0]),
            circuits=circuits,
            telemetry=telemetry,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        )
    if injected is not None:
        assert caught.value is injected
    assert circuits.abort_calls == 1
    assert [(event.state, event.attempts) for event in telemetry.events] == [("not_sent", 0)]

    circuits.enabled = False
    assert (
        call_with_policy(
            lambda _ctx, _request: "recovered",
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        ).state
        == "succeeded"
    )


def test_old_abort_fallback_token_does_not_release_reentrant_new_probe() -> None:
    """旧 cleanup token 的 fallback 对 callback 内新取得的其他 owner probe 为 no-op。"""

    current = [fixed_now()]
    new_owner = object()
    contender = object()

    class ReentrantAbortCircuits(CircuitBreakerRegistry):
        enabled = False
        abort_calls = 0

        def abort_probe(self, provider: str, operation: str):
            super().abort_probe(provider, operation)
            if self.enabled:
                self.abort_calls += 1
                assert (
                    self.allow_with_probe_token(
                        provider,
                        operation,
                        current[0],
                        new_owner,
                    )
                    is True
                )
                return "invalid"
            return None

    circuits = ReentrantAbortCircuits(threshold=1, open_seconds=60)
    assert (
        call_with_policy(
            lambda _ctx, _request: (_ for _ in ()).throw(
                ProviderFailure("timeout", retryable=True)
            ),
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        ).state
        == "failed"
    )
    current[0] += timedelta(seconds=61)
    circuits.enabled = True

    with pytest.raises(TypeError, match="circuit abort_probe must return None"):
        call_with_policy(
            lambda _ctx, _request: (_ for _ in ()).throw(ProviderPreSendDeadlineExceeded()),
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        )
    assert circuits.abort_calls == 1
    with pytest.raises(CircuitOpen, match="probe already in flight"):
        circuits.allow_with_probe_token(
            "test-provider",
            "generate",
            current[0],
            contender,
        )

    circuits.enabled = False
    circuits.abort_probe_with_token(
        "test-provider",
        "generate",
        new_owner,
    )


def test_caller_exception_context_does_not_hide_cleanup_fatal() -> None:
    """调用方 except 上下文不是本 invocation active error，cleanup fatal 必须传播。"""

    outer_error = RuntimeError("already handled by caller")
    cleanup_fatal = SystemExit("recovery cleanup fatal")

    class FatalRecoveryCircuits(CircuitBreakerRegistry):
        def recovery_after_seconds(
            self, provider: str, operation: str, now: datetime
        ) -> int | None:
            del provider, operation, now
            raise cleanup_fatal

    caught: BaseException | None = None
    try:
        raise outer_error
    except RuntimeError as handled:
        assert handled is outer_error
        try:
            call_with_policy(
                lambda _ctx, _request: "ok",
                context(),
                circuits=FatalRecoveryCircuits(),
                policy=RetryPolicy(synchronous_attempts=1),
                now=fixed_now,
            )
        except BaseException as exc:
            caught = exc

    assert caught is cleanup_fatal


def test_non_allow_circuit_open_preserves_identity_when_abort_cleanup_fails() -> None:
    """非 allow CircuitOpen 必须胜过 abort cleanup error，并释放 half-open probe。"""

    current = [fixed_now()]
    original = CircuitOpen("success callback circuit-open error")
    cleanup = RuntimeError("abort cleanup failed")

    class BrokenAbortAfterSuccess(CircuitBreakerRegistry):
        enabled = False
        success_calls = 0
        abort_calls = 0

        def success(self, provider: str, operation: str) -> None:
            if self.enabled:
                self.success_calls += 1
                raise original
            super().success(provider, operation)

        def abort_probe(self, provider: str, operation: str):
            if self.enabled:
                self.abort_calls += 1
                raise cleanup
            return super().abort_probe(provider, operation)

    circuits = BrokenAbortAfterSuccess(threshold=1, open_seconds=60)
    assert (
        call_with_policy(
            lambda _ctx, _request: (_ for _ in ()).throw(
                ProviderFailure("timeout", retryable=True)
            ),
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        ).state
        == "failed"
    )
    current[0] += timedelta(seconds=61)
    circuits.enabled = True

    caught: BaseException | None = None
    try:
        call_with_policy(
            lambda _ctx, _request: "ok",
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        )
    except BaseException as exc:
        caught = exc

    assert caught is original
    assert circuits.success_calls == 1
    assert circuits.abort_calls == 1
    circuits.enabled = False
    assert (
        call_with_policy(
            lambda _ctx, _request: "recovered",
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        ).state
        == "succeeded"
    )


def test_non_allow_loop_circuit_open_preserves_identity_when_abort_exits() -> None:
    """loop now 的 CircuitOpen 必须胜过 SystemExit cleanup，并释放 probe。"""

    current = [fixed_now()]
    original = CircuitOpen("loop clock circuit-open error")
    cleanup = SystemExit("abort cleanup exit")
    now_calls = 0

    class BrokenAbort(CircuitBreakerRegistry):
        enabled = False
        abort_calls = 0

        def abort_probe(self, provider: str, operation: str):
            if self.enabled:
                self.abort_calls += 1
                raise cleanup
            return super().abort_probe(provider, operation)

    def injected_now() -> datetime:
        nonlocal now_calls
        now_calls += 1
        if now_calls == 2:
            raise original
        return current[0]

    circuits = BrokenAbort(threshold=1, open_seconds=60)
    assert (
        call_with_policy(
            lambda _ctx, _request: (_ for _ in ()).throw(
                ProviderFailure("timeout", retryable=True)
            ),
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        ).state
        == "failed"
    )
    current[0] += timedelta(seconds=61)
    circuits.enabled = True

    caught: BaseException | None = None
    try:
        call_with_policy(
            lambda _ctx, _request: "must not run",
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=injected_now,
        )
    except BaseException as exc:
        caught = exc

    assert caught is original
    assert circuits.abort_calls == 1
    circuits.enabled = False
    assert (
        call_with_policy(
            lambda _ctx, _request: "recovered",
            context(now_utc=current[0]),
            circuits=circuits,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        ).state
        == "succeeded"
    )


def test_allow_circuit_open_still_emits_public_rejection_telemetry() -> None:
    """allow 阶段的 CircuitOpen 仍按公开 circuit rejection 分类。"""

    current = [fixed_now()]
    circuits = CircuitBreakerRegistry(threshold=1, open_seconds=60)
    telemetry = InMemoryProviderTelemetry()
    assert (
        call_with_policy(
            lambda _ctx, _request: (_ for _ in ()).throw(
                ProviderFailure("timeout", retryable=True)
            ),
            context(now_utc=current[0]),
            circuits=circuits,
            telemetry=telemetry,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        ).state
        == "failed"
    )
    telemetry.events.clear()

    with pytest.raises(CircuitOpen):
        call_with_policy(
            lambda _ctx, _request: "must not run",
            context(now_utc=current[0]),
            circuits=circuits,
            telemetry=telemetry,
            policy=RetryPolicy(synchronous_attempts=1),
            now=lambda: current[0],
        )

    assert len(telemetry.events) == 1
    assert telemetry.events[0].error_class == "circuit_open"
