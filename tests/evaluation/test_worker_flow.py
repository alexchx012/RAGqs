"""Worker golden matching, real metrics, retry classification (A12/A13/A14/A19)."""

from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.evaluation.policy import default_policy_snapshot
from app.evaluation.schema import shadow_evaluation_run_table
from app.evaluation.worker import ShadowEvaluationWorker
from app.platform.errors import PlatformError

from .conftest import (
    NOW,
    FakeAnswerReplayPort,
    FakeRetrievalReplayPort,
    build_test_env,
)


def _insert_run(
    env,
    *,
    run_id: str = "run_1",
    sample_items=(),
    candidate_configs=("default",),
    golden_version: str | None = None,
    snapshot_id: str = "snap_1",
    idempotency_key: str = "key_1",
) -> str:
    repo = env["runtime"].resolve("evaluation_repository")
    policy = default_policy_snapshot(now=NOW)
    frozen = {
        "snapshot_id": snapshot_id,
        "policy_version": policy.policy_version,
        "initiator_user_id": "ops_1",
    }
    if golden_version:
        frozen["golden_set_version"] = golden_version
    with env["engine"].begin() as connection:
        repo.ensure_policy(connection, policy=policy)
        repo.insert_run(
            connection,
            run_id=run_id,
            space_id="space_1",
            policy_version=policy.policy_version,
            comparator_key="cmp_1",
            candidate_config_versions=candidate_configs,
            index_generation_id="gen_1",
            index_revision=1,
            frozen_snapshot=frozen,
            snapshot_id=snapshot_id,
            sample_items=sample_items,
            now=NOW,
            initiator_user_id="ops_1",
            request_hash="hash_1",
            idempotency_key=idempotency_key,
        )
    return run_id


def _state(env, run_id: str) -> str:
    with env["engine"].connect() as connection:
        return str(
            connection.execute(
                select(shadow_evaluation_run_table.c.state).where(
                    shadow_evaluation_run_table.c.run_id == run_id
                )
            ).scalar_one()
        )


def _attempt(env, run_id: str) -> int:
    with env["engine"].connect() as connection:
        return int(
            connection.execute(
                select(shadow_evaluation_run_table.c.attempt).where(
                    shadow_evaluation_run_table.c.run_id == run_id
                )
            ).scalar_one()
        )


class RetryableJudge:
    """Judge fake that raises a retryable platform error."""

    def __init__(self, *, code: str = "judge_rate_limited", status: int = 429) -> None:
        self.code = code
        self.status = status
        self.calls = 0

    def preflight_probe(self) -> None:
        return None

    def judge(self, request):
        self.calls += 1
        raise PlatformError(self.code, "judge failed", {"retryable": True}, self.status, True)


class NonRetryableJudge:
    def __init__(self) -> None:
        self.calls = 0

    def preflight_probe(self) -> None:
        return None

    def judge(self, request):
        self.calls += 1
        raise PlatformError("evaluation_judge_transport_error", "broken", {}, 500, False)


class AttributionJudge:
    """Records the run attribution the worker passed on JudgeRequest (A19)."""

    def __init__(self, *, scores=None):
        from app.evaluation.models import JudgeScores

        self.scores = scores or JudgeScores(
            faithfulness=0.9, answer_relevancy=0.8, is_refusal=False, latency_ms=50
        )
        self.requests = []

    def preflight_probe(self) -> None:
        return None

    def judge(self, request):
        self.requests.append(request)
        return self.scores


class HitRetrieval:
    def __init__(self) -> None:
        self.calls = []

    def replay(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "session_id": kwargs["session_id"],
            "candidate_config_version": kwargs["candidate_config_version"],
            "hits": (
                {"document_id": "s1", "snippet": "x"},
                {"document_id": "s2", "snippet": "y"},
            ),
            "degradations": (),
        }


def _worker(env, judge, retrieval=None):
    repo = env["runtime"].resolve("evaluation_repository")
    return ShadowEvaluationWorker(
        env["engine"],
        repo,
        judge,
        retrieval or FakeRetrievalReplayPort(),
        answer_replay=FakeAnswerReplayPort(),
        now=lambda: NOW,
    )


def _sample(question: str = "question 1", question_hash: str = "hash_1"):
    return {
        "item_id": "item_1",
        "position": 1,
        "question_text": question,
        "question_hash": question_hash,
        "evidence_hash": "ev_1",
        "weak_signals": {},
        "source_ref": "msg_1",
    }


# ---------------------------------------------------------------- golden (A13)


def test_golden_set_publish_is_versioned_and_immutable() -> None:
    env = build_test_env()
    repo = env["runtime"].resolve("evaluation_repository")
    service = env["runtime"].resolve("evaluation_service")
    items_v1 = (
        {
            "question_text": "q1",
            "expected_sources": ["s1"],
            "expects_refusal": False,
        },
    )
    service.publish_golden_set(space_id="space_1", version="gv1", items=items_v1)
    with env["engine"].begin() as connection:
        assert repo.latest_golden_set_version(connection, space_id="space_1") == "gv1"
        stored = repo.list_golden_items(connection, space_id="space_1", golden_version="gv1")
    assert len(stored) == 1
    assert stored[0]["expected_sources_json"] == ["s1"]
    assert stored[0]["question_hash"] == hashlib.sha256(b"q1").hexdigest()
    # A revision creates a NEW version row; v1 facts stay untouched.
    items_v2 = (
        {
            "question_text": "q1",
            "expected_sources": ["s1", "s9"],
            "expects_refusal": False,
        },
    )
    service.publish_golden_set(space_id="space_1", version="gv2", items=items_v2)
    with env["engine"].begin() as connection:
        assert repo.latest_golden_set_version(connection, space_id="space_1") == "gv2"
        v1 = repo.list_golden_items(connection, space_id="space_1", golden_version="gv1")
        v2 = repo.list_golden_items(connection, space_id="space_1", golden_version="gv2")
    assert v1[0]["expected_sources_json"] == ["s1"]
    assert v2[0]["expected_sources_json"] == ["s1", "s9"]


