"""Run lifecycle state machine, fence and lease recovery (A7/A11/A12)."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.evaluation.policy import default_policy_snapshot
from app.evaluation.schema import shadow_evaluation_run_table

from .conftest import NOW, build_test_env


def _repository(env):
    return env["runtime"].resolve("evaluation_repository")


def _insert_run(env, *, run_id: str = "run_1", space_id: str = "space_1") -> str:
    repo = _repository(env)
    policy = default_policy_snapshot(now=NOW)
    with env["engine"].begin() as connection:
        repo.ensure_policy(connection, policy=policy)
        repo.insert_run(
            connection,
            run_id=run_id,
            space_id=space_id,
            policy_version=policy.policy_version,
            comparator_key="cmp_1",
            candidate_config_versions=("default",),
            index_generation_id="gen_1",
            index_revision=1,
            frozen_snapshot={"snapshot_id": "snap_1"},
            snapshot_id="snap_1",
            sample_items=(),
            now=NOW,
            initiator_user_id="ops_1",
            request_hash="hash_1",
            idempotency_key="key_1",
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


def test_queued_to_running_to_retry_wait_to_queued() -> None:
    env = build_test_env()
    run_id = _insert_run(env)
    repo = _repository(env)
    with env["engine"].begin() as connection:
        run = repo.claim_next(connection, owner="worker", lease_ttl_seconds=60, now=NOW)
    assert run is not None and run.state == "running"
    assert _state(env, run_id) == "running"
    with env["engine"].begin() as connection:
        ok = repo.transition_retry_wait(
            connection,
            run_id=run_id,
            attempt=run.attempt,
            owner="worker",
            fencing_token=run.fencing_token or "",
            next_attempt_at=NOW + timedelta(seconds=1),
            now=NOW,
        )
    assert ok is True
    assert _state(env, run_id) == "retry_wait"
    with env["engine"].begin() as connection:
        repo.requeue_retry_wait(connection, now=NOW + timedelta(seconds=2))
    assert _state(env, run_id) == "queued"


def test_terminal_is_not_overwritten_or_reopened() -> None:
    env = build_test_env()
    run_id = _insert_run(env)
    repo = _repository(env)
    with env["engine"].begin() as connection:
        run = repo.claim_next(connection, owner="worker", lease_ttl_seconds=60, now=NOW)
    assert run is not None
    with env["engine"].begin() as connection:
        ok = repo.transition_terminal(
            connection,
            run_id=run_id,
            attempt=run.attempt,
            owner="worker",
            fencing_token=run.fencing_token or "",
            to_state="succeeded",
            now=NOW,
        )
    assert ok is True
    assert _state(env, run_id) == "succeeded"
    with env["engine"].begin() as connection:
        again = repo.transition_terminal(
            connection,
            run_id=run_id,
            attempt=run.attempt,
            owner="worker",
            fencing_token=run.fencing_token or "",
            to_state="failed",
            now=NOW,
        )
    assert again is False
    assert _state(env, run_id) == "succeeded"


def test_heartbeat_requires_matching_fence() -> None:
    env = build_test_env()
    run_id = _insert_run(env)
    repo = _repository(env)
    with env["engine"].begin() as connection:
        run = repo.claim_next(connection, owner="worker", lease_ttl_seconds=60, now=NOW)
    assert run is not None
    with env["engine"].begin() as connection:
        ok = repo.heartbeat(
            connection,
            run_id=run_id,
            attempt=run.attempt,
            owner="worker",
            fencing_token="wrong_fence",
            now=NOW,
        )
    assert ok is False


def test_lease_expiry_recovers_with_new_attempt() -> None:
    env = build_test_env()
    run_id = _insert_run(env)
    repo = _repository(env)
    with env["engine"].begin() as connection:
        run = repo.claim_next(connection, owner="worker", lease_ttl_seconds=60, now=NOW)
    assert run is not None and run.attempt == 1
    with env["engine"].begin() as connection:
        recovered = repo.recover_expired(
            connection,
            run_id=run_id,
            attempt=run.attempt,
            next_attempt_at=NOW + timedelta(seconds=5),
            now=NOW + timedelta(seconds=120),
            max_attempts=3,
        )
    assert recovered is True
    assert _state(env, run_id) == "retry_wait"
    with env["engine"].connect() as connection:
        attempt = connection.execute(
            select(shadow_evaluation_run_table.c.attempt).where(
                shadow_evaluation_run_table.c.run_id == run_id
            )
        ).scalar_one()
    assert attempt == 2


def test_lease_expiry_at_max_attempts_fails() -> None:
    env = build_test_env()
    run_id = _insert_run(env)
    repo = _repository(env)
    with env["engine"].begin() as connection:
        run = repo.claim_next(connection, owner="worker", lease_ttl_seconds=60, now=NOW)
    assert run is not None
    with env["engine"].begin() as connection:
        recovered = repo.recover_expired(
            connection,
            run_id=run_id,
            attempt=run.attempt,
            next_attempt_at=NOW + timedelta(seconds=5),
            now=NOW + timedelta(seconds=120),
            max_attempts=1,
        )
    assert recovered is True
    assert _state(env, run_id) == "failed"
