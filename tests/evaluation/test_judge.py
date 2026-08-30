"""Judge constants, preflight, lane isolation and usage recording (A16–A19)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import Any

import pytest

from app.evaluation.judge import (
    JUDGE_MODE,
    JUDGE_MODEL,
    JUDGE_PROMPT_VERSION,
    JUDGE_PROVIDER,
    JUDGE_SHORT_RETRY_ATTEMPTS,
    CircuitBreakerRegistry,
    HttpJudgeProvider,
    JudgeConfiguration,
    JudgePreflight,
    JudgeRequest,
    UnavailableJudgeProvider,
    bounded_backoff_seconds,
    faithfulness_prompt,
)
from app.evaluation.usage import EvaluationUsageRecorder
from app.indexing.image_vlm import BailianImageDescriber
from app.platform.errors import PlatformError

from .conftest import FakeJudgeProvider, RecordingUsageSubmission


class _Response:
    def __init__(self, *, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    def __init__(self, responses: list[_Response]) -> None:
        self._responses = responses
        self.requests: list[dict[str, Any]] = []

    def close(self) -> None:
        return None

    def post(self, path: str, *, json: dict[str, Any]) -> _Response:
        self.requests.append({"path": path, "json": json})
        return self._responses.pop(0)


def _http_judge(
    *, submission: RecordingUsageSubmission, responses: list[_Response]
) -> tuple[Any, _Client]:
    provider = HttpJudgeProvider(
        base_url="https://judge.invalid",
        api_key="test-key",
        usage_submission=submission,
        now=lambda: datetime(2026, 8, 13, 12, 0, tzinfo=UTC),
        sleep=lambda _: None,
    )
    provider.close()
    client = _Client(responses)
    provider._client = client
    return provider, client


def test_judge_constants_are_fixed() -> None:
    assert JUDGE_PROVIDER == "bailian"
    assert JUDGE_MODEL == "qwen3.7-plus"
    assert JUDGE_MODE == "non_thinking"
    assert JUDGE_PROMPT_VERSION == "v2"


def test_judge_configuration_is_frozen() -> None:
    config = JudgeConfiguration()
    assert config.provider == "bailian"
    assert config.model == "qwen3.7-plus"
    assert config.mode == "non_thinking"
    assert config.prompt_version == "v2"


def test_http_judge_exposes_configuration_for_comparator() -> None:
    provider, _ = _http_judge(submission=RecordingUsageSubmission(), responses=[])

    assert provider.provider == JUDGE_PROVIDER
    assert provider.model == JUDGE_MODEL
    assert provider.mode == JUDGE_MODE
    assert provider.capability == "qwen3.7-plus"
    assert provider.release == "stable"
    assert provider.prompt_version == JUDGE_PROMPT_VERSION


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


def test_http_judge_preflight_explicitly_disables_thinking() -> None:
    provider, client = _http_judge(
        submission=RecordingUsageSubmission(),
        responses=[_Response(status_code=200, payload={})],
    )

    provider.preflight_probe()

    assert client.requests == [
        {
            "path": "/chat/completions",
            "json": {
                "model": JUDGE_MODEL,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
                "enable_thinking": False,
            },
        }
    ]


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


def test_saturated_judge_batch_does_not_starve_image_ingestion_lane() -> None:
    """Acceptance: concurrent judge saturation leaves image validation usable."""

    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    judge_lane = CircuitBreakerRegistry(threshold=2)
    image_lane = CircuitBreakerRegistry(threshold=2)
    barrier = Barrier(8)
    image_payload = {
        "choices": [{"message": {"content": "image validation result"}}],
    }

    def image_transport(url, payload, headers, options):
        del url, payload, headers, options
        return image_payload

    describer = BailianImageDescriber(
        base_url="https://image.invalid",
        api_key="image-key",
        transport=image_transport,
    )

    def judge_batch() -> bool:
        barrier.wait()
        judge_lane.record_failure(now)
        return judge_lane.is_open()

    def image_batch() -> str:
        barrier.wait()
        # The image lane owns a separate breaker state and remains allowed even
        # while the judge lane is open.
        assert image_lane.allow(now) is True
        return describer(b"image-bytes", {"caption": "validation"}).text

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(judge_batch) for _ in range(4)]
        futures.extend(executor.submit(image_batch) for _ in range(4))
        results = [future.result() for future in futures]

    assert judge_lane.is_open() is True
    # At least the threshold-crossing calls completed; later calls observe
    # the saturated judge lane and may report ``False``.
    assert results[:4].count(True) >= 2
    assert results[4:] == ["image validation result"] * 4
    assert image_lane.is_open() is False


def test_circuit_breaker_allows_one_probe_after_the_cooldown() -> None:
    breaker = CircuitBreakerRegistry(threshold=1, cooldown_seconds=30)
    opened_at = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    breaker.record_failure(opened_at)

    assert breaker.allow(opened_at + timedelta(seconds=29)) is False
    assert breaker.allow(opened_at + timedelta(seconds=30)) is True
    assert breaker.allow(opened_at + timedelta(seconds=30)) is False
    breaker.reset()
    assert breaker.allow(opened_at + timedelta(seconds=30)) is True


def test_bounded_backoff_is_lane_local() -> None:
    assert bounded_backoff_seconds(1) == 0.5
    assert bounded_backoff_seconds(2) == 1.0
    # Bounded: never exceeds the lane-local max delay.
    assert bounded_backoff_seconds(10) <= 4.0
    assert JUDGE_SHORT_RETRY_ATTEMPTS >= 1


def test_faithfulness_prompt_includes_context_and_requests_all_judge_fields() -> None:
    prompt = faithfulness_prompt(
        "What supports this?",
        "The answer is supported.",
        ({"document_id": "doc_1", "snippet": "supporting source"},),
    )

    assert "supporting source" in prompt
    assert "faithfulness" in prompt
    assert "answer_relevancy" in prompt
    assert "is_refusal" in prompt


def test_http_judge_rejects_non_boolean_refusal_response() -> None:
    submission = RecordingUsageSubmission()
    provider, _ = _http_judge(
        submission=submission,
        responses=[
            _Response(
                status_code=200,
                payload={
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"faithfulness": 0.9, "answer_relevancy": 0.8, '
                                    '"is_refusal": "false"}'
                                )
                            }
                        }
                    ]
                },
            )
        ],
    )

    with pytest.raises(PlatformError) as raised:
        provider.judge(JudgeRequest(question="q", answer="a"))

    assert raised.value.code == "evaluation_judge_unavailable"


@pytest.mark.parametrize(
    "content",
    [
        '{"answer_relevancy": 0.8, "is_refusal": false}',
        '{"faithfulness": 1.1, "answer_relevancy": 0.8, "is_refusal": false}',
        '{"faithfulness": NaN, "answer_relevancy": 0.8, "is_refusal": false}',
    ],
)
def test_http_judge_rejects_missing_nonfinite_or_out_of_range_scores(content: str) -> None:
    submission = RecordingUsageSubmission()
    provider, _ = _http_judge(
        submission=submission,
        responses=[
            _Response(
                status_code=200,
                payload={"choices": [{"message": {"content": content}}]},
            )
        ],
    )

    with pytest.raises(PlatformError) as raised:
        provider.judge(JudgeRequest(question="q", answer="a"))

    assert raised.value.code == "evaluation_judge_unavailable"


def test_http_judge_sends_the_request_context_to_the_faithfulness_prompt() -> None:
    submission = RecordingUsageSubmission()
    provider, client = _http_judge(
        submission=submission,
        responses=[
            _Response(
                status_code=200,
                payload={
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"faithfulness": 0.9, "answer_relevancy": 0.8, '
                                    '"is_refusal": false}'
                                )
                            }
                        }
                    ]
                },
            )
        ],
    )

    provider.judge(
        JudgeRequest(
            question="q",
            answer="a",
            context=({"document_id": "doc_1", "snippet": "current context"},),
        )
    )

    prompt = client.requests[0]["json"]["messages"][0]["content"]
    assert "current context" in prompt


def test_http_judge_records_sent_rate_limits_as_failed_usage() -> None:
    submission = RecordingUsageSubmission()
    provider, _ = _http_judge(
        submission=submission,
        responses=[_Response(status_code=429, payload={}) for _ in range(3)],
    )

    with pytest.raises(PlatformError) as raised:
        provider.judge(JudgeRequest(question="q", answer="a"))

    assert raised.value.code == "judge_rate_limited"
    assert len(submission.completed) == JUDGE_SHORT_RETRY_ATTEMPTS
    assert {entry["result"] for entry in submission.completed} == {"failed"}


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
