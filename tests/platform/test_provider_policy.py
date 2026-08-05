from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.platform.provider import (
    CircuitBreakerRegistry,
    CircuitOpen,
    InMemoryProviderTelemetry,
    ProviderCallContext,
    ProviderFailure,
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


def test_retryable_failures_use_new_attempt_ids_and_return_success() -> None:
    attempts: list[ProviderCallContext] = []
    outcomes = iter(
        [
            ProviderFailure("timeout", retryable=True),
            ProviderFailure("upstream_503", status_code=503, retryable=True),
            "ok",
        ]
    )
    current = [datetime(2026, 8, 5, 12, 0, tzinfo=UTC)]

    def operation(call: ProviderCallContext, request: object) -> str:
        attempts.append(call)
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
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
    assert len({call.attempt_id for call in attempts}) == 3
    assert all(call.idempotency_key.startswith("generate:") for call in attempts)


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

    result = call_with_policy(
        operation,
        context(seconds=1),
        now=lambda: now[0],
        sleep=lambda delay: (
            sleeps.append(delay),
            now.__setitem__(0, now[0] + timedelta(seconds=delay)),
        ),
        jitter=lambda _delay: 0,
    )

    assert result.state == "failed"
    assert result.attempts == 2
    assert sleeps == [0.25]
    assert now[0] == datetime(2026, 8, 5, 12, 0, 0, 250000, tzinfo=UTC)


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
    current[0] += timedelta(seconds=60)

    recovered = call_with_policy(
        lambda call, request: "ok",
        context(now_utc=current[0]),
        circuits=circuits,
        policy=RetryPolicy(synchronous_attempts=1),
        now=lambda: current[0],
    )

    assert recovered.state == "succeeded"


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
