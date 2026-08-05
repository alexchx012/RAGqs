from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from threading import Lock
from typing import Any, Literal, Protocol

ProviderState = Literal["not_sent", "unknown", "succeeded", "failed"]


class CircuitOpen(RuntimeError):
    """The provider/operation circuit is open."""


class ProviderFailure(RuntimeError):
    def __init__(
        self,
        error_class: str,
        *,
        status_code: int | None = None,
        retryable: bool | None = None,
        sent: bool = False,
    ) -> None:
        super().__init__(error_class)
        self.error_class = error_class
        self.status_code = status_code
        self.retryable = retryable
        self.sent = sent


@dataclass(frozen=True, slots=True)
class ProviderCallContext:
    provider: str
    operation: str
    provider_call_id: str
    attempt_id: str
    deadline_utc: datetime
    resource_id: str | None = None

    @property
    def idempotency_key(self) -> str:
        resource = self.resource_id or "none"
        return f"{self.operation}:{self.attempt_id}:{resource}"


@dataclass(frozen=True, slots=True)
class ProviderResult:
    state: ProviderState
    value: Any = None
    error_class: str | None = None
    elapsed_ms: int = 0
    attempts: int = 0
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class ProviderCallObservation:
    provider: str
    operation: str
    state: ProviderState
    error_class: str | None
    attempts: int
    retry_count: int
    elapsed_ms: int
    recovery_after_seconds: int | None


class ProviderTelemetryPort(Protocol):
    def record(self, observation: ProviderCallObservation) -> None: ...


class InMemoryProviderTelemetry:
    """Deterministic telemetry sink used by tests and replaceable runtime adapters."""

    def __init__(self) -> None:
        self.events: list[ProviderCallObservation] = []

    def record(self, observation: ProviderCallObservation) -> None:
        self.events.append(observation)


class LoggingProviderTelemetry:
    """Process metric/log adapter without request payloads or provider response bodies."""

    def __init__(self) -> None:
        self._logger = logging.getLogger(__name__)
        self._measurements: dict[tuple[str, str, str, str], int] = {}
        self._lock = Lock()

    def record(self, observation: ProviderCallObservation) -> None:
        key = (
            observation.provider,
            observation.operation,
            observation.state,
            observation.error_class or "none",
        )
        with self._lock:
            self._measurements[key] = self._measurements.get(key, 0) + 1
        self._logger.info(
            "provider_call_completed",
            extra={
                "provider_telemetry": {
                    "provider": observation.provider,
                    "operation": observation.operation,
                    "state": observation.state,
                    "error_class": observation.error_class,
                    "attempts": observation.attempts,
                    "retry_count": observation.retry_count,
                    "elapsed_ms": observation.elapsed_ms,
                    "recovery_after_seconds": observation.recovery_after_seconds,
                }
            },
        )


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    synchronous_attempts: int = 3
    asynchronous_attempts: int = 5
    delays_seconds: tuple[float, ...] = (0.25, 1.0, 4.0, 16.0)
    max_delay_seconds: float = 30.0

    def max_attempts(self, asynchronous: bool) -> int:
        return self.asynchronous_attempts if asynchronous else self.synchronous_attempts

    def delay(self, attempt_number: int) -> float:
        index = min(attempt_number - 1, len(self.delays_seconds) - 1)
        return min(self.delays_seconds[index], self.max_delay_seconds)


@dataclass
class _Circuit:
    failures: int = 0
    opened_at: datetime | None = None
    half_open_probe: bool = False


class CircuitBreakerRegistry:
    def __init__(self, *, threshold: int = 5, open_seconds: int = 60) -> None:
        self.threshold = threshold
        self.open_seconds = open_seconds
        self._circuits: dict[tuple[str, str], _Circuit] = {}
        self._lock = Lock()

    def allow(self, provider: str, operation: str, now: datetime) -> bool:
        with self._lock:
            circuit = self._circuits.setdefault((provider, operation), _Circuit())
            if circuit.opened_at is None:
                return False
            elapsed = (now - circuit.opened_at).total_seconds()
            if elapsed < self.open_seconds:
                raise CircuitOpen(f"circuit open for {provider}:{operation}")
            if circuit.half_open_probe:
                raise CircuitOpen(
                    f"circuit half-open probe already in flight for {provider}:{operation}"
                )
            circuit.half_open_probe = True
            return True

    def success(self, provider: str, operation: str) -> None:
        with self._lock:
            self._circuits[(provider, operation)] = _Circuit()

    def failure(self, provider: str, operation: str, now: datetime) -> None:
        with self._lock:
            circuit = self._circuits.setdefault((provider, operation), _Circuit())
            circuit.half_open_probe = False
            circuit.failures += 1
            if circuit.failures >= self.threshold:
                circuit.opened_at = now

    def recovery_after_seconds(self, provider: str, operation: str, now: datetime) -> int | None:
        with self._lock:
            circuit = self._circuits.get((provider, operation))
            if circuit is None or circuit.opened_at is None:
                return None
            remaining = self.open_seconds - (now - circuit.opened_at).total_seconds()
            return max(0, int(remaining))


_DEFAULT_CIRCUITS = CircuitBreakerRegistry()
_DEFAULT_TELEMETRY = LoggingProviderTelemetry()
_RETRYABLE_STATUS = {429, 502, 503, 504}
_RETRYABLE_CLASSES = {"timeout", "connection_lost", "network_error"}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _is_retryable(failure: ProviderFailure) -> bool:
    if failure.status_code is not None and 400 <= failure.status_code < 500:
        return failure.status_code == 429
    if failure.retryable is not None:
        return failure.retryable
    return failure.status_code in _RETRYABLE_STATUS or failure.error_class in _RETRYABLE_CLASSES


