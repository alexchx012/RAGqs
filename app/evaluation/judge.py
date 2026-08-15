"""LLM-as-a-Judge configuration, transport and preflight.

The judge lane is fully isolated from online generation and image VLM lanes:
its own circuit-breaker registry and retry budget ensure judge saturation or a
429 burst never borrows from another lane (A17/A18). Every real provider send —
including each bounded short retry — is recorded through the usage/quota port
with the real evaluation run/attempt attribution (A19).
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock
from typing import Any, Protocol

from app.platform.errors import PlatformError

from .models import JudgeScores
from .usage import EvaluationUsageRecorder

JUDGE_PROVIDER = "bailian"
JUDGE_MODEL = "qwen3.7-plus"
JUDGE_MODE = "non_thinking"
JUDGE_PROMPT_VERSION = "v2"

# Bounded short-retry budget for one judge call; the run-level retry_wait loop
# (policy max_attempts) sits on top of this lane-local budget (A18).
JUDGE_SHORT_RETRY_ATTEMPTS = 3
JUDGE_RETRY_BASE_DELAY_SECONDS = 0.5
JUDGE_RETRY_MAX_DELAY_SECONDS = 4.0
JUDGE_BREAKER_THRESHOLD = 3
JUDGE_BREAKER_COOLDOWN_SECONDS = 30


def bounded_backoff_seconds(attempt: int) -> float:
    """Lane-local exponential backoff, bounded (A18)."""
    delay = JUDGE_RETRY_BASE_DELAY_SECONDS * (2 ** max(0, attempt - 1))
    return min(delay, JUDGE_RETRY_MAX_DELAY_SECONDS)


@dataclass(frozen=True, slots=True)
class JudgeConfiguration:
    provider: str = JUDGE_PROVIDER
    model: str = JUDGE_MODEL
    mode: str = JUDGE_MODE
    prompt_version: str = JUDGE_PROMPT_VERSION
    k: int = 5
    credential_ref: str = "judge"
    capability: str = "qwen3.7-plus"
    release: str = "stable"


@dataclass(frozen=True, slots=True)
class JudgeRequest:
    question: str
    answer: str
    context: tuple[Mapping[str, Any], ...] = ()
    expected_sources: tuple[str, ...] = ()
    expects_refusal: bool | None = None
    # Usage attribution for A19; preflight/fakes leave these unset.
    run_id: str | None = None
    attempt_id: str | None = None
    actor_user_id: str | None = None
    deadline_utc: datetime | None = None

    def fingerprint(self) -> str:
        payload = {
            "question": self.question,
            "answer": self.answer,
            "context": [dict(item) for item in self.context],
            "expected_sources": list(self.expected_sources),
            "expects_refusal": self.expects_refusal,
        }
        encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(b"evaluation-judge-request-v1\0" + encoded.encode()).hexdigest()


class JudgeProviderPort(Protocol):
    def preflight_probe(self) -> None: ...

    def judge(self, request: JudgeRequest) -> JudgeScores: ...


class UnavailableJudgeProvider:
    """Fail-closed placeholder for development/test environments (A17)."""

    def __init__(self, environment: str = "development") -> None:
        self._environment = environment

    def preflight_probe(self) -> None:
        raise PlatformError(
            "evaluation_judge_unavailable",
            "The evaluation judge is not configured",
            {"retryable": True},
            503,
            True,
        )

    def judge(self, request: JudgeRequest) -> JudgeScores:
        del request
        raise PlatformError(
            "evaluation_judge_unavailable",
            "The evaluation judge is not configured",
            {"retryable": True},
            503,
            True,
        )


class CircuitBreakerRegistry:
    """Lane-private circuit breaker state (A17/A18).

    Kept deliberately small: the judge lane must never share its breaker with
    the online generation or image VLM lanes.
    """

    def __init__(
        self,
        *,
        threshold: int = JUDGE_BREAKER_THRESHOLD,
        cooldown_seconds: int = JUDGE_BREAKER_COOLDOWN_SECONDS,
    ) -> None:
        self.failures = 0
        self.opened_at: datetime | None = None
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self._half_open_probe = False
        self._lock = Lock()

    def record_failure(self, now: datetime) -> bool:
        """Record a failed call and return whether it was the half-open probe."""

        with self._lock:
            was_half_open_probe = self._half_open_probe
            self._half_open_probe = False
            if self.opened_at is not None:
                self.opened_at = now
                self.failures = max(self.failures, self.threshold)
                return was_half_open_probe
            self.failures += 1
            if self.failures >= self.threshold:
                self.opened_at = now
            return was_half_open_probe

    def reset(self) -> None:
        with self._lock:
            self.failures = 0
            self.opened_at = None
            self._half_open_probe = False

    def allow(self, now: datetime) -> bool:
        """Reject during cooldown, then grant exactly one recovery probe."""

        with self._lock:
            if self.opened_at is None:
                return True
            if now < self.opened_at + timedelta(seconds=self.cooldown_seconds):
                return False
            if self._half_open_probe:
                return False
            self._half_open_probe = True
            return True

    def abort_probe(self) -> None:
        with self._lock:
            self._half_open_probe = False

    def is_open(self) -> bool:
        with self._lock:
            return self.opened_at is not None


def faithfulness_prompt(
    question: str,
    answer: str,
    context: tuple[Mapping[str, Any], ...],
) -> str:
    serialized_context = json.dumps(
        [dict(item) for item in context],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (
        "You are evaluating answer faithfulness. Judge whether every claim in the "
        "answer is supported by the provided context. Return a JSON object with "
        "numeric `faithfulness` and `answer_relevancy` values between 0 and 1, "
        "and boolean `is_refusal`.\n\n"
        f"Question: {question}\n\nContext: {serialized_context}\n\nAnswer: {answer}\n"
    )


def answer_relevancy_prompt(question: str, answer: str) -> str:
    return (
        "You are evaluating answer relevancy. Judge how directly the answer addresses "
        "the question without unrelated content. Return the same JSON object schema: "
        "numeric `faithfulness` and `answer_relevancy` values between 0 and 1, and "
        f"boolean `is_refusal`.\n\nQuestion: {question}\n\nAnswer: {answer}\n"
    )


class HttpJudgeProvider:
    """Bailian OpenAI-compatible judge transport with per-send usage recording."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        usage_submission: Any,
        configuration: JudgeConfiguration | None = None,
        model: str | None = None,
        timeout_seconds: float = 30.0,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        import httpx

        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
        )
        self._configuration = configuration or JudgeConfiguration(model=model or JUDGE_MODEL)
        self._usage_submission = usage_submission
        self._now = now or (lambda: datetime.now().astimezone())
        self._sleep = sleep or time.sleep
        self._breaker = CircuitBreakerRegistry()

    @property
    def provider(self) -> str:
        return self._configuration.provider

    @property
    def model(self) -> str:
        return self._configuration.model

    @property
    def mode(self) -> str:
        return self._configuration.mode

    @property
    def capability(self) -> str:
        return self._configuration.capability

    @property
    def release(self) -> str:
        return self._configuration.release

    @property
    def prompt_version(self) -> str:
        return self._configuration.prompt_version

    def close(self) -> None:
        self._client.close()

    def dispose(self) -> None:
        self.close()

    def _recorder(
        self,
        request: JudgeRequest,
        *,
        fallback_run_id: str,
        fallback_attempt_id: str,
        deadline_utc: datetime,
    ) -> EvaluationUsageRecorder:
        return EvaluationUsageRecorder(
            submission=self._usage_submission,
            provider=self._configuration.provider,
            model=self._configuration.model,
            run_id=request.run_id or fallback_run_id,
            attempt_id=request.attempt_id or fallback_attempt_id,
            actor_user_id=request.actor_user_id or "system:evaluation",
            deadline_utc=deadline_utc,
            started_at=self._now,
        )

    @staticmethod
    def _usage_result(response: Any) -> str:
        return "failed" if int(getattr(response, "status_code", 500)) >= 400 else "succeeded"

    def preflight_probe(self) -> None:
        """Authentication/capability probe with judge-only credentials (A17).

        The probe is a control-plane send without a business run, recorded with
        the ``judge_preflight`` attribution so every real send is metered (A19).
        """
        now = self._now()
        recorder = EvaluationUsageRecorder(
            submission=self._usage_submission,
            provider=self._configuration.provider,
            model=self._configuration.model,
            run_id="judge_preflight",
            attempt_id="preflight",
            actor_user_id="system:evaluation",
            deadline_utc=now + timedelta(seconds=30),
            started_at=self._now,
        )
        try:
            response = recorder.record_call(
                resource_id="judge-preflight",
                request_fingerprint="judge-preflight-v1",
                send=lambda: self._client.post(
                    "/chat/completions",
                    json={
                        "model": self._configuration.model,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 1,
                    },
                ),
                result_for=self._usage_result,
            )
            response.raise_for_status()
        except PlatformError:
            raise
        except Exception as error:  # noqa: BLE001 - preflight maps any transport fault
            raise PlatformError(
                "evaluation_judge_unavailable",
                "The evaluation judge preflight failed",
                {"retryable": True},
                503,
                True,
            ) from error

    def judge(self, request: JudgeRequest) -> JudgeScores:
        if not self._breaker.allow(self._now()):
            raise PlatformError(
                "judge_rate_limited",
                "The judge lane circuit is open",
                {"retryable": False},
                429,
                False,
            )
        started = self._now()
        deadline = request.deadline_utc or (started + timedelta(seconds=60))
        recorder = self._recorder(
            request,
            fallback_run_id="judge_unattributed",
            fallback_attempt_id="judge",
            deadline_utc=deadline,
        )
        for attempt in range(1, JUDGE_SHORT_RETRY_ATTEMPTS + 1):
            try:
                response = recorder.record_call(
                    resource_id=request.fingerprint(),
                    request_fingerprint=request.fingerprint(),
                    send=lambda: self._client.post(
                        "/chat/completions",
                        json={
                            "model": self._configuration.model,
                            "messages": [
                                {
                                    "role": "user",
                                    "content": faithfulness_prompt(
                                        request.question, request.answer, request.context
                                    ),
                                },
                                {
                                    "role": "user",
                                    "content": answer_relevancy_prompt(
                                        request.question, request.answer
                                    ),
                                },
                            ],
                            "max_tokens": 256,
                        },
                    ),
                    result_for=self._usage_result,
                )
            except PlatformError as error:
                if error.code == "evaluation_judge_deadline_exceeded":
                    self._breaker.abort_probe()
                    raise
                self._breaker.record_failure(self._now())
                raise
            status_code = getattr(response, "status_code", 0)
            if status_code == 429:
                # 429 consumes only the judge lane's bounded budget (A18).
                probe_failed = self._breaker.record_failure(self._now())
                if probe_failed:
                    raise PlatformError(
                        "judge_rate_limited",
                        "The judge provider is rate limited",
                        {"retryable": True},
                        429,
                        True,
                    )
                if attempt < JUDGE_SHORT_RETRY_ATTEMPTS:
                    remaining = (deadline - self._now()).total_seconds()
                    if remaining <= 0:
                        raise PlatformError(
                            "evaluation_judge_deadline_exceeded",
                            "The judge call crossed its deadline",
                            {"retryable": False},
                            408,
                            False,
                        )
                    self._sleep(min(bounded_backoff_seconds(attempt), remaining))
                    continue
                raise PlatformError(
                    "judge_rate_limited",
                    "The judge provider is rate limited",
                    {"retryable": True},
                    429,
                    True,
                )
            if status_code >= 400:
                self._breaker.record_failure(self._now())
                raise PlatformError(
                    "evaluation_judge_unavailable",
                    "The judge provider rejected the call",
                    {"retryable": True},
                    503,
                    True,
                )
            try:
                body = self._parse_json(response)
                faithfulness = self._score_value(body, "faithfulness")
                answer_relevancy = self._score_value(body, "answer_relevancy")
                refusal = body.get("is_refusal")
                if not isinstance(refusal, bool):
                    raise self._invalid_response("is_refusal must be a boolean")
            except PlatformError:
                self._breaker.record_failure(self._now())
                raise
            self._breaker.reset()
            return JudgeScores(
                faithfulness=faithfulness,
                answer_relevancy=answer_relevancy,
                is_refusal=refusal,
                latency_ms=int((self._now() - started).total_seconds() * 1000),
            )
        raise PlatformError(
            "judge_rate_limited",
            "The judge lane retry budget is exhausted",
            {"retryable": True},
            429,
            True,
        )

    @staticmethod
    def _invalid_response(message: str) -> PlatformError:
        return PlatformError(
            "evaluation_judge_unavailable",
            message,
            {"retryable": True},
            503,
            True,
        )

    @staticmethod
    def _score_value(body: Mapping[str, Any], name: str) -> float:
        value = body.get(name)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0.0 <= value <= 1.0
        ):
            raise HttpJudgeProvider._invalid_response(
                f"{name} must be a finite number between 0 and 1"
            )
        return float(value)

    @staticmethod
    def _parse_json(response: Any) -> Mapping[str, Any]:
        try:
            payload = response.json()
        except (TypeError, ValueError) as error:
            raise HttpJudgeProvider._invalid_response(
                "The judge response is not valid JSON"
            ) from error
        if not isinstance(payload, Mapping):
            raise HttpJudgeProvider._invalid_response("The judge response must be a JSON object")
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
            content = message.get("content") if isinstance(message, Mapping) else None
            if not isinstance(content, str) or not content.strip():
                raise HttpJudgeProvider._invalid_response("The judge response content is missing")
            try:
                decoded = json.loads(content)
            except (TypeError, ValueError) as error:
                raise HttpJudgeProvider._invalid_response(
                    "The judge response is not valid JSON"
                ) from error
            if not isinstance(decoded, Mapping):
                raise HttpJudgeProvider._invalid_response(
                    "The judge response must be a JSON object"
                )
            return decoded
        return payload


class JudgePreflight:
    """Runs the judge preflight before startup or run creation (A17)."""

    def __init__(self, provider: JudgeProviderPort) -> None:
        self._provider = provider

    def verify_startup(self) -> None:
        try:
            self._provider.preflight_probe()
        except PlatformError as error:
            raise RuntimeError("evaluation judge preflight failed") from error

    def verify_run(self) -> None:
        self._provider.preflight_probe()


__all__ = [
    "CircuitBreakerRegistry",
    "HttpJudgeProvider",
    "JUDGE_BREAKER_THRESHOLD",
    "JUDGE_MODE",
    "JUDGE_MODEL",
    "JUDGE_PROMPT_VERSION",
    "JUDGE_PROVIDER",
    "JUDGE_SHORT_RETRY_ATTEMPTS",
    "JudgeConfiguration",
    "JudgePreflight",
    "JudgeProviderPort",
    "JudgeRequest",
    "UnavailableJudgeProvider",
    "answer_relevancy_prompt",
    "bounded_backoff_seconds",
    "faithfulness_prompt",
]
