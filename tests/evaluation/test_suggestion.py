"""Suggestion transitions and outbox publication (A25–A28)."""

from __future__ import annotations

from app.evaluation.policy import default_policy_snapshot

from .conftest import (
    NOW,
    RecordingCalibrationOutboxPort,
    build_test_env,
)


def _make_succeeded_run(env, *, run_id="run_1", space_id="space_1") -> None:
    repo = env["runtime"].resolve("evaluation_repository")
    policy = default_policy_snapshot(now=NOW)
    samples = tuple(
        {
            "item_id": f"item_{i}",
            "position": i,
            "question_text": f"q{i}",
            "question_hash": f"h{i}",
            "evidence_hash": f"e{i}",
            "weak_signals": {},
            "source_ref": f"m{i}",
        }
        for i in range(1, 51)
    )
    with env["engine"].begin() as connection:
        repo.ensure_policy(connection, policy=policy)
        repo.insert_run(
            connection,
            run_id=run_id,
            space_id=space_id,
            policy_version=policy.policy_version,
            comparator_key="cmp_1",
            candidate_config_versions=("default", "candidate_b"),
            index_generation_id="gen_1",
            index_revision=1,
            frozen_snapshot={"snapshot_id": "snap_1"},
            snapshot_id="snap_1",
            sample_items=samples,
            now=NOW,
            initiator_user_id="ops_1",
            request_hash="hash_1",
            idempotency_key="key_1",
        )
        claimed = repo.claim_next(connection, owner="worker", lease_ttl_seconds=60, now=NOW)
        assert claimed is not None
        repo.transition_terminal(
            connection,
            run_id=run_id,
            attempt=claimed.attempt,
            owner="worker",
            fencing_token=claimed.fencing_token or "",
            to_state="succeeded",
            now=NOW,
            progress={"total": 50, "completed": 50, "failed": 0},
        )
        for i in range(1, 51):
            for candidate in ("default", "candidate_b"):
                repo.insert_result(
                    connection,
                    run_id=run_id,
                    sample_item_id=f"item_{i}",
                    candidate_config_version=candidate,
                    session_id=f"shadow:{run_id}:item_{i}:{candidate}",
                    metrics_json={
                        "faithfulness": 0.9,
                        "answer_relevancy": 0.8,
                        "refusal_rate": 1.0,
                        "hit_at_k_final": 0.9,
                        "mrr": 0.8,
                        "p95_latency_ms": 100,
                        "cost_per_query": 0.001,
                    },
                    weak_signals_json={},
                    judged_at=NOW,
                )


def test_suggestion_transitions_and_publishes_once() -> None:
    outbox = RecordingCalibrationOutboxPort()
    env = build_test_env(outbox=outbox)
    _make_succeeded_run(env)
    service = env["runtime"].resolve("evaluation_service")
    service.attach_outbox(outbox)
    service.compute_suggestion("run_1")
    assert len(outbox.events) == 1
    event = outbox.events[0]
    assert event["transition_version"] == 2
    # Only first actionable transition publishes; recomputation must not.
    service.compute_suggestion("run_1")
    assert len(outbox.events) == 1


def test_non_actionable_does_not_publish() -> None:
    outbox = RecordingCalibrationOutboxPort()
    env = build_test_env(outbox=outbox)
    # No succeeded run → compute_suggestion is a no-op.
    service = env["runtime"].resolve("evaluation_service")
    service.attach_outbox(outbox)
    service.compute_suggestion("missing")
    assert outbox.events == []


def test_manual_open_does_not_create_suggestion() -> None:
    outbox = RecordingCalibrationOutboxPort()
    env = build_test_env(outbox=outbox)
    service = env["runtime"].resolve("evaluation_service")
    service.attach_outbox(outbox)
    token, _, _ = _ops(env)
    response = env["client"].post(
        "/v1/calibration/window",
        json={"action": "open", "window_kind": "manual"},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "k1"},
    )
    assert response.status_code == 201
    assert outbox.events == []


def _ops(env):
    from .conftest import provision_and_login

    return provision_and_login(env["identity"], "ops1", role="ops")
