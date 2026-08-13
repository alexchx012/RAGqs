"""Policy validation, threshold gating and weighted ranking.

The policy is an immutable, versioned deployment fact. The evaluation domain
only reads the effective policy version; there is no ``PUT/PATCH`` policy API
and no run/window override of thresholds or sample rates.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from math import inf
from typing import Any

from app.platform.errors import PlatformError

from .models import EvaluationPolicySnapshot

GENERATION_WEIGHT = 0.6
RETRIEVAL_WEIGHT = 0.4
FAITHFULNESS_WEIGHT = 0.3
ANSWER_RELEVANCY_WEIGHT = 0.3
HIT_AT_K_FINAL_WEIGHT = 0.25
MRR_WEIGHT = 0.15

DEFAULT_POLICY_VERSION = "eval-v1"


def default_policy_snapshot(*, now: datetime | None = None) -> EvaluationPolicySnapshot:
    """Build the V1 deployment default policy fact."""
    created = now if now is not None else datetime.now(UTC)
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return EvaluationPolicySnapshot(
        policy_version=DEFAULT_POLICY_VERSION,
        faithfulness_min=0.6,
        refusal_rate_min=0.6,
        hit_at_k_final_min=0.5,
        mrr_min=0.4,
        p95_latency_max_ms=5000,
        cost_per_query_max=0.05,
        min_real_queries=50,
        shadow_max_examples=200,
        shadow_max_candidate_configs=3,
        calibration_open_score_gap=0.03,
        cold_start_sample_rate=0.4,
        sentinel_sample_rate=0.03,
        pair_vote_ttl_seconds=86400,
        close_grace_seconds=3600,
        max_attempts=3,
        run_deadline_seconds=3600,
        lease_seconds=60,
        heartbeat_seconds=15,
        concurrency=2,
        judge_k=5,
        created_at_utc=created,
    )


def validate_policy(policy: EvaluationPolicySnapshot) -> None:
    """Reject an incomplete, zero or out-of-range policy (A21)."""
    if not isinstance(policy, EvaluationPolicySnapshot):
        raise PlatformError("evaluation_policy_invalid", "Policy is missing", {}, 422)
    thresholds: dict[str, float] = {
        "faithfulness_min": policy.faithfulness_min,
        "refusal_rate_min": policy.refusal_rate_min,
        "hit_at_k_final_min": policy.hit_at_k_final_min,
        "mrr_min": policy.mrr_min,
        "p95_latency_max_ms": float(policy.p95_latency_max_ms),
        "cost_per_query_max": policy.cost_per_query_max,
    }
    for name, value in thresholds.items():
        if value is None or value <= 0:
            raise PlatformError(
                "evaluation_policy_invalid",
                f"Policy threshold {name} must be a positive non-zero value",
                {"field": name},
                422,
            )
    if not 20 <= policy.min_real_queries <= 500:
        raise PlatformError(
            "evaluation_policy_invalid",
            "min_real_queries must be within 20 and 500",
            {"field": "min_real_queries"},
            422,
        )
    if not 20 <= policy.shadow_max_examples <= 500:
        raise PlatformError(
            "evaluation_policy_invalid",
            "shadow_max_examples must be within 20 and 500",
            {"field": "shadow_max_examples"},
            422,
        )
    if not 2 <= policy.shadow_max_candidate_configs <= 5:
        raise PlatformError(
            "evaluation_policy_invalid",
            "shadow_max_candidate_configs must be within 2 and 5",
            {"field": "shadow_max_candidate_configs"},
            422,
        )
    if not 0.01 <= policy.calibration_open_score_gap <= 0.10:
        raise PlatformError(
            "evaluation_policy_invalid",
            "calibration_open_score_gap must be within 0.01 and 0.10",
            {"field": "calibration_open_score_gap"},
            422,
        )
    if not 0 <= policy.cold_start_sample_rate <= 0.5:
        raise PlatformError(
            "evaluation_policy_invalid",
            "cold_start_sample_rate must be within 0 and 0.5",
            {"field": "cold_start_sample_rate"},
            422,
        )
    if not 0 <= policy.sentinel_sample_rate <= 0.05:
        raise PlatformError(
            "evaluation_policy_invalid",
            "sentinel_sample_rate must be within 0 and 0.05",
            {"field": "sentinel_sample_rate"},
            422,
        )


def build_comparator_key(
    *,
    golden_set_version: str | None,
    judge_provider: str,
    judge_model: str,
    judge_mode: str,
    judge_capability: str,
    judge_release: str,
    judge_prompt_version: str,
    judge_k: int,
) -> str:
    """Deterministic comparability key.

    Any member change forms a new, non-comparable score series (A15).
    """
    parts = {
        "golden_set_version": golden_set_version or "",
        "judge_provider": judge_provider,
        "judge_model": judge_model,
        "judge_mode": judge_mode,
        "judge_capability": judge_capability,
        "judge_release": judge_release,
        "judge_prompt_version": judge_prompt_version,
        "judge_k": judge_k,
    }
    encoded = json.dumps(parts, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(b"evaluation-comparator-v1\0" + encoded.encode("utf-8")).hexdigest()


def threshold_eligibility(metrics: Mapping[str, float], policy: EvaluationPolicySnapshot) -> bool:
    """Every dimension must pass independently; no cross-dimension compensation (A22)."""
    faithfulness = metrics.get("faithfulness", 0.0)
    refusal_rate = metrics.get("refusal_rate", 0.0)
    hit_at_k_final = metrics.get("hit_at_k_final", 0.0)
    mrr = metrics.get("mrr", 0.0)
    p95_latency_ms = metrics.get("p95_latency_ms", inf)
    cost_per_query = metrics.get("cost_per_query", inf)
    return bool(
        faithfulness >= policy.faithfulness_min
        and refusal_rate >= policy.refusal_rate_min
        and hit_at_k_final >= policy.hit_at_k_final_min
        and mrr >= policy.mrr_min
        and p95_latency_ms <= float(policy.p95_latency_max_ms)
        and cost_per_query <= policy.cost_per_query_max
    )


def weighted_score(metrics: Mapping[str, float]) -> float:
    """Composite score only meaningful after all thresholds pass (A22)."""
    faithfulness = float(metrics.get("faithfulness", 0.0))
    answer_relevancy = float(metrics.get("answer_relevancy", 0.0))
    hit_at_k_final = float(metrics.get("hit_at_k_final", 0.0))
    mrr = float(metrics.get("mrr", 0.0))
    return (
        FAITHFULNESS_WEIGHT * faithfulness
        + ANSWER_RELEVANCY_WEIGHT * answer_relevancy
        + HIT_AT_K_FINAL_WEIGHT * hit_at_k_final
        + MRR_WEIGHT * mrr
    )


def policy_view(policy: EvaluationPolicySnapshot) -> dict[str, Any]:
    """The policy subset exposed to the frontend (leaderboard response)."""
    return {
        "policy_version": policy.policy_version,
        "min_real_queries": policy.min_real_queries,
        "shadow_max_examples": policy.shadow_max_examples,
        "shadow_max_candidate_configs": policy.shadow_max_candidate_configs,
        "calibration_open_score_gap": policy.calibration_open_score_gap,
        "cold_start_sample_rate": policy.cold_start_sample_rate,
        "sentinel_sample_rate": policy.sentinel_sample_rate,
    }


__all__ = [
    "DEFAULT_POLICY_VERSION",
    "build_comparator_key",
    "default_policy_snapshot",
    "policy_view",
    "threshold_eligibility",
    "validate_policy",
    "weighted_score",
]
