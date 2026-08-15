"""Frozen inputs, read-only sampling and shadow session identity (A8/A9/A10)."""

from __future__ import annotations

from sqlalchemy import select

from app.evaluation.policy import default_policy_snapshot
from app.evaluation.schema import (
    evaluation_sample_snapshot_item_table,
    shadow_evaluation_result_table,
    shadow_evaluation_run_table,
)
from app.evaluation.worker import ShadowEvaluationWorker

from .conftest import (
    NOW,
    FakeAnswerReplayPort,
    FakeJudgeProvider,
    FakeRetrievalReplayPort,
    build_test_env,
)


def _insert_run(env, *, sample_items=(), candidate_configs=("default",)) -> str:
    repo = env["runtime"].resolve("evaluation_repository")
    policy = default_policy_snapshot(now=NOW)
    with env["engine"].begin() as connection:
        repo.ensure_policy(connection, policy=policy)
        repo.insert_run(
            connection,
            run_id="run_1",
            space_id="space_1",
            policy_version=policy.policy_version,
            comparator_key="cmp_1",
            candidate_config_versions=candidate_configs,
            index_generation_id="gen_1",
            index_revision=1,
            frozen_snapshot={"snapshot_id": "snap_1", "policy_version": policy.policy_version},
            snapshot_id="snap_1",
            sample_items=sample_items,
            now=NOW,
            initiator_user_id="ops_1",
            request_hash="hash_1",
            idempotency_key="key_1",
        )
    return "run_1"


def test_frozen_snapshot_not_rewritten_after_changes() -> None:
    env = build_test_env()
    repo = env["runtime"].resolve("evaluation_repository")
    _insert_run(env)
    with env["engine"].connect() as connection:
        before = connection.execute(
            select(shadow_evaluation_run_table.c.frozen_snapshot_json).where(
                shadow_evaluation_run_table.c.run_id == "run_1"
            )
        ).scalar_one()
    # Simulate later policy/index/config changes.
    policy = default_policy_snapshot(now=NOW)
    with env["engine"].begin() as connection:
        repo.ensure_policy(connection, policy=policy)
    with env["engine"].connect() as connection:
        after = connection.execute(
            select(shadow_evaluation_run_table.c.frozen_snapshot_json).where(
                shadow_evaluation_run_table.c.run_id == "run_1"
            )
        ).scalar_one()
    assert before == after


def test_sample_snapshot_is_immutable_and_read_only() -> None:
    env = build_test_env()
    samples = (
        {
            "item_id": "item_1",
            "position": 1,
            "question_text": "question 1",
            "question_hash": "hash_1",
            "evidence_hash": "ev_1",
            "weak_signals": {},
            "source_ref": "msg_1",
        },
    )
    _insert_run(env, sample_items=samples)
    with env["engine"].connect() as connection:
        rows = connection.execute(
            select(evaluation_sample_snapshot_item_table).where(
                evaluation_sample_snapshot_item_table.c.snapshot_id == "snap_1"
            )
        ).all()
    assert len(rows) == 1
    # The evaluation domain never writes chat facts.
    from app.chat.schema import chat_message_table

    with env["engine"].connect() as connection:
        assert connection.execute(select(chat_message_table)).all() == []


def test_worker_uses_shadow_session_and_no_online_messages() -> None:
    env = build_test_env()
    retrieval = FakeRetrievalReplayPort()
    answer_replay = FakeAnswerReplayPort()
    judge = FakeJudgeProvider()
    env["retrieval"] = retrieval
    env["judge"] = judge
    samples = (
        {
            "item_id": "item_1",
            "position": 1,
            "question_text": "question 1",
            "question_hash": "hash_1",
            "evidence_hash": "ev_1",
            "weak_signals": {},
            "source_ref": "msg_1",
        },
    )
    _insert_run(env, sample_items=samples, candidate_configs=("default",))
    repo = env["runtime"].resolve("evaluation_repository")
    worker = ShadowEvaluationWorker(
        env["engine"],
        repo,
        judge,
        retrieval,
        answer_replay=answer_replay,
        now=lambda: NOW,
    )
    worker.run_once()
    assert retrieval.calls
    for call in retrieval.calls:
        assert call["session_id"] == "shadow:run_1:item_1:default"
    from app.chat.schema import chat_message_table

    with env["engine"].connect() as connection:
        assert connection.execute(select(chat_message_table)).all() == []


def test_worker_replays_a_non_empty_answer_without_writing_chat_facts() -> None:
    env = build_test_env()
    retrieval = FakeRetrievalReplayPort()
    answer_replay = FakeAnswerReplayPort(answer="completed answer")
    judge = FakeJudgeProvider()
    _insert_run(
        env,
        sample_items=(
            {
                "item_id": "item_1",
                "position": 1,
                "question_text": "question 1",
                "question_hash": "hash_1",
                "evidence_hash": "ev_1",
                "weak_signals": {},
                "source_ref": "user_message_1",
            },
        ),
        candidate_configs=("default",),
    )
    repository = env["runtime"].resolve("evaluation_repository")
    worker = ShadowEvaluationWorker(
        env["engine"],
        repository,
        judge,
        retrieval,
        answer_replay=answer_replay,
        now=lambda: NOW,
    )

    worker.run_once()

    assert judge.calls[0].answer == "completed answer"
    assert answer_replay.calls[0]["source_ref"] == "user_message_1"
    assert answer_replay.calls[0]["session_id"] == "shadow:run_1:item_1:default"
    from app.chat.schema import chat_message_table

    with env["engine"].connect() as connection:
        assert connection.execute(select(chat_message_table)).all() == []


def test_worker_retries_when_answer_replay_returns_no_usable_answer() -> None:
    env = build_test_env()
    answer_replay = FakeAnswerReplayPort(answer="   ")
    judge = FakeJudgeProvider()
    _insert_run(
        env,
        sample_items=(
            {
                "item_id": "item_1",
                "position": 1,
                "question_text": "question 1",
                "question_hash": "hash_1",
                "evidence_hash": "ev_1",
                "weak_signals": {},
                "source_ref": "user_message_1",
            },
        ),
        candidate_configs=("default",),
    )
    repository = env["runtime"].resolve("evaluation_repository")
    worker = ShadowEvaluationWorker(
        env["engine"],
        repository,
        judge,
        FakeRetrievalReplayPort(),
        answer_replay=answer_replay,
        now=lambda: NOW,
    )

    worker.run_once()

    with env["engine"].connect() as connection:
        state = connection.execute(
            select(shadow_evaluation_run_table.c.state).where(
                shadow_evaluation_run_table.c.run_id == "run_1"
            )
        ).scalar_one()
        results = connection.execute(select(shadow_evaluation_result_table)).all()
    assert state == "retry_wait"
    assert judge.calls == []
    assert results == []
