from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import random
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from threading import Lock, local
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


class ProviderPolicyAbort(RuntimeError):
    """Abort policy evaluation without changing provider circuit or emitting result telemetry.

    供本地基础设施/lifecycle 故障绕过 provider 异常转换使用，包括发送前 hook 与
    operation cancellation 后的终态化写故障；调用方负责重新抛出保存的原始错误。
    """


class ProviderFailureAccountingAbort(RuntimeError):
    """Account one current provider failure, then let the wrapper raise its local error.

    这是 policy 与 usage wrapper 间的内部控制信号；携带已发生的 provider failure
    及是否强制把该 outcome 计入 circuit。外部 callback 异常由 wrapper 自己保存和
    重抛，禁止向外部异常对象动态挂属性。
    """

    def __init__(
        self,
        failure: ProviderFailure,
        *,
        force_circuit_failure: bool = False,
    ) -> None:
        super().__init__(failure.error_class)
        self.failure = failure
        self.force_circuit_failure = force_circuit_failure


class ProviderPreSendDeadlineExceeded(RuntimeError):
    """The local pre-send lifecycle crossed the absolute provider deadline."""


def _canonical_sha256(domain: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(domain.encode("utf-8") + b"\0" + canonical).hexdigest()


def _physical_provider_call_id(root_id: str, attempt_number: int) -> str:
    digest = _canonical_sha256(
        "ragqs-provider-call-v1",
        {"attempt_number": attempt_number, "provider_call_id": root_id},
    )
    return f"pc_{digest[:61]}"


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
        return _canonical_sha256(
            "ragqs-provider-idempotency-v1",
            {
                "attempt_id": self.attempt_id,
                "operation": self.operation,
                "resource_id": self.resource_id,
            },
        )


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
    half_open_probe_owner: object | None = None


class CircuitBreakerRegistry:
    def __init__(self, *, threshold: int = 5, open_seconds: int = 60) -> None:
        self.threshold = threshold
        self.open_seconds = open_seconds
        self._circuits: dict[tuple[str, str], _Circuit] = {}
        self._lock = Lock()
        self._probe_context = local()

    def _invoke_with_probe_token(
        self,
        probe_token: object,
        callback: Callable[[], Any],
    ) -> Any:
        """Expose per-invocation ownership to overridable circuit callbacks."""
        previous = getattr(self._probe_context, "token", None)
        self._probe_context.token = probe_token
        try:
            return callback()
        finally:
            self._probe_context.token = previous

    def allow_with_probe_token(
        self,
        provider: str,
        operation: str,
        now: datetime,
        probe_token: object,
    ) -> Any:
        return self._invoke_with_probe_token(
            probe_token,
            lambda: self.allow(provider, operation, now),
        )

    def success_with_probe_token(
        self,
        provider: str,
        operation: str,
        probe_token: object,
    ) -> Any:
        return self._invoke_with_probe_token(
            probe_token,
            lambda: self.success(provider, operation),
        )

    def failure_with_probe_token(
        self,
        provider: str,
        operation: str,
        now: datetime,
        probe_token: object,
    ) -> Any:
        return self._invoke_with_probe_token(
            probe_token,
            lambda: self.failure(provider, operation, now),
        )

    def _release_probe_owned_by_token(
        self,
        provider: str,
        operation: str,
        probe_token: object | None,
    ) -> None:
        """Atomically release only the exact owner; callers use the base implementation.

        The unbound ``CircuitBreakerRegistry`` call site is intentional: subclasses may
        override the public abort hook, but cannot intercept this state transition.
        """
        with self._lock:
            circuit = self._circuits.get((provider, operation))
            if (
                circuit is None
                or not circuit.half_open_probe
                or circuit.half_open_probe_owner is not probe_token
            ):
                return
            circuit.half_open_probe = False
            circuit.half_open_probe_owner = None

    def abort_probe_with_token(
        self,
        provider: str,
        operation: str,
        probe_token: object,
    ) -> Any:
        try:
            return self._invoke_with_probe_token(
                probe_token,
                lambda: self.abort_probe(provider, operation),
            )
        finally:
            # The overridable hook is observational/customization surface; ownership
            # release is a separate lock-protected invariant even if the hook fails.
            CircuitBreakerRegistry._release_probe_owned_by_token(
                self,
                provider,
                operation,
                probe_token,
            )

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
            circuit.half_open_probe_owner = getattr(self._probe_context, "token", None)
            return True

    def success(self, provider: str, operation: str) -> None:
        with self._lock:
            circuit = self._circuits.get((provider, operation))
            probe_token = getattr(self._probe_context, "token", None)
            if (
                circuit is not None
                and circuit.half_open_probe
                and circuit.half_open_probe_owner is not probe_token
            ):
                return
            self._circuits[(provider, operation)] = _Circuit()

    def failure(self, provider: str, operation: str, now: datetime) -> None:
        with self._lock:
            circuit = self._circuits.setdefault((provider, operation), _Circuit())
            probe_token = getattr(self._probe_context, "token", None)
            if circuit.half_open_probe and circuit.half_open_probe_owner is not probe_token:
                return
            circuit.half_open_probe = False
            circuit.half_open_probe_owner = None
            circuit.failures += 1
            if circuit.failures >= self.threshold:
                circuit.opened_at = now

    def abort_probe(self, provider: str, operation: str) -> None:
        """Run the default abort hook for the current token or a legacy owner."""
        probe_token = getattr(self._probe_context, "token", None)
        CircuitBreakerRegistry._release_probe_owned_by_token(
            self,
            provider,
            operation,
            probe_token,
        )

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
_MAX_SYNCHRONOUS_ATTEMPTS = 3
_MAX_ASYNCHRONOUS_ATTEMPTS = 5
_MAX_PROVIDER_ATTEMPT_SECONDS = 30


def _utc(value: Any, *, callback_name: str | None = None) -> datetime:
    if not isinstance(value, datetime):
        if callback_name is not None:
            raise TypeError(f"{callback_name} callback must return datetime")
        raise TypeError("datetime value must be a datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _is_retryable(failure: ProviderFailure) -> bool:
    if failure.status_code is not None:
        return failure.status_code in _RETRYABLE_STATUS
    return failure.error_class in _RETRYABLE_CLASSES


def _default_jitter(delay: float) -> float:
    return random.uniform(0, min(delay * 0.1, 0.5))


class ProviderPort(Protocol):
    """One synchronous physical provider transport attempt.

    A successful transport returns its raw SDK/provider value. A classified provider
    failure must be raised as :class:`ProviderFailure`; only ``call_with_policy`` owns
    and returns the aggregate :class:`ProviderResult`. Returning ``ProviderResult``
    from this low-level port is a contract error and is rejected at runtime.

    ``call_with_policy`` supplies ``context.deadline_utc`` as the absolute deadline
    for this physical attempt. A transport must pass that deadline (or its equivalent
    timeout) to its network/client operation and stop cooperatively; the policy wrapper
    intentionally never abandons a running synchronous call in a background thread.
    """

    def call(self, context: ProviderCallContext, request: Any) -> object: ...


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
    started = _utc(now(), callback_name="now")
    last_known_now = started
    deadline = _utc(context.deadline_utc)
    max_attempts = policy.max_attempts(asynchronous)
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts <= 0:
        raise TypeError("max_attempts callback must return a positive int")
    hard_attempt_limit = _MAX_ASYNCHRONOUS_ATTEMPTS if asynchronous else _MAX_SYNCHRONOUS_ATTEMPTS
    max_attempts = min(max_attempts, hard_attempt_limit)
    unconfirmed_send = False
    attempts = 0
    last_failure: ProviderFailure | None = None
    half_open_probe = False
    probe_completed = False
    circuit_rejected = False
    probe_token = object()
    result: ProviderResult | None = None

    def sample_now() -> datetime:
        nonlocal last_known_now
        sampled = _utc(now(), callback_name="now")
        last_known_now = sampled
        return sampled

    def elapsed_ms(timestamp: datetime | None = None) -> int:
        finished = timestamp or last_known_now
        return int((finished - started).total_seconds() * 1000)

    def deadline_result(
        timestamp: datetime | None = None,
        *,
        sent_on_deadline: bool = False,
    ) -> ProviderResult:
        return ProviderResult(
            state=(
                "unknown"
                if unconfirmed_send or sent_on_deadline
                else ("failed" if attempts else "not_sent")
            ),
            error_class="deadline_exceeded",
            elapsed_ms=elapsed_ms(timestamp),
            attempts=attempts,
            retryable=True,
        )

    def complete(value: ProviderResult) -> ProviderResult:
        nonlocal result
        result = value
        return value

    def require_none(callback: Callable[[], Any], callback_name: str) -> None:
        if callback() is not None:
            raise TypeError(f"{callback_name} must return None")

    def release_owned_probe_fallback() -> None:
        """Bypass overrides and release only this invocation's exact token."""
        CircuitBreakerRegistry._release_probe_owned_by_token(
            circuits,
            context.provider,
            context.operation,
            probe_token,
        )

    def invoke_abort_probe_hook() -> None:
        """Call the hook once; the registry invariant is repaired in all exit modes."""
        try:
            require_none(
                lambda: circuits.abort_probe_with_token(
                    context.provider,
                    context.operation,
                    probe_token,
                ),
                "circuit abort_probe",
            )
        finally:
            # Also protects against an override of abort_probe_with_token itself.
            release_owned_probe_fallback()

    def abort_probe() -> None:
        nonlocal probe_completed
        if half_open_probe and not probe_completed:
            try:
                invoke_abort_probe_hook()
            finally:
                # The exact token no longer owns a probe even when the hook failed.
                probe_completed = True

    def record_success() -> None:
        nonlocal probe_completed
        require_none(
            lambda: circuits.success_with_probe_token(
                context.provider,
                context.operation,
                probe_token,
            ),
            "circuit success",
        )
        probe_completed = probe_completed or half_open_probe

    def record_failure(timestamp: datetime) -> None:
        nonlocal probe_completed
        require_none(
            lambda: circuits.failure_with_probe_token(
                context.provider,
                context.operation,
                timestamp,
                probe_token,
            ),
            "circuit failure",
        )
        probe_completed = probe_completed or half_open_probe

    def record_failure_preserving_interrupted_error(timestamp: datetime) -> None:
        """Attempt one mutation without replacing the local error already being raised."""
        try:
            record_failure(timestamp)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    def complete_with_failure(
        value: ProviderResult,
        timestamp: datetime,
        *,
        mutate_circuit: bool = True,
    ) -> ProviderResult:
        completed = complete(value)
        if mutate_circuit:
            record_failure(timestamp)
        return completed

    def complete_with_success(value: ProviderResult) -> ProviderResult:
        completed = complete(value)
        record_success()
        return completed

    def abort_possible_allow_probe() -> BaseException | None:
        """Isolate cleanup errors while always repairing this token's ownership."""
        try:
            invoke_abort_probe_hook()
        except BaseException as cleanup_error:
            # BaseException is intentionally isolated only inside cleanup. The caller
            # preserves an active error or re-raises this exact object when none exists.
            return cleanup_error
        return None

    def provider_failure_result(
        failure: ProviderFailure,
        timestamp: datetime,
        *,
        attempt_count: int,
        retryable: bool,
    ) -> ProviderResult:
        return ProviderResult(
            state="unknown" if unconfirmed_send else "failed",
            error_class=failure.error_class,
            elapsed_ms=elapsed_ms(timestamp),
            attempts=attempt_count,
            retryable=retryable,
        )

    def account_interrupted_failure(
        failure: ProviderFailure,
        timestamp: datetime,
        *,
        attempt_count: int,
        force_circuit_failure: bool = False,
    ) -> None:
        """Stop on a local callback error without erasing the provider outcome."""
        retryable = _is_retryable(failure)
        complete(
            provider_failure_result(
                failure,
                timestamp,
                attempt_count=attempt_count,
                retryable=retryable,
            )
        )
        if force_circuit_failure or retryable or half_open_probe:
            record_failure_preserving_interrupted_error(timestamp)

    def checked_recovery_after_seconds() -> int | None:
        recovery_after = circuits.recovery_after_seconds(
            context.provider,
            context.operation,
            last_known_now,
        )
        if recovery_after is None:
            return None
        if (
            isinstance(recovery_after, bool)
            or not isinstance(recovery_after, int)
            or recovery_after < 0
        ):
            raise TypeError("circuit recovery_after_seconds must return a non-negative int or None")
        return recovery_after

    def emit_telemetry(observation: ProviderCallObservation) -> None:
        require_none(lambda: telemetry.record(observation), "telemetry record")

    def checked_retry_number(value: Any, callback_name: str) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise TypeError(f"{callback_name} callback must return a finite non-negative number")
        return float(value)

    invocation_error: BaseException | None = None
    try:
        allow_completed = False
        try:
            try:
                allowed = circuits.allow_with_probe_token(
                    context.provider,
                    context.operation,
                    started,
                    probe_token,
                )
            except CircuitOpen:
                circuit_rejected = True
                raise
            if not isinstance(allowed, bool):
                raise TypeError("circuit allow must return bool")
            half_open_probe = allowed
            allow_completed = True
        finally:
            # Before local ownership handoff, cleanup by token here. Once the local
            # half-open flag is set, the outer finally is the unique cleanup owner.
            if not allow_completed and not half_open_probe:
                active_allow_error = sys.exception()
                allow_cleanup_error = abort_possible_allow_probe()
                if active_allow_error is None and allow_cleanup_error is not None:
                    raise allow_cleanup_error
        while attempts < max_attempts:
            try:
                current = sample_now()
            except asyncio.CancelledError:
                if attempts > 0 and last_failure is not None:
                    account_interrupted_failure(
                        last_failure,
                        last_known_now,
                        attempt_count=attempts,
                    )
                raise
            except Exception:
                if attempts > 0 and last_failure is not None:
                    account_interrupted_failure(
                        last_failure,
                        last_known_now,
                        attempt_count=attempts,
                    )
                raise
            if current >= deadline:
                deadline_outcome = deadline_result(current)
                if attempts > 0 and last_failure is not None:
                    # Retry sleep/clock movement crossed the deadline after a real
                    # provider failure; aggregate it exactly once before returning.
                    return complete_with_failure(deadline_outcome, current)
                return complete(deadline_outcome)
            attempts += 1
            attempt_deadline = min(
                deadline,
                current + timedelta(seconds=_MAX_PROVIDER_ATTEMPT_SECONDS),
            )
            attempt_context = replace(
                context,
                provider_call_id=_physical_provider_call_id(context.provider_call_id, attempts),
                deadline_utc=attempt_deadline,
            )
            try:
                # Provider transports must derive their request timeout from this
                # per-attempt deadline. The synchronous policy cannot safely preempt a
                # transport without risking continued background provider side effects.
                value = operation(attempt_context, request)
            except ProviderFailureAccountingAbort as accounting_abort:
                failure = accounting_abort.failure
                last_failure = failure
                unconfirmed_send = unconfirmed_send or (
                    failure.sent and failure.status_code is None
                )
                failure_time = last_known_now
                try:
                    failure_time = sample_now()
                except asyncio.CancelledError:
                    # The wrapper already holds the higher-priority terminal/hook
                    # error. Preserve it after accounting with the last valid time.
                    pass
                except Exception:
                    pass
                account_interrupted_failure(
                    failure,
                    failure_time,
                    attempt_count=attempts,
                    force_circuit_failure=accounting_abort.force_circuit_failure,
                )
                raise
            except asyncio.CancelledError:
                # Current cancellation is neutral, but a real earlier retry failure
                # remains the aggregate provider outcome for circuit and telemetry.
                prior_attempts = attempts - 1
                if prior_attempts > 0 and last_failure is not None:
                    account_interrupted_failure(
                        last_failure,
                        last_known_now,
                        attempt_count=prior_attempts,
                    )
                raise
            except ProviderPreSendDeadlineExceeded:
                # The current policy attempt never crossed the provider send boundary.
                attempts -= 1
                try:
                    deadline_time = sample_now()
                except asyncio.CancelledError:
                    if attempts > 0 and last_failure is not None:
                        account_interrupted_failure(
                            last_failure,
                            last_known_now,
                            attempt_count=attempts,
                        )
                    raise
                except Exception:
                    if attempts > 0 and last_failure is not None:
                        account_interrupted_failure(
                            last_failure,
                            last_known_now,
                            attempt_count=attempts,
                        )
                    raise
                deadline_outcome = deadline_result(deadline_time)
                if attempts > 0 and last_failure is not None:
                    return complete_with_failure(deadline_outcome, deadline_time)
                return complete(deadline_outcome)
            except ProviderPolicyAbort:
                # Local lifecycle abort is circuit-neutral. Preserve only an earlier
                # real provider failure; the current unsent attempt is not counted.
                prior_attempts = attempts - 1
                if prior_attempts > 0 and last_failure is not None:
                    account_interrupted_failure(
                        last_failure,
                        last_known_now,
                        attempt_count=prior_attempts,
                    )
                raise
            except ProviderFailure as failure:
                last_failure = failure
                unconfirmed_send = unconfirmed_send or (
                    failure.sent and failure.status_code is None
                )
                try:
                    finished = sample_now()
                except asyncio.CancelledError:
                    account_interrupted_failure(
                        failure,
                        last_known_now,
                        attempt_count=attempts,
                    )
                    raise
                except Exception:
                    account_interrupted_failure(
                        failure,
                        last_known_now,
                        attempt_count=attempts,
                    )
                    raise
                if finished >= attempt_deadline:
                    return complete_with_failure(deadline_result(finished), finished)
                retryable = _is_retryable(failure)
                if not retryable:
                    return complete_with_failure(
                        provider_failure_result(
                            failure,
                            finished,
                            attempt_count=attempts,
                            retryable=False,
                        ),
                        finished,
                        mutate_circuit=half_open_probe,
                    )
                if attempts >= max_attempts:
                    return complete_with_failure(
                        provider_failure_result(
                            failure,
                            finished,
                            attempt_count=attempts,
                            retryable=True,
                        ),
                        finished,
                    )
                try:
                    delay = checked_retry_number(policy.delay(attempts), "delay")
                    jitter_seconds = checked_retry_number(jitter(delay), "jitter")
                    sleep_seconds = min(
                        policy.max_delay_seconds,
                        delay + jitter_seconds,
                    )
                except asyncio.CancelledError:
                    account_interrupted_failure(
                        failure,
                        finished,
                        attempt_count=attempts,
                    )
                    raise
                except Exception:
                    account_interrupted_failure(
                        failure,
                        finished,
                        attempt_count=attempts,
                    )
                    raise
                remaining_seconds = (deadline - finished).total_seconds()
                if sleep_seconds >= remaining_seconds:
                    return complete_with_failure(deadline_result(finished), finished)
                try:
                    if sleep(sleep_seconds) is not None:
                        raise TypeError("sleep callback must return None")
                except asyncio.CancelledError:
                    account_interrupted_failure(
                        failure,
                        finished,
                        attempt_count=attempts,
                    )
                    raise
                except Exception:
                    account_interrupted_failure(
                        failure,
                        finished,
                        attempt_count=attempts,
                    )
                    raise
                continue
            except Exception:
                # A raw operation exception is a provider outcome. A later local
                # clock failure may propagate, but must not erase its accounting.
                try:
                    finished = sample_now()
                except asyncio.CancelledError:
                    complete(
                        ProviderResult(
                            state="unknown" if unconfirmed_send else "failed",
                            error_class="provider_error",
                            elapsed_ms=elapsed_ms(),
                            attempts=attempts,
                            retryable=False,
                        )
                    )
                    record_failure_preserving_interrupted_error(last_known_now)
                    raise
                except Exception:
                    complete(
                        ProviderResult(
                            state="unknown" if unconfirmed_send else "failed",
                            error_class="provider_error",
                            elapsed_ms=elapsed_ms(),
                            attempts=attempts,
                            retryable=False,
                        )
                    )
                    record_failure_preserving_interrupted_error(last_known_now)
                    raise
                if finished >= attempt_deadline:
                    return complete_with_failure(deadline_result(finished), finished)
                return complete_with_failure(
                    ProviderResult(
                        state="unknown" if unconfirmed_send else "failed",
                        error_class="provider_error",
                        elapsed_ms=elapsed_ms(finished),
                        attempts=attempts,
                        retryable=False,
                    ),
                    finished,
                )
            if isinstance(value, ProviderResult):
                # Reject the current malformed transport value without erasing a real
                # provider failure from an earlier retry attempt.
                prior_attempts = attempts - 1
                if prior_attempts > 0 and last_failure is not None:
                    account_interrupted_failure(
                        last_failure,
                        last_known_now,
                        attempt_count=prior_attempts,
                    )
                raise TypeError("provider transport must return a raw value, not ProviderResult")
            try:
                finished = sample_now()
            except asyncio.CancelledError:
                # Provider work already succeeded. The local policy clock failure is
                # propagated, while success closes/releases the circuit exactly once.
                complete(
                    ProviderResult(
                        state="succeeded",
                        value=value,
                        elapsed_ms=elapsed_ms(),
                        attempts=attempts,
                    )
                )
                try:
                    record_success()
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
                raise
            except Exception:
                complete(
                    ProviderResult(
                        state="succeeded",
                        value=value,
                        elapsed_ms=elapsed_ms(),
                        attempts=attempts,
                    )
                )
                try:
                    record_success()
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
                raise
            if finished >= attempt_deadline:
                return complete_with_failure(
                    deadline_result(finished, sent_on_deadline=True),
                    finished,
                )
            return complete_with_success(
                ProviderResult(
                    state="succeeded",
                    value=value,
                    elapsed_ms=elapsed_ms(finished),
                    attempts=attempts,
                )
            )
        return complete(deadline_result(last_known_now))
    except CircuitOpen as circuit_open_error:
        invocation_error = circuit_open_error
        if not circuit_rejected:
            raise
        # Only circuits.allow may classify an error as a circuit rejection.
        # Recovery and telemetry are cleanup for the active public CircuitOpen; even
        # a fatal cleanup error cannot replace that already-selected exception.
        recovery_after = None
        try:
            recovery_after = checked_recovery_after_seconds()
        except BaseException:
            pass
        try:
            emit_telemetry(
                ProviderCallObservation(
                    provider=context.provider,
                    operation=context.operation,
                    state="failed",
                    error_class="circuit_open",
                    attempts=0,
                    retry_count=0,
                    elapsed_ms=elapsed_ms(),
                    recovery_after_seconds=recovery_after,
                )
            )
        except BaseException:
            pass
        raise
    except BaseException as active_error:
        # Record only this invocation's escaping object. ``sys.exception()`` in the
        # finally block can instead expose an unrelated caller ``except`` context.
        invocation_error = active_error
        raise
    finally:
        # BaseException is isolated only in this cleanup layer. All callbacks still
        # run once; an active invocation error keeps identity, otherwise the first
        # cleanup error (including KeyboardInterrupt/SystemExit) is re-raised unchanged.
        post_result_error: BaseException | None = None
        if half_open_probe and not probe_completed:
            try:
                abort_probe()
            except BaseException as cleanup_error:
                post_result_error = cleanup_error
        if result is not None:
            recovery_after = None
            try:
                recovery_after = checked_recovery_after_seconds()
            except BaseException as cleanup_error:
                if post_result_error is None:
                    post_result_error = cleanup_error
            try:
                emit_telemetry(
                    ProviderCallObservation(
                        provider=context.provider,
                        operation=context.operation,
                        state=result.state,
                        error_class=result.error_class,
                        attempts=result.attempts,
                        retry_count=max(0, result.attempts - 1),
                        elapsed_ms=result.elapsed_ms,
                        recovery_after_seconds=recovery_after,
                    )
                )
            except BaseException as cleanup_error:
                if post_result_error is None:
                    post_result_error = cleanup_error
        if invocation_error is None and post_result_error is not None:
            raise post_result_error