def test_golden_set_rejects_missing_refusal_label() -> None:
    env = build_test_env()
    service = env["runtime"].resolve("evaluation_service")

    with pytest.raises(PlatformError) as raised:
        service.publish_golden_set(
            space_id="space_1",
            version="gv1",
            items=({"question_text": "q1", "expected_sources": ["s1"]},),
        )

    assert raised.value.code == "validation_error"


# ------------------------------------------------------- real metrics (A14)


def test_worker_computes_real_retrieval_metrics_from_golden() -> None:
    env = build_test_env()
    question = "golden question"
    question_hash = hashlib.sha256(question.encode("utf-8")).hexdigest()
    service = env["runtime"].resolve("evaluation_service")
    service.publish_golden_set(
        space_id="space_1",
        version="gv1",
        items=(
            {
                "question_text": question,
                "expected_sources": ["s1", "s2"],
                "expects_refusal": False,
            },
        ),
    )
    _insert_run(
        env,
        sample_items=(_sample(question=question, question_hash=question_hash),),
        golden_version="gv1",
    )
    judge = AttributionJudge()
    worker = _worker(env, judge, retrieval=HitRetrieval())
    worker.run_once()
    repo = env["runtime"].resolve("evaluation_repository")
    with env["engine"].connect() as connection:
        results = repo.list_results(connection, run_id="run_1")
    assert len(results) == 1
    metrics = results[0]["metrics_json"]
    assert metrics["hit_at_k_candidate"] == 1.0
    assert metrics["hit_at_k_final"] == 1.0
    assert metrics["mrr"] == 1.0
    assert metrics["ndcg_at_k"] > 0
    assert metrics["refusal_rate"] == 1.0
    # The golden sample's expected sources reached the judge request (A14).
    assert judge.requests[0].expected_sources == ("s1", "s2")


def test_worker_passes_retrieval_hits_as_answer_replay_context() -> None:
    env = build_test_env()
    _insert_run(env, sample_items=(_sample(),))
    answer_replay = FakeAnswerReplayPort()
    repo = env["runtime"].resolve("evaluation_repository")
    worker = ShadowEvaluationWorker(
        env["engine"],
        repo,
        AttributionJudge(),
        HitRetrieval(),
        answer_replay=answer_replay,
        now=lambda: NOW,
    )

    worker.run_once()

    assert len(answer_replay.calls) == 1
    call = answer_replay.calls[0]
    assert call["context_items"] == (
        {"document_id": "s1", "snippet": "x"},
        {"document_id": "s2", "snippet": "y"},
    )


def test_worker_does_not_fabricate_refusal_quality_without_a_golden_label() -> None:
    from app.evaluation.models import JudgeScores

    env = build_test_env()
    _insert_run(env, sample_items=(_sample(),))
    worker = _worker(
        env,
        AttributionJudge(
            scores=JudgeScores(
                faithfulness=0.9,
                answer_relevancy=0.8,
                is_refusal=True,
                latency_ms=50,
            )
        ),
    )

    worker.run_once()

    repository = env["runtime"].resolve("evaluation_repository")
    with env["engine"].connect() as connection:
        results = repository.list_results(connection, run_id="run_1")
    assert "refusal_rate" not in results[0]["metrics_json"]


# ------------------------------------------------ retry classification (A12/A18)


def test_retryable_judge_failure_moves_to_retry_wait_with_new_attempt() -> None:
    env = build_test_env()
    _insert_run(env, sample_items=(_sample(),))
    judge = RetryableJudge(code="judge_rate_limited")
    worker = _worker(env, judge)
    stats = worker.run_once()
    assert stats.runs_failed == 0
    assert _state(env, "run_1") == "retry_wait"
    assert _attempt(env, "run_1") == 2


class MutableClock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self):
        return self.now


def test_retryable_judge_failure_at_max_attempts_fails() -> None:
    env = build_test_env()
    _insert_run(env, sample_items=(_sample(),))
    judge = RetryableJudge(code="evaluation_judge_unavailable", status=503)
    repo = env["runtime"].resolve("evaluation_repository")
    clock = MutableClock()
    worker = ShadowEvaluationWorker(
        env["engine"],
        repo,
        judge,
        FakeRetrievalReplayPort(),
        answer_replay=FakeAnswerReplayPort(),
        now=clock,
    )
    for _ in range(3):
        worker.run_once()
        if _state(env, "run_1") == "failed":
            break
        # Advance past the frozen-policy backoff so the retry_wait run requeues.
        clock.now = clock.now + timedelta(hours=1)
    assert _state(env, "run_1") == "failed"
    assert _attempt(env, "run_1") >= 3


def test_non_retryable_judge_failure_fails_immediately() -> None:
    env = build_test_env()
    _insert_run(env, sample_items=(_sample(),))
    judge = NonRetryableJudge()
    worker = _worker(env, judge)
    worker.run_once()
    assert _state(env, "run_1") == "failed"
    assert _attempt(env, "run_1") == 1


def test_worker_passes_real_run_attribution_to_judge() -> None:
    env = build_test_env()
    _insert_run(env, sample_items=(_sample(),))
    judge = AttributionJudge()
    worker = _worker(env, judge)
    worker.run_once()
    assert len(judge.requests) == 1
    request = judge.requests[0]
    assert request.run_id == "run_1"
    assert request.attempt_id == "run_1:1"
    assert request.actor_user_id == "ops_1"