def _default_jitter(delay: float) -> float:
    return random.uniform(0, min(delay * 0.1, 0.5))


class ProviderPort(Protocol):
    def call(self, context: ProviderCallContext, request: Any) -> ProviderResult: ...


def call_with_policy(
    operation: Callable[[ProviderCallContext, Any], Any],
    context: ProviderCallContext,
    request: Any = None,
    *,
    asynchronous: bool = False,
    policy: RetryPolicy | None = None,
    circuits: CircuitBreakerRegistry | None = None,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
    jitter: Callable[[float], float] | None = None,
    telemetry: ProviderTelemetryPort | None = None,
) -> ProviderResult:
    policy = policy or RetryPolicy()
    circuits = circuits or _DEFAULT_CIRCUITS
    now = now or (lambda: datetime.now(UTC))
    sleep = sleep or time.sleep
    jitter = jitter or _default_jitter
    telemetry = telemetry or _DEFAULT_TELEMETRY
    started = _utc(now())
    deadline = _utc(context.deadline_utc)
    max_attempts = policy.max_attempts(asynchronous)
    sent = False
    attempts = 0
    half_open_probe = False
    probe_completed = False
    result: ProviderResult | None = None

    def deadline_result(*, sent_on_deadline: bool = False) -> ProviderResult:
        return ProviderResult(
            state=(
                "unknown" if sent or sent_on_deadline else ("failed" if attempts else "not_sent")
            ),
            error_class="deadline_exceeded",
            elapsed_ms=int((_utc(now()) - started).total_seconds() * 1000),
            attempts=attempts,
            retryable=True,
        )

    def complete(value: ProviderResult) -> ProviderResult:
        nonlocal result
        result = value
        return value

    def record_success() -> None:
        nonlocal probe_completed
        circuits.success(context.provider, context.operation)
        probe_completed = probe_completed or half_open_probe

    def record_failure(timestamp: datetime) -> None:
        nonlocal probe_completed
        circuits.failure(context.provider, context.operation, timestamp)
        probe_completed = probe_completed or half_open_probe

    try:
        half_open_probe = circuits.allow(context.provider, context.operation, started)
        while attempts < max_attempts:
            current = _utc(now())
            if current >= deadline:
                return complete(deadline_result())
            attempts += 1
            attempt_context = replace(
                context,
                provider_call_id=f"{context.provider_call_id}:{attempts}",
                attempt_id=f"{context.attempt_id}:{attempts}",
            )
            try:
                value = operation(attempt_context, request)
            except ProviderFailure as failure:
                finished = _utc(now())
                sent = sent or failure.sent
                if finished >= deadline:
                    record_failure(finished)
                    return complete(deadline_result(sent_on_deadline=failure.sent))
                retryable = _is_retryable(failure)
                if not retryable:
                    return complete(
                        ProviderResult(
                            state="failed",
                            error_class=failure.error_class,
                            elapsed_ms=int((finished - started).total_seconds() * 1000),
                            attempts=attempts,
                            retryable=False,
                        )
                    )
                if attempts >= max_attempts:
                    record_failure(finished)
                    return complete(
                        ProviderResult(
                            state="unknown" if sent else "failed",
                            error_class=failure.error_class,
                            elapsed_ms=int((finished - started).total_seconds() * 1000),
                            attempts=attempts,
                            retryable=True,
                        )
                    )
                delay = policy.delay(attempts)
                sleep_seconds = min(policy.max_delay_seconds, delay + jitter(delay))
                remaining_seconds = (deadline - finished).total_seconds()
                if sleep_seconds >= remaining_seconds:
                    record_failure(finished)
                    return complete(deadline_result())
                sleep(sleep_seconds)
                continue
            except Exception:
                finished = _utc(now())
                record_failure(finished)
                if finished >= deadline:
                    return complete(deadline_result())
                return complete(
                    ProviderResult(
                        state="unknown" if sent else "failed",
                        error_class="provider_error",
                        elapsed_ms=int((finished - started).total_seconds() * 1000),
                        attempts=attempts,
                        retryable=False,
                    )
                )
            finished = _utc(now())
            if finished >= deadline:
                record_failure(finished)
                return complete(deadline_result(sent_on_deadline=True))
            record_success()
            return complete(
                ProviderResult(
                    state="succeeded",
                    value=value,
                    elapsed_ms=int((finished - started).total_seconds() * 1000),
                    attempts=attempts,
                )
            )
        return complete(deadline_result())
    except CircuitOpen:
        telemetry.record(
            ProviderCallObservation(
                provider=context.provider,
                operation=context.operation,
                state="failed",
                error_class="circuit_open",
                attempts=0,
                retry_count=0,
                elapsed_ms=int((_utc(now()) - started).total_seconds() * 1000),
                recovery_after_seconds=circuits.recovery_after_seconds(
                    context.provider, context.operation, _utc(now())
                ),
            )
        )
        raise
    finally:
        if half_open_probe and not probe_completed:
            record_failure(_utc(now()))
        if result is not None:
            telemetry.record(
                ProviderCallObservation(
                    provider=context.provider,
                    operation=context.operation,
                    state=result.state,
                    error_class=result.error_class,
                    attempts=result.attempts,
                    retry_count=max(0, result.attempts - 1),
                    elapsed_ms=result.elapsed_ms,
                    recovery_after_seconds=circuits.recovery_after_seconds(
                        context.provider, context.operation, _utc(now())
                    ),
                )
            )
