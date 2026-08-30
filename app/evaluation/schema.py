"""Greenfield evaluation & calibration persistence schema.

These tables are created from an empty schema. The evaluation domain owns the
``evaluation_*`` and ``calibration_window*`` facts only; it reads chat/indexing/
identity/usage facts and never owns conversation, message, generation, feedback,
A/B pair/vote, document, publication, ACL, usage/quota or outbox tables.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    text,
)

evaluation_metadata = MetaData()

evaluation_policy_table = Table(
    "evaluation_policy",
    evaluation_metadata,
    Column("policy_version", String(64), primary_key=True),
    Column("faithfulness_min", Float, nullable=False),
    Column("refusal_rate_min", Float, nullable=False),
    Column("hit_at_k_final_min", Float, nullable=False),
    Column("mrr_min", Float, nullable=False),
    Column("p95_latency_max_ms", Integer, nullable=False),
    Column("cost_per_query_max", Float, nullable=False),
    Column("min_real_queries", Integer, nullable=False),
    Column("shadow_max_examples", Integer, nullable=False),
    Column("shadow_max_candidate_configs", Integer, nullable=False),
    Column("calibration_open_score_gap", Float, nullable=False),
    Column("cold_start_sample_rate", Float, nullable=False),
    Column("sentinel_sample_rate", Float, nullable=False),
    Column("pair_vote_ttl_seconds", Integer, nullable=False),
    Column("close_grace_seconds", Integer, nullable=False),
    Column("max_attempts", Integer, nullable=False),
    Column("run_deadline_seconds", Integer, nullable=False),
    Column("lease_seconds", Integer, nullable=False),
    Column("heartbeat_seconds", Integer, nullable=False),
    Column("concurrency", Integer, nullable=False),
    Column("judge_k", Integer, nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "min_real_queries >= 20 AND min_real_queries <= 500",
        name="ck_evaluation_policy_min_real_queries",
    ),
    CheckConstraint(
        "shadow_max_examples >= 20 AND shadow_max_examples <= 500",
        name="ck_evaluation_policy_shadow_max_examples",
    ),
    CheckConstraint(
        "shadow_max_candidate_configs >= 2 AND shadow_max_candidate_configs <= 5",
        name="ck_evaluation_policy_shadow_max_candidate_configs",
    ),
    CheckConstraint(
        "calibration_open_score_gap >= 0.01 AND calibration_open_score_gap <= 0.10",
        name="ck_evaluation_policy_open_score_gap",
    ),
    CheckConstraint(
        "cold_start_sample_rate >= 0 AND cold_start_sample_rate <= 0.5",
        name="ck_evaluation_policy_cold_start_sample_rate",
    ),
    CheckConstraint(
        "sentinel_sample_rate >= 0 AND sentinel_sample_rate <= 0.05",
        name="ck_evaluation_policy_sentinel_sample_rate",
    ),
)

evaluation_golden_set_version_table = Table(
    "evaluation_golden_set_version",
    evaluation_metadata,
    Column("space_id", String(128), primary_key=True),
    Column("version", String(64), primary_key=True),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
)

evaluation_golden_item_table = Table(
    "evaluation_golden_item",
    evaluation_metadata,
    Column("item_id", String(64), primary_key=True),
    Column("space_id", String(128), nullable=False),
    Column("golden_version", String(64), nullable=False),
    Column("question_text", Text, nullable=False),
    Column("question_hash", String(128), nullable=False),
    Column("expected_sources_json", JSON, nullable=False),
    Column("expects_refusal", Boolean, nullable=False),
    Column("evidence_hash", String(128), nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["space_id", "golden_version"],
        ["evaluation_golden_set_version.space_id", "evaluation_golden_set_version.version"],
        name="fk_evaluation_golden_item_set_version",
    ),
    Index("ix_evaluation_golden_item_set", "space_id", "golden_version"),
    Index("ix_evaluation_golden_item_hash", "space_id", "golden_version", "question_hash"),
)

evaluation_sample_snapshot_table = Table(
    "evaluation_sample_snapshot",
    evaluation_metadata,
    Column("snapshot_id", String(64), primary_key=True),
    Column("space_id", String(128), nullable=False),
    Column("policy_version", String(64), nullable=False),
    Column("comparator_key", String(256), nullable=True),
    Column("candidate_config_versions_json", JSON, nullable=False),
    Column("index_generation_id", String(64), nullable=False),
    Column("index_revision", Integer, nullable=False),
    Column("sample_count", Integer, nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
)

evaluation_sample_snapshot_item_table = Table(
    "evaluation_sample_snapshot_item",
    evaluation_metadata,
    Column("snapshot_id", String(64), primary_key=True),
    Column("item_id", String(64), primary_key=True),
    Column("position", Integer, nullable=False),
    Column("question_text", Text, nullable=False),
    Column("question_hash", String(128), nullable=False),
    Column("evidence_hash", String(128), nullable=False),
    Column("weak_signals_json", JSON, nullable=False),
    Column("source_ref", String(256), nullable=False),
    ForeignKeyConstraint(
        ["snapshot_id"],
        ["evaluation_sample_snapshot.snapshot_id"],
        name="fk_evaluation_sample_snapshot_item_snapshot",
    ),
    Index("ix_evaluation_sample_snapshot_item_snapshot", "snapshot_id"),
)

shadow_evaluation_run_table = Table(
    "shadow_evaluation_run",
    evaluation_metadata,
    Column("run_id", String(64), primary_key=True),
    Column("space_id", String(128), nullable=False),
    Column("state", String(16), nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("lease_owner", String(128), nullable=True),
    Column("lease_expires_at_utc", DateTime(timezone=True), nullable=True),
    Column("heartbeat_at_utc", DateTime(timezone=True), nullable=True),
    Column("fencing_token", String(64), nullable=True),
    Column("next_attempt_at_utc", DateTime(timezone=True), nullable=True),
    Column("failure_class", String(64), nullable=True),
    Column("progress_json", JSON, nullable=False),
    Column("report_ref", String(256), nullable=True),
    Column("policy_version", String(64), nullable=False),
    Column("comparator_key", String(256), nullable=True),
    Column("candidate_config_versions_json", JSON, nullable=False),
    Column("index_generation_id", String(64), nullable=False),
    Column("index_revision", Integer, nullable=False),
    Column("frozen_snapshot_json", JSON, nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("started_at_utc", DateTime(timezone=True), nullable=True),
    Column("completed_at_utc", DateTime(timezone=True), nullable=True),
    Column("version", Integer, nullable=False),
    CheckConstraint(
        "state IN ('queued','running','retry_wait','succeeded','failed','cancelled')",
        name="ck_shadow_evaluation_run_state",
    ),
    Index(
        "ix_shadow_evaluation_run_claim",
        "state",
        "next_attempt_at_utc",
    ),
)

shadow_evaluation_result_table = Table(
    "shadow_evaluation_result",
    evaluation_metadata,
    Column("run_id", String(64), primary_key=True),
    Column("sample_item_id", String(64), primary_key=True),
    Column("candidate_config_version", String(64), primary_key=True),
    Column("session_id", String(256), nullable=False),
    Column("metrics_json", JSON, nullable=False),
    Column("weak_signals_json", JSON, nullable=False),
    Column("judged_at_utc", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["run_id"],
        ["shadow_evaluation_run.run_id"],
        name="fk_shadow_evaluation_result_run",
    ),
)

evaluation_active_default_table = Table(
    "evaluation_active_default",
    evaluation_metadata,
    Column("space_id", String(128), primary_key=True),
    Column("candidate_config_version", String(64), nullable=False),
    Column("comparator_key", String(256), nullable=True),
    Column("adopted_at_utc", DateTime(timezone=True), nullable=False),
    Column("source_run_id", String(64), nullable=True),
)

evaluation_ab_golden_seed_table = Table(
    "evaluation_ab_golden_seed",
    evaluation_metadata,
    Column("seed_id", String(64), primary_key=True),
    Column("pair_id", String(64), nullable=False, unique=True),
    Column("space_id", String(128), nullable=False),
    Column("question_text", Text, nullable=False),
    Column("preferred_candidate", Integer, nullable=False),
    Column("preferred_candidate_config_version", String(64), nullable=True),
    Column("preferred_content", Text, nullable=False),
    Column("preferred_citations_json", JSON, nullable=False),
    Column("rejected_candidate", Integer, nullable=False),
    Column("rejected_candidate_config_version", String(64), nullable=True),
    Column("policy_version", String(64), nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    CheckConstraint("preferred_candidate IN (0,1)", name="ck_evaluation_ab_golden_seed_preferred"),
    CheckConstraint("rejected_candidate IN (0,1)", name="ck_evaluation_ab_golden_seed_rejected"),
    Index("ix_evaluation_ab_golden_seed_space", "space_id"),
)

calibration_window_suggestion_table = Table(
    "calibration_window_suggestion",
    evaluation_metadata,
    Column("suggestion_id", String(64), primary_key=True),
    Column("space_id", String(128), nullable=False),
    Column("policy_version", String(64), nullable=False),
    Column("comparator_key", String(256), nullable=True),
    Column("rank_summary_json", JSON, nullable=False),
    Column("status", String(32), nullable=False),
    Column("version", Integer, nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("invalidated_at_utc", DateTime(timezone=True), nullable=True),
    Column("consumed_at_utc", DateTime(timezone=True), nullable=True),
    CheckConstraint(
        "status IN ('not_actionable','actionable','superseded','consumed')",
        name="ck_calibration_window_suggestion_status",
    ),
    CheckConstraint("version >= 1", name="ck_calibration_window_suggestion_version"),
)

calibration_window_table = Table(
    "calibration_window",
    evaluation_metadata,
    Column("window_id", String(64), primary_key=True),
    Column("status", String(16), nullable=False),
    Column("window_kind", String(16), nullable=False),
    Column("policy_version", String(64), nullable=False),
    Column("sample_rate", Float, nullable=False),
    Column("pairs_collected", Integer, nullable=False),
    Column("opened_by", String(64), nullable=False),
    Column("opened_at_utc", DateTime(timezone=True), nullable=False),
    Column("closed_by", String(64), nullable=True),
    Column("closed_at_utc", DateTime(timezone=True), nullable=True),
    Column("close_deadline_at_utc", DateTime(timezone=True), nullable=True),
    Column("version", Integer, nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("updated_at_utc", DateTime(timezone=True), nullable=False),
    CheckConstraint("status IN ('open','closing','closed')", name="ck_calibration_window_status"),
    CheckConstraint(
        "window_kind IN ('cold_start','sentinel','manual')",
        name="ck_calibration_window_kind",
    ),
    Index(
        "uq_calibration_window_single_open",
        "status",
        unique=True,
        sqlite_where=text("status = 'open'"),
        postgresql_where=text("status = 'open'"),
    ),
)

calibration_window_command_table = Table(
    "calibration_window_command",
    evaluation_metadata,
    Column("operator_user_id", String(64), primary_key=True),
    Column("idempotency_key", String(256), primary_key=True),
    Column("action", String(16), nullable=False),
    Column("request_hash", String(128), nullable=False),
    Column("target_window_id", String(64), nullable=True),
    Column("response_json", JSON, nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("completed_at_utc", DateTime(timezone=True), nullable=False),
    CheckConstraint("action IN ('open','close')", name="ck_calibration_window_command_action"),
)

evaluation_run_command_table = Table(
    "evaluation_run_command",
    evaluation_metadata,
    Column("operator_user_id", String(64), primary_key=True),
    Column("idempotency_key", String(256), primary_key=True),
    Column("request_hash", String(128), nullable=False),
    Column("run_id", String(64), nullable=True),
    Column("response_json", JSON, nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("completed_at_utc", DateTime(timezone=True), nullable=False),
)

EVALUATION_TABLE_NAMES = frozenset(evaluation_metadata.tables)

__all__ = [
    "EVALUATION_TABLE_NAMES",
    "calibration_window_command_table",
    "calibration_window_suggestion_table",
    "calibration_window_table",
    "evaluation_ab_golden_seed_table",
    "evaluation_active_default_table",
    "evaluation_golden_item_table",
    "evaluation_golden_set_version_table",
    "evaluation_metadata",
    "evaluation_policy_table",
    "evaluation_run_command_table",
    "evaluation_sample_snapshot_item_table",
    "evaluation_sample_snapshot_table",
    "shadow_evaluation_result_table",
    "shadow_evaluation_run_table",
]
