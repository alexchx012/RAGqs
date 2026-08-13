"""Policy immutability, validation, thresholds and ranking (A20/A21/A22)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.evaluation.policy import (
    build_comparator_key,
    default_policy_snapshot,
    threshold_eligibility,
    validate_policy,
    weighted_score,
)
from app.platform.errors import PlatformError


def test_policy_has_no_update_api() -> None:
    # The evaluation domain exposes no PUT/PATCH policy endpoint; the router
    # module defines only the three read/write run/leaderboard/window routes.
    import app.api.v1.evaluation as evaluation_api

    routes = {route.path for route in evaluation_api.router.routes}
    assert not any("policy" in path for path in routes)


def test_default_policy_passes_validation() -> None:
    policy = default_policy_snapshot()
    validate_policy(policy)


@pytest.mark.parametrize(
    "field,value",
    [
        ("faithfulness_min", 0),
        ("refusal_rate_min", 0),
        ("hit_at_k_final_min", 0),
        ("mrr_min", 0),
        ("p95_latency_max_ms", 0),
        ("cost_per_query_max", 0),
        ("min_real_queries", 19),
        ("min_real_queries", 501),
        ("shadow_max_examples", 501),
        ("shadow_max_candidate_configs", 6),
        ("calibration_open_score_gap", 0.2),
        ("cold_start_sample_rate", 0.6),
        ("sentinel_sample_rate", 0.06),
    ],
)
def test_policy_rejects_missing_zero_or_out_of_range(field: str, value: object) -> None:
    policy = default_policy_snapshot()
    with pytest.raises(PlatformError) as raised:
        replaced = replace(policy, **{field: value})
        validate_policy(replaced)
    assert raised.value.code == "evaluation_policy_invalid"


def test_threshold_eligibility_has_no_compensation() -> None:
    policy = default_policy_snapshot()
    # Every dimension passes except faithfulness; other strong scores must not compensate.
    metrics = {
        "faithfulness": 0.0,
        "refusal_rate": 1.0,
        "hit_at_k_final": 1.0,
        "mrr": 1.0,
        "p95_latency_ms": 10,
        "cost_per_query": 0.001,
    }
    assert threshold_eligibility(metrics, policy) is False


def test_weighted_score_uses_generation_and_retrieval_weights() -> None:
    metrics = {
        "faithfulness": 0.6,
        "answer_relevancy": 0.8,
        "hit_at_k_final": 1.0,
        "mrr": 0.5,
    }
    expected = 0.3 * 0.6 + 0.3 * 0.8 + 0.25 * 1.0 + 0.15 * 0.5
    assert abs(weighted_score(metrics) - expected) < 1e-9


def test_threshold_eligibility_passes_when_all_pass() -> None:
    policy = default_policy_snapshot()
    metrics = {
        "faithfulness": policy.faithfulness_min,
        "refusal_rate": policy.refusal_rate_min,
        "hit_at_k_final": policy.hit_at_k_final_min,
        "mrr": policy.mrr_min,
        "p95_latency_ms": policy.p95_latency_max_ms,
        "cost_per_query": policy.cost_per_query_max,
    }
    assert threshold_eligibility(metrics, policy) is True


def test_comparator_key_changes_with_any_member() -> None:
    base = dict(
        golden_set_version="gv1",
        judge_provider="bailian",
        judge_model="qwen3.7-plus",
        judge_mode="non_thinking",
        judge_capability="qwen3.7-plus",
        judge_release="stable",
        judge_prompt_version="v1",
        judge_k=5,
    )
    first = build_comparator_key(**base)
    assert first == build_comparator_key(**base)
    assert first != build_comparator_key(**{**base, "judge_k": 6})
    assert first != build_comparator_key(**{**base, "judge_prompt_version": "v2"})
    assert first != build_comparator_key(**{**base, "golden_set_version": "gv2"})
