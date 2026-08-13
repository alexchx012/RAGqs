"""Read-model dataclasses for the evaluation & calibration domain."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from app.chat.models import CalibrationWindowSnapshot
from app.platform.errors import PlatformError

ShadowRunState = Literal["queued", "running", "retry_wait", "succeeded", "failed", "cancelled"]
WindowStatus = Literal["open", "closing", "closed"]
WindowKind = Literal["cold_start", "sentinel", "manual"]
SuggestionStatus = Literal["not_actionable", "actionable", "superseded", "consumed"]


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.isoformat() + "Z"
    return value.astimezone().isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class EvaluationPolicySnapshot:
    """Immutable, versioned evaluation policy fact."""

    policy_version: str
    faithfulness_min: float
    refusal_rate_min: float
    hit_at_k_final_min: float
    mrr_min: float
    p95_latency_max_ms: int
    cost_per_query_max: float
    min_real_queries: int
    shadow_max_examples: int
    shadow_max_candidate_configs: int
    calibration_open_score_gap: float
    cold_start_sample_rate: float
    sentinel_sample_rate: float
    pair_vote_ttl_seconds: int
    close_grace_seconds: int
    max_attempts: int
    run_deadline_seconds: int
    lease_seconds: int
    heartbeat_seconds: int
    concurrency: int
    judge_k: int
    created_at_utc: datetime

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise PlatformError("evaluation_policy_invalid", "Policy version is required", {}, 422)
        if self.judge_k < 1:
            raise PlatformError("evaluation_policy_invalid", "judge_k must be positive", {}, 422)
        thresholds = {
            "faithfulness_min": self.faithfulness_min,
            "refusal_rate_min": self.refusal_rate_min,
            "hit_at_k_final_min": self.hit_at_k_final_min,
            "mrr_min": self.mrr_min,
            "p95_latency_max_ms": self.p95_latency_max_ms,
            "cost_per_query_max": self.cost_per_query_max,
        }
        for name, value in thresholds.items():
            if value is None or value <= 0:
                raise PlatformError(
                    "evaluation_policy_invalid",
                    f"{name} must be a positive non-zero threshold",
                    {"field": name},
                    422,
                )

    def to_json(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "faithfulness_min": self.faithfulness_min,
            "refusal_rate_min": self.refusal_rate_min,
            "hit_at_k_final_min": self.hit_at_k_final_min,
            "mrr_min": self.mrr_min,
            "p95_latency_max_ms": self.p95_latency_max_ms,
            "cost_per_query_max": self.cost_per_query_max,
            "min_real_queries": self.min_real_queries,
            "shadow_max_examples": self.shadow_max_examples,
            "shadow_max_candidate_configs": self.shadow_max_candidate_configs,
            "calibration_open_score_gap": self.calibration_open_score_gap,
            "cold_start_sample_rate": self.cold_start_sample_rate,
            "sentinel_sample_rate": self.sentinel_sample_rate,
            "pair_vote_ttl_seconds": self.pair_vote_ttl_seconds,
            "close_grace_seconds": self.close_grace_seconds,
            "max_attempts": self.max_attempts,
            "run_deadline_seconds": self.run_deadline_seconds,
            "lease_seconds": self.lease_seconds,
            "heartbeat_seconds": self.heartbeat_seconds,
            "concurrency": self.concurrency,
            "judge_k": self.judge_k,
            "created_at_utc": _iso(self.created_at_utc),
        }


@dataclass(frozen=True, slots=True)
class ShadowRunRecord:
    run_id: str
    space_id: str
    state: str
    attempt: int
    lease_owner: str | None
    lease_expires_at: datetime | None
    heartbeat_at: datetime | None
    fencing_token: str | None
    next_attempt_at: datetime | None
    failure_class: str | None
    progress: Mapping[str, Any]
    report_ref: str | None
    policy_version: str
    comparator_key: str | None
    candidate_config_versions: tuple[str, ...]
    index_generation_id: str
    index_revision: int
    frozen_snapshot: Mapping[str, Any]
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    version: int


@dataclass(frozen=True, slots=True)
class RunReadModel:
    run_id: str
    state: str
    attempt: int
    progress: Mapping[str, Any]
    failure_class: str | None
    report_ref: str | None
    policy_version: str
    comparator_key: str | None
    candidate_config_versions: tuple[str, ...]
    index_generation_id: str
    index_revision: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "state": self.state,
            "attempt": self.attempt,
            "progress": {
                key: value
                for key, value in self.progress.items()
                if key in {"total", "completed", "failed"}
            },
            "failure_class": self.failure_class,
            "report_ref": self.report_ref,
            "policy_version": self.policy_version,
            "comparator_key": self.comparator_key,
            "candidate_config_versions": list(self.candidate_config_versions),
            "index_generation_id": self.index_generation_id,
            "index_revision": self.index_revision,
            "created_at": _iso(self.created_at),
            "started_at": _iso(self.started_at),
            "completed_at": _iso(self.completed_at),
        }


@dataclass(frozen=True, slots=True)
class WindowSnapshot:
    window_id: str | None
    status: str
    opened_at: datetime | None
    closed_at: datetime | None
    pairs_collected: int
    close_deadline_at: datetime | None
    window_kind: str | None
    policy_version: str | None
    sample_rate: float
    opened_by: str | None
    closed_by: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "status": self.status,
            "opened_at": _iso(self.opened_at),
            "closed_at": _iso(self.closed_at),
            "pairs_collected": self.pairs_collected,
            "close_deadline_at": _iso(self.close_deadline_at),
            "window_kind": self.window_kind,
            "policy_version": self.policy_version,
            "sample_rate": self.sample_rate,
            "opened_by": self.opened_by,
            "closed_by": self.closed_by,
        }

    def to_chat_snapshot(self) -> CalibrationWindowSnapshot:
        if self.window_id is None or self.status != "open":
            raise PlatformError(
                "calibration_window_not_open",
                "The calibration window is not open",
                {},
                409,
            )
        return CalibrationWindowSnapshot(
            window_id=self.window_id,
            status=self.status,
            policy_version=self.policy_version or "",
            sample_rate=self.sample_rate,
            window_kind=self.window_kind or "manual",
            expires_at_utc=self.close_deadline_at,
            close_deadline_at_utc=self.close_deadline_at,
            pair_vote_ttl_seconds=None,
        )


@dataclass(frozen=True, slots=True)
class LeaderboardEntry:
    rank: int
    name: str
    score: float
    metrics: Mapping[str, float]
    eligible: bool
    is_active: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "name": self.name,
            "score": self.score,
            "metrics": dict(self.metrics),
            "eligible": self.eligible,
            "is_active": self.is_active,
        }


@dataclass(frozen=True, slots=True)
class JudgeScores:
    faithfulness: float | None
    answer_relevancy: float | None
    is_refusal: bool = False
    latency_ms: int | None = None


@dataclass(frozen=True, slots=True)
class SuggestionRecord:
    suggestion_id: str
    space_id: str
    policy_version: str
    comparator_key: str | None
    rank_summary: Mapping[str, Any]
    status: str
    version: int
    created_at: datetime
    invalidated_at: datetime | None = None
    consumed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SampleSnapshotItem:
    item_id: str
    position: int
    question_text: str
    question_hash: str
    evidence_hash: str
    weak_signals: Mapping[str, Any] = field(default_factory=dict)
    source_ref: str = ""


__all__ = [
    "EvaluationPolicySnapshot",
    "JudgeScores",
    "LeaderboardEntry",
    "RunReadModel",
    "SampleSnapshotItem",
    "ShadowRunRecord",
    "SuggestionRecord",
    "WindowSnapshot",
]
