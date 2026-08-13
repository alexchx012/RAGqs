"""Judge constants, preflight, lane isolation and usage recording (A16–A19)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.evaluation.judge import (
    JUDGE_MODE,
    JUDGE_MODEL,
    JUDGE_PROMPT_VERSION,
    JUDGE_PROVIDER,
    JUDGE_SHORT_RETRY_ATTEMPTS,
    CircuitBreakerRegistry,
    JudgeConfiguration,
    JudgePreflight,
    JudgeRequest,
    UnavailableJudgeProvider,
    bounded_backoff_seconds,
)
from app.evaluation.usage import EvaluationUsageRecorder
from app.platform.errors import PlatformError

from .conftest import FakeJudgeProvider, RecordingUsageSubmission


def test_judge_constants_are_fixed() -> None:
    assert JUDGE_PROVIDER == "bailian"
    assert JUDGE_MODEL == "qwen3.7-plus"
    assert JUDGE_MODE == "non_thinking"
    assert JUDGE_PROMPT_VERSION == "v1"


def test_judge_configuration_is_frozen() -> None:
    config = JudgeConfiguration()
    assert config.provider == "bailian"
    assert config.model == "qwen3.7-plus"
    assert config.mode == "non_thinking"
    assert config.prompt_version == "v1"


def test_unavailable_judge_fails_closed_on_preflight() -> None:
    provider = UnavailableJudgeProvider(environment="development")
    with pytest.raises(PlatformError) as raised:
        provider.preflight_probe()
    assert raised.value.code == "evaluation_judge_unavailable"
    assert raised.value.status_code == 503


def test_preflight_verify_startup_raises_runtime_error() -> None:
    preflight = JudgePreflight(UnavailableJudgeProvider(environment="development"))
    with pytest.raises(RuntimeError):
        preflight.verify_startup()


def test_preflight_verify_run_raises_platform_error() -> None:
    preflight = JudgePreflight(UnavailableJudgeProvider(environment="development"))
    with pytest.raises(PlatformError) as raised:
        preflight.verify_run()
    assert raised.value.code == "evaluation_judge_unavailable"


def test_circuit_breaker_is_lane_private_and_bounded() -> None:
    breaker = CircuitBreakerRegistry(threshold=3)
    assert breaker.failures == 0
    assert breaker.is_open() is False
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    breaker.record_failure(now)
    breaker.record_failure(now)
    assert breaker.is_open() is False
    breaker.record_failure(now)
    assert breaker.is_open() is True
    breaker.reset()
    assert breaker.is_open() is False
    # A freshly created instance is independent from any other lane.
    assert CircuitBreakerRegistry().failures == 0


def test_bounded_backoff_is_lane_local() -> None:
    assert bounded_backoff_seconds(1) == 0.5
    assert bounded_backoff_seconds(2) == 1.0
    # Bounded: never exceeds the lane-local max delay.
    assert bounded_backoff_seconds(10) <= 4.0
    assert JUDGE_SHORT_RETRY_ATTEMPTS >= 1


def test_usage_recorder_records_every_real_send_without_quota() -> None:
    submission = RecordingUsageSubmission()
    recorder = EvaluationUsageRecorder(
        submission=submission,
        provider="bailian",
        model="qwen3.7-plus",
        run_id="run_1",
        attempt_id="run_1:1",
        actor_user_id="ops_1",
        deadline_utc=datetime(2026, 8, 13, 13, 0, tzinfo=UTC),
        started_at=lambda: datetime(2026, 8, 13, 12, 0, tzinfo=UTC),
    )
    recorder.record_call(
        resource_id="sample_1",
        request_fingerprint="fingerprint_1",
        send=lambda: "ok",
    )
    assert len(submission.prepared) == 1
    assert len(submission.completed) == 1
    prepared = submission.prepared[0]
    assert prepared["execution_kind"] == "shadow_evaluation"
    assert prepared["operation"] == "evaluation_judge"
    assert prepared["execution_id"] == "run_1"
    assert prepared["attempt_id"] == "run_1:1"
    completed = submission.completed[0]
    assert completed["ownership"].cost_center_key == "system:evaluation"
    # No quota debit field is ever present.
    assert all("quota_debit" not in entry for entry in submission.prepared)
    assert all("quota_debit" not in entry for entry in submission.completed)


def test_usage_recorder_no_call_when_deadline_pre_rejected() -> None:
    submission = RecordingUsageSubmission()
    recorder = EvaluationUsageRecorder(
        submission=submission,
        provider="bailian",
        model="qwen3.7-plus",
        run_id="run_1",
        attempt_id="run_1:1",
        actor_user_id="ops_1",
        deadline_utc=datetime(2026, 8, 13, 12, 0, tzinfo=UTC),
        started_at=lambda: datetime(2026, 8, 13, 12, 1, tzinfo=UTC),
    )
    with pytest.raises(PlatformError) as raised:
        recorder.record_call(
            resource_id="sample_1",
            request_fingerprint="fingerprint_1",
            send=lambda: "ok",
        )
    assert raised.value.code == "evaluation_judge_deadline_exceeded"
    # A pre-send deadline rejection must not fabricate usage (A19).
    assert submission.prepared == []
    assert submission.completed == []


def test_usage_recorder_no_call_when_not_sent() -> None:
    submission = RecordingUsageSubmission()
    # A fail-closed judge preflight path must never fabricate usage.
    with pytest.raises(PlatformError):
        UnavailableJudgeProvider(environment="development").preflight_probe()
    assert submission.prepared == []
    assert submission.completed == []


def test_fake_judge_provider_is_injectable() -> None:
    provider = FakeJudgeProvider()
    request = JudgeRequest(question="q", answer="a")
    scores = provider.judge(request)
    assert scores.faithfulness == 0.9
    assert provider.calls == [request]


def test_judge_request_carries_run_attribution() -> None:
    request = JudgeRequest(
        question="q",
        answer="a",
        run_id="run_1",
        attempt_id="run_1:2",
        actor_user_id="ops_1",
        deadline_utc=datetime(2026, 8, 13, 13, 0, tzinfo=UTC),
    )
    assert request.run_id == "run_1"
    assert request.attempt_id == "run_1:2"
    assert request.actor_user_id == "ops_1"
    assert request.deadline_utc == datetime(2026, 8, 13, 13, 0, tzinfo=UTC)
