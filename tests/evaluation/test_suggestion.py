"""Suggestion transitions and outbox publication (A25–A28)."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.evaluation.policy import default_policy_snapshot
from app.evaluation.schema import calibration_window_suggestion_table

from .conftest import (
    NOW,
    RecordingCalibrationOutboxPort,
    build_test_env,
)


def _make_succeeded_run(
    env,
    *,
    run_id="run_1",
    space_id="space_1",
    now=NOW,
    golden_version: str | None = None,
    metrics_by_config: dict[str, dict] | None = None,
    weak_signals: dict | None = None,
) -> None:
    repo = env["runtime"].resolve("evaluation_repository")
    policy = default_policy_snapshot(now=now)
    metrics_by_config = metrics_by_config or {
        "default": {
            "faithfulness": 0.9,
            "answer_relevancy": 0.8,
            "refusal_rate": 1.0,
            "hit_at_k_final": 0.9,
            "mrr": 0.8,
            "p95_latency_ms": 100,
            "cost_per_query": 0.001,
        },
        "candidate_b": {
            "faithfulness": 0.9,
            "answer_relevancy": 0.8,
            "refusal_rate": 1.0,
            "hit_at_k_final": 0.9,
            "mrr": 0.8,
            "p95_latency_ms": 100,
            "cost_per_query": 0.001,
        },
    }
    samples = tuple(
        {
            "item_id": f"item_{i}",
            "position": i,
            "question_text": f"q{i}",
            "question_hash": f"h{i}",
            "evidence_hash": f"e{i}",
            "weak_signals": dict(weak_signals or {}),
            "source_ref": f"m{i}",
        }
        for i in range(1, 51)
    )
    frozen: dict = {"snapshot_id": f"snap_{run_id}"}
    if golden_version:
        frozen["golden_set_version"] = golden_version
    with env["engine"].begin() as connection:
        repo.ensure_policy(connection, policy=policy)
        repo.insert_run(
            connection,
            run_id=run_id,
            space_id=space_id,
            policy_version=policy.policy_version,
            comparator_key="cmp_1",
            candidate_config_versions=tuple(metrics_by_config),
            index_generation_id="gen_1",
            index_revision=1,
            frozen_snapshot=frozen,
            snapshot_id=f"snap_{run_id}",
            sample_items=samples,
            now=now,
            initiator_user_id="ops_1",
            request_hash=f"hash_{run_id}",
            idempotency_key=f"key_{run_id}",
        )
        claimed = repo.claim_next(connection, owner="worker", lease_ttl_seconds=60, now=now)
        assert claimed is not None
        repo.transition_terminal(
            connection,
            run_id=run_id,
            attempt=claimed.attempt,
            owner="worker",
            fencing_token=claimed.fencing_token or "",
            to_state="succeeded",
            now=now,
            progress={"total": 50, "completed": 50, "failed": 0},
        )
        for i in range(1, 51):
            for candidate, metrics in metrics_by_config.items():
                repo.insert_result(
                    connection,
                    run_id=run_id,
                    sample_item_id=f"item_{i}",
                    candidate_config_version=candidate,
                    session_id=f"shadow:{run_id}:item_{i}:{candidate}",
                    metrics_json=dict(metrics),
                    weak_signals_json=dict(weak_signals or {}),
                    judged_at=now,
                )


def _make_standard_leaderboard_source(
    env,
    *,
    run_id: str,
    space_id: str,
    comparator_key: str,
    set_active: bool = True,
    now=NOW,
) -> None:
    """Seed the durable facts represented by a standard leaderboard row."""

    repo = env["runtime"].resolve("evaluation_repository")
    policy = default_policy_snapshot(now=now)
    samples = tuple(
        {
            "item_id": f"{run_id}_item_{i}",
            "position": i,
            "question_text": f"{run_id} question {i}",
            "question_hash": f"{run_id}_hash_{i}",
            "evidence_hash": f"{run_id}_evidence_{i}",
            "weak_signals": {},
            "source_ref": f"{run_id}_message_{i}",
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
            comparator_key=comparator_key,
            candidate_config_versions=("cfg_a", "cfg_b"),
            index_generation_id="gen_1",
            index_revision=1,
            frozen_snapshot={"snapshot_id": f"snap_{run_id}"},
            snapshot_id=f"snap_{run_id}",
            sample_items=samples,
            now=now,
            initiator_user_id="ops_1",
            request_hash=f"hash_{run_id}",
            idempotency_key=f"key_{run_id}",
        )
        claimed = repo.claim_next(connection, owner="worker", lease_ttl_seconds=60, now=now)
        assert claimed is not None
        repo.transition_terminal(
            connection,
            run_id=run_id,
            attempt=claimed.attempt,
            owner="worker",
            fencing_token=claimed.fencing_token or "",
            to_state="succeeded",
            now=now,
            progress={"total": 100, "completed": 100, "failed": 0},
        )
        for i in range(1, 51):
            for candidate, answer_relevancy in (("cfg_a", 0.8), ("cfg_b", 0.79)):
                repo.insert_result(
                    connection,
                    run_id=run_id,
                    sample_item_id=f"{run_id}_item_{i}",
                    candidate_config_version=candidate,
                    session_id=f"standard:{run_id}:{i}:{candidate}",
                    metrics_json={
                        "faithfulness": 0.9,
                        "answer_relevancy": answer_relevancy,
                        "refusal_rate": 1.0,
                        "hit_at_k_final": 0.9,
                        "mrr": 0.8,
                        "p95_latency_ms": 100,
                        "cost_per_query": 0.001,
                    },
                    weak_signals_json={},
                    judged_at=now,
                )
        if set_active:
            repo.set_active_default(
                connection,
                space_id=space_id,
                candidate_config_version="cfg_a",
                comparator_key=comparator_key,
                source_run_id=run_id,
                now=now,
            )


def test_standard_leaderboard_source_is_scoped_to_space_and_comparator() -> None:
    env = build_test_env()
    _make_standard_leaderboard_source(
        env, run_id="standard_space_1", space_id="space_1", comparator_key="cmp_same"
    )
    _make_standard_leaderboard_source(
        env, run_id="standard_space_2", space_id="space_2", comparator_key="cmp_same"
    )
    _make_standard_leaderboard_source(
        env,
        run_id="standard_other_cmp",
        space_id="space_1",
        comparator_key="cmp_other",
        set_active=False,
    )

    service = env["runtime"].resolve("evaluation_service")
    # ``evaluation_active_default`` is one row per space. The unrelated
    # comparator run above has no active leaderboard row and must not join the
    # two valid scopes below.
    assert service.compute_standard_leaderboard_suggestions() == 2

    with env["engine"].connect() as connection:
        rows = (
            connection.execute(
                select(
                    calibration_window_suggestion_table.c.space_id,
                    calibration_window_suggestion_table.c.comparator_key,
                    calibration_window_suggestion_table.c.rank_summary_json,
                    calibration_window_suggestion_table.c.status,
                ).order_by(calibration_window_suggestion_table.c.created_at_utc)
            )
            .mappings()
            .all()
        )

    assert {(row["space_id"], row["comparator_key"]) for row in rows} == {
        ("space_1", "cmp_same"),
        ("space_2", "cmp_same"),
    }
    assert all(row["status"] == "actionable" for row in rows)
    assert all(row["rank_summary_json"]["source"] == "standard_leaderboard" for row in rows)


def test_standard_leaderboard_does_not_combine_spaces_into_one_suggestion() -> None:
    env = build_test_env()
    _make_standard_leaderboard_source(
        env, run_id="standard_space_1", space_id="space_1", comparator_key="cmp_same"
    )
    _make_standard_leaderboard_source(
        env, run_id="standard_space_2", space_id="space_2", comparator_key="cmp_same"
    )
    service = env["runtime"].resolve("evaluation_service")

    service.compute_standard_leaderboard_suggestions(space_id="space_1", comparator_key="cmp_same")

    with env["engine"].connect() as connection:
        rows = connection.execute(select(calibration_window_suggestion_table)).mappings().all()
    assert len(rows) == 1
    assert rows[0]["space_id"] == "space_1"
    assert rows[0]["comparator_key"] == "cmp_same"


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


def test_newer_run_supersedes_the_previous_actionable_suggestion() -> None:
    outbox = RecordingCalibrationOutboxPort()
    env = build_test_env(outbox=outbox)
    service = env["runtime"].resolve("evaluation_service")
    service.attach_outbox(outbox)
    _make_succeeded_run(env, run_id="run_1", now=NOW)
    service.compute_suggestion("run_1")
    _make_succeeded_run(env, run_id="run_2", now=NOW + timedelta(seconds=1))

    service.compute_suggestion("run_2")

    with env["engine"].connect() as connection:
        rows = (
            connection.execute(
                select(calibration_window_suggestion_table.c.status).order_by(
                    calibration_window_suggestion_table.c.created_at_utc
                )
            )
            .scalars()
            .all()
        )
    assert rows == ["superseded", "actionable"]
    assert len(outbox.events) == 2
    service.compute_suggestion("run_2")
    assert len(outbox.events) == 2


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


# ------------------------------------------- §8.4/§8.5 no-golden ladder path


_CITED_WEAK = {"weak_has_citation": True}
_UNCITED_WEAK = {"weak_has_citation": False}


def _suggestion_rows(env, *, space_id: str):
    with env["engine"].connect() as connection:
        return (
            connection.execute(
                select(
                    calibration_window_suggestion_table.c.suggestion_id,
                    calibration_window_suggestion_table.c.rank_summary_json,
                ).where(
                    calibration_window_suggestion_table.c.space_id == space_id,
                    calibration_window_suggestion_table.c.status == "actionable",
                )
            )
            .mappings()
            .all()
        )


def test_no_golden_run_suggests_on_score_gap_with_weak_signal_eligibility() -> None:
    """§8.4/§8.5: a golden-less run needs only the top-2 gap to suggest."""
    outbox = RecordingCalibrationOutboxPort()
    env = build_test_env(outbox=outbox)
    service = env["runtime"].resolve("evaluation_service")
    service.attach_outbox(outbox)
    _make_succeeded_run(
        env,
        run_id="run_weak",
        golden_version=None,
        weak_signals=_CITED_WEAK,
        metrics_by_config={
            # Retrieval metrics are unmeasurable without golden labels; the
            # weak citation share carries the hit@k bar instead.
            "cfg_a": {
                "faithfulness": 0.9,
                "answer_relevancy": 0.8,
                "hit_at_k_final": 0.0,
                "mrr": 0.0,
                "p95_latency_ms": 100,
                "cost_per_query": None,
            },
            "cfg_b": {
                "faithfulness": 0.9,
                "answer_relevancy": 0.79,
                "hit_at_k_final": 0.0,
                "mrr": 0.0,
                "p95_latency_ms": 100,
                "cost_per_query": None,
            },
        },
    )

    service.compute_suggestion("run_weak")

    rows = _suggestion_rows(env, space_id="space_1")
    assert len(rows) == 1
    rankings = rows[0]["rank_summary_json"]["rankings"]
    # The weak citation share (1.0) passes the hit_at_k_final bar.
    assert all(item["eligible"] is True for item in rankings)
    assert len(outbox.events) == 1


def test_no_golden_run_still_suggests_without_weak_signal_eligibility() -> None:
    """Ladder level 3 does not require threshold eligibility without golden."""
    outbox = RecordingCalibrationOutboxPort()
    env = build_test_env(outbox=outbox)
    service = env["runtime"].resolve("evaluation_service")
    service.attach_outbox(outbox)
    # No weak-signal data at all: configs stay ineligible, but the small
    # top-2 composite gap still opens the §8.5 level-3 suggestion.
    _make_succeeded_run(env, run_id="run_nosignal", golden_version=None)

    service.compute_suggestion("run_nosignal")

    rows = _suggestion_rows(env, space_id="space_1")
    assert len(rows) == 1
    assert all(item["eligible"] is False for item in rows[0]["rank_summary_json"]["rankings"])


def test_golden_run_still_requires_threshold_eligible_top_two() -> None:
    """The relaxed gap-only gate applies only to golden-less runs."""
    env = build_test_env()
    service = env["runtime"].resolve("evaluation_service")
    _make_succeeded_run(
        env,
        run_id="run_golden_strict",
        golden_version="gv1",
        metrics_by_config={
            "cfg_a": {
                "faithfulness": 0.1,
                "answer_relevancy": 0.8,
                "refusal_rate": 1.0,
                "hit_at_k_final": 0.9,
                "mrr": 0.8,
                "p95_latency_ms": 100,
                "cost_per_query": 0.001,
            },
            "cfg_b": {
                "faithfulness": 0.1,
                "answer_relevancy": 0.8,
                "refusal_rate": 1.0,
                "hit_at_k_final": 0.9,
                "mrr": 0.8,
                "p95_latency_ms": 100,
                "cost_per_query": 0.001,
            },
        },
    )

    service.compute_suggestion("run_golden_strict")

    assert _suggestion_rows(env, space_id="space_1") == []


def _ops(env):
    from .conftest import provision_and_login

    return provision_and_login(env["identity"], "ops1", role="ops")
