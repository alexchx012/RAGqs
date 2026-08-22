"""Leaderboard response shape, ACL and no raw-content leakage (A23/A24)."""

from __future__ import annotations

from app.evaluation.schema import shadow_evaluation_result_table, shadow_evaluation_run_table
from app.evaluation.service import EvaluationService

from .conftest import NOW, build_test_env, provision_and_login


def _leaderboard(env, token: str):
    return env["client"].get(
        "/v1/evaluation/leaderboard",
        headers={"Authorization": f"Bearer {token}"},
    )


def test_leaderboard_forbidden_for_user() -> None:
    env = build_test_env()
    token, _, _ = provision_and_login(env["identity"], "user1", role="user")
    response = _leaderboard(env, token)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "evaluation_leaderboard_forbidden"


def test_leaderboard_shape_for_ops() -> None:
    env = build_test_env()
    token, _, _ = provision_and_login(env["identity"], "ops1", role="ops")
    response = _leaderboard(env, token)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"entries", "shadow_entries", "policy"}
    assert isinstance(body["entries"], list)
    assert isinstance(body["shadow_entries"], list)
    assert body["policy"]["policy_version"]
    for entry in body["entries"]:
        assert set(entry) == {"rank", "name", "score", "metrics", "eligible", "is_active"}


def test_leaderboard_entries_come_from_active_default() -> None:
    env = build_test_env()
    repo = env["runtime"].resolve("evaluation_repository")
    with env["engine"].begin() as connection:
        from app.identity.schema import identity_space_table

        connection.execute(
            identity_space_table.insert().values(
                id="space_1",
                kind="public",
                name="space one",
                owner_user_id=None,
                department_id=None,
                created_at_utc=NOW,
            )
        )
        repo.set_active_default(
            connection,
            space_id="space_1",
            candidate_config_version="default",
            comparator_key="cmp_1",
            source_run_id="run_1",
            now=NOW,
        )
        connection.execute(
            shadow_evaluation_run_table.insert().values(
                run_id="run_1",
                space_id="space_1",
                state="succeeded",
                attempt=1,
                lease_owner=None,
                lease_expires_at_utc=None,
                heartbeat_at_utc=None,
                fencing_token=None,
                next_attempt_at_utc=None,
                failure_class=None,
                progress_json={"total": 1, "completed": 1, "failed": 0},
                report_ref=None,
                policy_version="eval-v1",
                comparator_key="cmp_1",
                candidate_config_versions_json=["default"],
                index_generation_id="generation_1",
                index_revision=1,
                frozen_snapshot_json={},
                created_at_utc=NOW,
                started_at_utc=NOW,
                completed_at_utc=NOW,
                version=1,
            )
        )
        connection.execute(
            shadow_evaluation_result_table.insert().values(
                run_id="run_1",
                sample_item_id="sample_1",
                candidate_config_version="default",
                session_id="session_1",
                metrics_json={
                    "faithfulness": 0.9,
                    "answer_relevancy": 0.9,
                    "refusal_rate": 0.9,
                    "hit_at_k_final": 0.9,
                    "mrr": 0.9,
                    "p95_latency_ms": 100.0,
                    "cost_per_query": 0.01,
                },
                weak_signals_json={},
                judged_at_utc=NOW,
            )
        )
    token, _, _ = provision_and_login(env["identity"], "ops1", role="ops")
    body = _leaderboard(env, token).json()
    assert len(body["entries"]) == 1
    assert body["entries"][0]["name"] == "default"
    assert body["entries"][0]["is_active"] is True
    assert body["entries"][0]["metrics"]["faithfulness"] == 0.9
    assert body["entries"][0]["score"] > 0


def test_leaderboard_filters_spaces_by_acl() -> None:
    env = build_test_env()
    repo = env["runtime"].resolve("evaluation_repository")
    with env["engine"].begin() as connection:
        from app.identity.schema import identity_space_table

        # A private personal space owned by another user: invisible to the ops
        # viewer below, so its active-default row must not leak into entries.
        connection.execute(
            identity_space_table.insert().values(
                id="private_space",
                kind="personal",
                name="private",
                owner_user_id="someone_else",
                department_id=None,
                created_at_utc=NOW,
            )
        )
        repo.set_active_default(
            connection,
            space_id="private_space",
            candidate_config_version="default",
            comparator_key="cmp_1",
            source_run_id="run_1",
            now=NOW,
        )
    token, _, _ = provision_and_login(env["identity"], "ops1", role="ops")
    body = _leaderboard(env, token).json()
    assert body["entries"] == []


def test_leaderboard_does_not_leak_raw_content() -> None:
    env = build_test_env()
    token, _, _ = provision_and_login(env["identity"], "ops1", role="ops")
    body = _leaderboard(env, token).json()
    text = str(body)
    for forbidden in (
        "question",
        "answer",
        "snippet",
        "citation",
        "document",
        "password",
        "token",
    ):
        assert forbidden not in text


def test_result_metric_aggregation_uses_nearest_rank_p95_latency() -> None:
    metrics = EvaluationService._aggregate_result_metrics(
        [
            {"metrics_json": {"p95_latency_ms": latency}}
            for latency in (10, 20, 30, 40, 50, 60, 70, 80, 90, 100)
        ]
    )

    assert metrics["p95_latency_ms"] == 100
