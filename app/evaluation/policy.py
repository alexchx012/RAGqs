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
from math import ceil, inf
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

# 模型家族清单（后端设计 §8.1：判官不得与被评测管线模型同族）。
# provider + 模型名前缀 → 家族；只收录当前实际使用的家族（qwen 系、deepseek 系）。
# 多族网关（dashscope、bailian）不按 provider 整体映射；仅自名厂商（deepseek、
# qwen）在缺少模型名时可按 provider 名判族，未知组合返回 None、不施加约束。
_MODEL_PREFIX_FAMILIES: tuple[tuple[str, str, str], ...] = (
    ("bailian", "qwen", "qwen"),
    ("dashscope", "qwen", "qwen"),
    ("dashscope", "deepseek", "deepseek"),
    ("deepseek", "deepseek", "deepseek"),
    ("deepseek", "ds", "deepseek"),
)
_PROVIDER_FAMILIES: tuple[tuple[str, str], ...] = (("deepseek", "deepseek"), ("qwen", "qwen"))


def model_family(*, provider: str, model: str | None = None) -> str | None:
    """Resolve the static model family of a (provider, model) pair.

    Model-name prefixes win over provider names; ``None`` means the family is
    unknown and imposes no isolation constraint.
    """
    normalized_provider = provider.strip().casefold()
    if model is not None:
        normalized_model = model.strip().casefold()
        for family_provider, prefix, family in _MODEL_PREFIX_FAMILIES:
            if normalized_provider == family_provider and normalized_model.startswith(prefix):
                return family
    for family_provider, family in _PROVIDER_FAMILIES:
        if normalized_provider == family_provider:
            return family
    return None


def assert_judge_family_isolation(
    *,
    judge_provider: str,
    judge_model: str,
    pipeline_models: Mapping[str, tuple[str, str | None]],
) -> None:
    """Reject a judge model that shares a family with an evaluated-pipeline model.

    ``pipeline_models`` maps a role name (generation, contextual_retrieval,
    reranker, ...) to its ``(provider, model)`` pair; ``model`` may be ``None``
    when settings only carry a provider identity.
    """
    judge = model_family(provider=judge_provider, model=judge_model)
    if judge is None:
        return
    for role, (provider, model) in pipeline_models.items():
        family = model_family(provider=provider, model=model)
        if family == judge:
            raise ValueError(
                "production judge model family conflicts with the evaluated pipeline: "
                f"judge ({judge_provider}/{judge_model}) and {role} "
                f"({provider}/{model if model is not None else provider}) are both "
                f"{family} family"
            )


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


def aggregate_weak_signals(results: list[Mapping[str, Any]]) -> dict[str, float]:
    """Mean weak-signal rates over one candidate config's result rows.

    §8.4: without golden labels the retrieval layer is judged through weak
    signals (answered-with-citation share, 👍👎, A/B votes) instead of hit@k.
    The citation share is the only weak signal the eligibility ladder consumes;
    feedback and vote counts stay visible facts, never retrieval metrics.
    """
    rows = [result.get("weak_signals_json") or {} for result in results]
    citations = [signals.get("weak_has_citation") for signals in rows if signals is not None]
    values = [1.0 if citation else 0.0 for citation in citations if citation is not None]
    clicks = [signals.get("weak_citation_clicks") for signals in rows if signals is not None]
    clicked = [1.0 if count else 0.0 for count in clicks if count is not None]
    return {
        "weak_citation_rate": (sum(values) / len(values)) if values else 0.0,
        "weak_citation_click_rate": (sum(clicked) / len(clicked)) if clicked else 0.0,
        "weak_sampled_items": float(len(values)),
    }


