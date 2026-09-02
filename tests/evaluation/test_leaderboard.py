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
                # The seeded run carries no golden set version, so eligibility
                # runs through the §8.4 weak-signal replacement path.
                weak_signals_json={"weak_has_citation": True},
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


def _seed_no_golden_active_default(
    env,
    *,
    space_id: str,
    run_id: str,
    config_name: str,
    sample_count: int,
    weak_signals: dict,
) -> None:
    """A succeeded golden-less run behind an active-default row (§8.4 ladder)."""
    repo = env["runtime"].resolve("evaluation_repository")
    with env["engine"].begin() as connection:
        from app.identity.schema import identity_space_table

        connection.execute(
            identity_space_table.insert().values(
                id=space_id,
                kind="public",
                name=space_id,
                owner_user_id=None,
                department_id=None,
                created_at_utc=NOW,
            )
        )
        connection.execute(
            shadow_evaluation_run_table.insert().values(
                run_id=run_id,
                space_id=space_id,
                state="succeeded",
                attempt=1,
                lease_owner=None,
                lease_expires_at_utc=None,
                heartbeat_at_utc=None,
                fencing_token=None,
                next_attempt_at_utc=None,
                failure_class=None,
                progress_json={"total": sample_count, "completed": sample_count, "failed": 0},
                report_ref=None,
                policy_version="eval-v1",
                comparator_key=f"cmp_{run_id}",
                candidate_config_versions_json=[config_name],
                index_generation_id="generation_1",
                index_revision=1,
                frozen_snapshot_json={},
                created_at_utc=NOW,
                started_at_utc=NOW,
                completed_at_utc=NOW,
                version=1,
            )
        )
        for index in range(1, sample_count + 1):
            connection.execute(
                shadow_evaluation_result_table.insert().values(
                    run_id=run_id,
                    sample_item_id=f"sample_{index}",
                    candidate_config_version=config_name,
                    session_id=f"session_{index}",
                    metrics_json={
                        "faithfulness": 0.9,
                        "answer_relevancy": 0.9,
                        "hit_at_k_final": 0.0,
                        "mrr": 0.0,
                        "p95_latency_ms": 100.0,
                        "cost_per_query": None,
                    },
                    weak_signals_json=dict(weak_signals),
                    judged_at_utc=NOW,
                )
            )
        repo.set_active_default(
            connection,
            space_id=space_id,
            candidate_config_version=config_name,
            comparator_key=f"cmp_{run_id}",
            source_run_id=run_id,
            now=NOW,
        )


def _seed_run_usage_event(env, *, run_id: str, event_id: str, amount: str) -> None:
    from decimal import Decimal

    from app.usage.schema import usage_event_table

    with env["engine"].begin() as connection:
        connection.execute(
            usage_event_table.insert().values(
                usage_event_id=event_id,
                event_kind="provider_usage",
                provider_call_id=f"call_{event_id}",
                provider="bailian",
                model="qwen3.7-plus",
                operation="evaluation_judge",
                execution_kind="shadow_evaluation",
                execution_id=run_id,
                cost_center_key="system:evaluation",
                price_version_id="price_1",
                currency_code="USD",
                estimated_cost_amount=Decimal(amount),
                estimated_cost_status="complete",
                result="succeeded",
                event_fingerprint=f"fp_{event_id}",
                ownership_json={},
                started_at_utc=NOW,
                completed_at_utc=NOW,
                effective_calendar_version_id="cal_1",
                effective_at_utc=NOW,
                effective_period="2026-09",
                recorded_calendar_version_id="cal_1",
                recorded_at_utc=NOW,
                recorded_period="2026-09",
                created_at_utc=NOW,
            )
        )


def test_leaderboard_cost_per_query_comes_from_metered_usage_or_null() -> None:
    env = build_test_env()
    _seed_no_golden_active_default(
        env,
        space_id="space_1",
        run_id="run_1",
        config_name="cfg_metered",
        sample_count=2,
        weak_signals={},
    )
    _seed_no_golden_active_default(
        env,
        space_id="space_2",
        run_id="run_2",
        config_name="cfg_unmetered",
        sample_count=2,
        weak_signals={},
    )
    # run_1 carries two metered provider-call usage events totalling 0.02.
    _seed_run_usage_event(env, run_id="run_1", event_id="ue_1", amount="0.012")
    _seed_run_usage_event(env, run_id="run_1", event_id="ue_2", amount="0.008")

    token, _, _ = provision_and_login(env["identity"], "ops1", role="ops")
    body = _leaderboard(env, token).json()
    metrics_by_name = {entry["name"]: entry["metrics"] for entry in body["entries"]}
    # 0.02 metered cost summed over the run, divided by 2 distinct items.
    assert metrics_by_name["cfg_metered"]["cost_per_query"] == 0.01
    # No metered usage: null, never a fabricated 0.0.
    assert metrics_by_name["cfg_unmetered"]["cost_per_query"] is None


def test_leaderboard_no_golden_entry_uses_weak_signal_eligibility() -> None:
    """§8.4: a golden-less run ranks through weak signals, not zeroed hit@k."""
    env = build_test_env()
    _seed_no_golden_active_default(
        env,
        space_id="space_1",
        run_id="run_weak",
        config_name="cfg_cited",
        sample_count=2,
        weak_signals={"weak_has_citation": True},
    )
    _seed_no_golden_active_default(
        env,
        space_id="space_2",
        run_id="run_uncited",
        config_name="cfg_uncited",
        sample_count=2,
        weak_signals={"weak_has_citation": False},
    )

    token, _, _ = provision_and_login(env["identity"], "ops1", role="ops")
    body = _leaderboard(env, token).json()
    entry_by_name = {entry["name"]: entry for entry in body["entries"]}
    # Citation share 1.0 passes the hit_at_k_final bar; 0.0 fails it.
    assert entry_by_name["cfg_cited"]["eligible"] is True
    assert entry_by_name["cfg_cited"]["score"] > 0
    assert entry_by_name["cfg_uncited"]["eligible"] is False
    assert entry_by_name["cfg_uncited"]["score"] == 0