def threshold_eligibility(
    metrics: Mapping[str, float | None],
    policy: EvaluationPolicySnapshot,
    *,
    has_golden: bool = True,
    weak_signal_rate: float | None = None,
) -> bool:
    """Every dimension must pass independently; no cross-dimension compensation (A22).

    ``has_golden=False`` switches the §8.4 replacement ladder: retrieval gates
    (hit@k/MRR) are unmeasurable without golden labels, so the weak-signal
    citation share takes the ``hit_at_k_final_min`` bar and the refusal gate
    (which needs golden ``expects_refusal`` labels) is skipped; generation,
    latency and cost gates keep applying. Without any weak-signal data the
    candidate stays ineligible — missing retrieval evidence is not judged as
    zero-hit, but it is never judged as passing either. A ``None`` cost means
    metering was unavailable, not zero cost: the metric reports null instead of
    fabricating a pass or fail value.
    """
    faithfulness = metrics.get("faithfulness") or 0.0
    p95_latency_ms = metrics.get("p95_latency_ms")
    if p95_latency_ms is None:
        p95_latency_ms = inf
    cost_per_query = metrics.get("cost_per_query")
    generation_ok = (
        faithfulness >= policy.faithfulness_min
        and p95_latency_ms <= float(policy.p95_latency_max_ms)
        and (cost_per_query is None or cost_per_query <= policy.cost_per_query_max)
    )
    if not generation_ok:
        return False
    if not has_golden:
        return weak_signal_rate is not None and weak_signal_rate >= policy.hit_at_k_final_min
    refusal_rate = metrics.get("refusal_rate") or 0.0
    hit_at_k_final = metrics.get("hit_at_k_final") or 0.0
    mrr = metrics.get("mrr") or 0.0
    return bool(
        refusal_rate >= policy.refusal_rate_min
        and hit_at_k_final >= policy.hit_at_k_final_min
        and mrr >= policy.mrr_min
    )


def config_threshold_eligibility(
    config_results: list[Mapping[str, Any]],
    metrics: Mapping[str, float | None],
    policy: EvaluationPolicySnapshot,
    *,
    has_golden: bool,
) -> bool:
    """Threshold eligibility for one candidate config's aggregated metrics.

    With golden labels every gate applies (A22); the §8.4 golden-less path
    swaps the retrieval gates for the weak-signal citation bar.
    """
    if has_golden:
        return threshold_eligibility(metrics, policy)
    weak = aggregate_weak_signals(config_results)
    return threshold_eligibility(
        metrics, policy, has_golden=False, weak_signal_rate=weak["weak_citation_rate"]
    )


def weighted_score(metrics: Mapping[str, float | None]) -> float:
    """Composite score only meaningful after all thresholds pass (A22)."""
    faithfulness = metrics.get("faithfulness") or 0.0
    answer_relevancy = metrics.get("answer_relevancy") or 0.0
    hit_at_k_final = metrics.get("hit_at_k_final") or 0.0
    mrr = metrics.get("mrr") or 0.0
    return (
        FAITHFULNESS_WEIGHT * faithfulness
        + ANSWER_RELEVANCY_WEIGHT * answer_relevancy
        + HIT_AT_K_FINAL_WEIGHT * hit_at_k_final
        + MRR_WEIGHT * mrr
    )


def aggregate_result_metrics(results: list[Mapping[str, Any]]) -> dict[str, float | None]:
    """Mean aggregation per candidate config; p95 latency uses the rank method."""
    keys = (
        "faithfulness",
        "answer_relevancy",
        "refusal_rate",
        "hit_at_k_final",
        "mrr",
        "p95_latency_ms",
        "cost_per_query",
    )
    aggregates: dict[str, float | None] = {}
    for key in keys:
        numbers = [
            float(result["metrics_json"].get(key))
            for result in results
            if result["metrics_json"] and result["metrics_json"].get(key) is not None
        ]
        if key == "p95_latency_ms" and numbers:
            rank = ceil(len(numbers) * 0.95)
            aggregates[key] = sorted(numbers)[rank - 1]
        else:
            aggregates[key] = (sum(numbers) / len(numbers)) if numbers else 0.0
    return aggregates


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
        "faithfulness_min": policy.faithfulness_min,
        "refusal_rate_min": policy.refusal_rate_min,
        "hit_at_k_final_min": policy.hit_at_k_final_min,
        "mrr_min": policy.mrr_min,
        "p95_latency_max_ms": policy.p95_latency_max_ms,
        "cost_per_query_max": policy.cost_per_query_max,
    }


__all__ = [
    "DEFAULT_POLICY_VERSION",
    "aggregate_result_metrics",
    "aggregate_weak_signals",
    "build_comparator_key",
    "config_threshold_eligibility",
    "default_policy_snapshot",
    "policy_view",
    "threshold_eligibility",
    "validate_policy",
    "weighted_score",
]
