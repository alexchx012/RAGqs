"""Backup/restore orchestration persistence facts.

Only orchestration bookkeeping lives here: backup sets and their components,
restore sessions, per-stage/per-target state, the repair queue and the
maintenance read gate. Business facts stay with their owning domains;
Postgres and object storage remain the only authoritative sources.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    text,
)

backup_metadata = MetaData()

backup_sets_table = Table(
    "backup_sets",
    backup_metadata,
    Column("id", String(128), primary_key=True),
    Column("status", String(32), nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("completed_at_utc", DateTime(timezone=True), nullable=True),
    # Purged backups keep status='complete' for history; restorability is
    # status='complete' AND purged_at_utc IS NULL (retention expiry marks this
    # column only after the provider delete protocol finished).
    Column("purged_at_utc", DateTime(timezone=True), nullable=True),
    CheckConstraint(
        "status IN ('creating','complete','failed')",
        name="ck_backup_sets_status",
    ),
)

# A backup set binds three components to one stable backup_id; it may only be
# marked complete once all three components succeeded (design §2.8).
backup_components_table = Table(
    "backup_components",
    backup_metadata,
    Column("id", String(128), primary_key=True),
    Column("backup_id", String(128), ForeignKey("backup_sets.id"), nullable=False),
    Column(
        "kind",
        String(32),
        nullable=False,
    ),
    Column("status", String(32), nullable=False),
    Column("reference", String(512), nullable=True),
    Column("failure_reason", String(512), nullable=True),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("completed_at_utc", DateTime(timezone=True), nullable=True),
    UniqueConstraint("backup_id", "kind", name="uq_backup_components_kind"),
    CheckConstraint(
        "kind IN ('postgres_snapshot','object_store_snapshot','object_manifest')",
        name="ck_backup_components_kind",
    ),
    CheckConstraint(
        "status IN ('pending','running','succeeded','failed')",
        name="ck_backup_components_status",
    ),
)

# Object manifest: per-object proof of restorability (opaque object identity,
# size, checksum). Objects are never inferred from derived indexes.
backup_objects_table = Table(
    "backup_objects",
    backup_metadata,
    Column("backup_id", String(128), ForeignKey("backup_sets.id"), nullable=False),
    Column("object_key", String(512), primary_key=True),
    Column("size_bytes", Integer, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("metadata_json", JSON, nullable=False),
    CheckConstraint("size_bytes >= 0", name="ck_backup_objects_size"),
)

# One restore execution, referenced by a stable restore_id bound to a backup_id.
restore_sessions_table = Table(
    "restore_sessions",
    backup_metadata,
    Column("id", String(128), primary_key=True),
    Column("backup_id", String(128), ForeignKey("backup_sets.id"), nullable=False),
    Column("status", String(32), nullable=False),
    Column("failure_reason", String(512), nullable=True),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("updated_at_utc", DateTime(timezone=True), nullable=False),
    Column("completed_at_utc", DateTime(timezone=True), nullable=True),
    CheckConstraint(
        "status IN ('accepted','running','blocked','completed','failed')",
        name="ck_restore_sessions_status",
    ),
    Index("ix_restore_sessions_status", "status"),
    # Durable single-active-restore mutex: at most one session may hold an
    # active status, enforced by the database rather than by a plain query.
    Index(
        "uq_restore_sessions_active",
        "status",
        unique=True,
        sqlite_where=text("status IN ('accepted','running','blocked')"),
        postgresql_where=text("status IN ('accepted','running','blocked')"),
    ),
)

# Fixed ordered stages; a stage only starts after the previous stage committed
# and passed validation (design §2.8).
RESTORE_STAGES: tuple[str, ...] = (
    "postgres",
    "object_store",
    "milvus",
    "sparse",
    "summary",
    "graph",
    "cache",
)

FACT_STAGES: frozenset[str] = frozenset({"postgres", "object_store"})
DERIVED_STAGES: frozenset[str] = frozenset({"milvus", "sparse", "summary", "graph", "cache"})

restore_stages_table = Table(
    "restore_stages",
    backup_metadata,
    Column("id", String(128), primary_key=True),
    Column("restore_id", String(128), ForeignKey("restore_sessions.id"), nullable=False),
    Column("stage", String(32), nullable=False),
    Column("position", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("started_at_utc", DateTime(timezone=True), nullable=True),
    Column("completed_at_utc", DateTime(timezone=True), nullable=True),
    Column("validated", Integer, nullable=False),
    UniqueConstraint("restore_id", "stage", name="uq_restore_stages_stage"),
    CheckConstraint(
        "stage IN ('postgres','object_store','milvus','sparse','summary','graph','cache')",
        name="ck_restore_stages_stage",
    ),
    CheckConstraint(
        "status IN ('pending','running','succeeded','failed')",
        name="ck_restore_stages_status",
    ),
    CheckConstraint("validated IN (0, 1)", name="ck_restore_stages_validated"),
)

# Internal idempotency key: (restore_id, stage, resource_id). Succeeded targets
# are never re-executed; failed targets retry alone with lease/fencing.
restore_targets_table = Table(
    "restore_targets",
    backup_metadata,
    Column("id", String(128), primary_key=True),
    Column("restore_id", String(128), ForeignKey("restore_sessions.id"), nullable=False),
    Column("stage", String(32), nullable=False),
    Column("resource_id", String(128), nullable=False),
    Column("status", String(32), nullable=False),
    Column("failure_classification", String(64), nullable=True),
    Column("attempt", Integer, nullable=False),
    Column("fencing_epoch", Integer, nullable=False),
    Column("lease_token", String(128), nullable=True),
    Column("completed_at_utc", DateTime(timezone=True), nullable=True),
    Column("next_retry_at_utc", DateTime(timezone=True), nullable=True),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("updated_at_utc", DateTime(timezone=True), nullable=False),
    UniqueConstraint("restore_id", "stage", "resource_id", name="uq_restore_targets_key"),
    CheckConstraint(
        "status IN ('pending','running','succeeded','failed')",
        name="ck_restore_targets_status",
    ),
    CheckConstraint("attempt >= 0", name="ck_restore_targets_attempt"),
    CheckConstraint("fencing_epoch >= 0", name="ck_restore_targets_fencing"),
    CheckConstraint(
        "stage IN ('postgres','object_store','milvus','sparse','summary','graph','cache')",
        name="ck_restore_targets_stage",
    ),
)

# Repair queue: fact-source mismatches and derived rebuild failures keep the
# affected resources invisible and retryable. Version-restore precondition
# rejections never enter this queue.
repair_targets_table = Table(
    "repair_targets",
    backup_metadata,
    Column("id", String(128), primary_key=True),
    Column("restore_id", String(128), ForeignKey("restore_sessions.id"), nullable=False),
    Column("stage", String(32), nullable=False),
    Column("resource_id", String(128), nullable=False),
    Column("failure_classification", String(64), nullable=False),
    Column("detail", String(512), nullable=False),
    Column("status", String(32), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("next_retry_at_utc", DateTime(timezone=True), nullable=True),
    Column("resolved_at_utc", DateTime(timezone=True), nullable=True),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("updated_at_utc", DateTime(timezone=True), nullable=False),
    UniqueConstraint("restore_id", "stage", "resource_id", name="uq_repair_targets_key"),
    CheckConstraint(
        "status IN ('open','succeeded')",
        name="ck_repair_targets_status",
    ),
    CheckConstraint("attempts >= 0", name="ck_repair_targets_attempts"),
)

# Single-row maintenance gate persisted in Postgres so worker restarts and
# other processes can read the closed state (design §2.8).
maintenance_gate_table = Table(
    "maintenance_gate",
    backup_metadata,
    Column("id", Integer, primary_key=True),
    Column("reads_closed", Integer, nullable=False),
    Column("restore_id", String(128), nullable=True),
    Column("updated_at_utc", DateTime(timezone=True), nullable=False),
    CheckConstraint("id = 1", name="ck_maintenance_gate_single_row"),
    CheckConstraint("reads_closed IN (0, 1)", name="ck_maintenance_gate_reads_closed"),
)

# Single-row versioned backup schedule/retention policy (Q5/Q6). The policy is
# read and patched through the ops layer with optimistic concurrency on
# `version`; `next_run_at` is derived from these fields, never stored.
backup_policy_table = Table(
    "backup_policy",
    backup_metadata,
    Column("id", Integer, primary_key=True),
    Column("enabled", Integer, nullable=False),
    Column("frequency", String(16), nullable=False),
    Column("local_time", String(5), nullable=False),
    Column("weekdays", JSON, nullable=False),
    Column("timezone", String(64), nullable=False),
    Column("keep_last", Integer, nullable=False),
    Column("retention_days", Integer, nullable=False),
    Column("version", Integer, nullable=False),
    Column("last_scheduled_for_utc", DateTime(timezone=True), nullable=True),
    Column("last_outcome", String(32), nullable=True),
    Column("updated_by", String(128), nullable=True),
    Column("updated_at_utc", DateTime(timezone=True), nullable=False),
    CheckConstraint("id = 1", name="ck_backup_policy_single_row"),
    CheckConstraint("enabled IN (0, 1)", name="ck_backup_policy_enabled"),
    CheckConstraint("frequency IN ('daily','weekly')", name="ck_backup_policy_frequency"),
    CheckConstraint("keep_last >= 1", name="ck_backup_policy_keep_last"),
    CheckConstraint("retention_days >= 1", name="ck_backup_policy_retention_days"),
    CheckConstraint("version >= 1", name="ck_backup_policy_version"),
)

# Durable schedule occurrence identity (Q8): one row per due window so a
# restarted worker converges on the same occurrence instead of fabricating an
# HTTP idempotency key.
backup_schedule_occurrences_table = Table(
    "backup_schedule_occurrences",
    backup_metadata,
    Column("id", String(128), primary_key=True),
    Column("scheduled_for_utc", DateTime(timezone=True), nullable=False),
    Column("backup_id", String(128), ForeignKey("backup_sets.id"), nullable=True),
    Column("outcome", String(32), nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    UniqueConstraint("scheduled_for_utc", name="uq_backup_schedule_occurrences_slot"),
    # 'skipped_missed': an earlier missed window recorded without a re-run; a
    # restarted worker only ever re-runs the most recent missed window (Q5).
    CheckConstraint(
        "outcome IN ('executed','skipped_active_backup','skipped_disabled','skipped_missed')",
        name="ck_backup_schedule_occurrences_outcome",
    ),
)

# External write-command idempotency records (Q8): keyed by
# (operator, endpoint, hashed key); the plaintext Idempotency-Key is never
# stored. The first response snapshot is replayed for same-fingerprint
# retries.
ops_idempotency_commands_table = Table(
    "ops_idempotency_commands",
    backup_metadata,
    Column("id", String(128), primary_key=True),
    Column("operator_user_id", String(128), nullable=False),
    Column("endpoint", String(128), nullable=False),
    Column("key_hash", String(64), nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("response_status", Integer, nullable=False),
    Column("response_json", JSON, nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "operator_user_id",
        "endpoint",
        "key_hash",
        name="uq_ops_idempotency_commands_key",
    ),
)

# Single-row backup write gate (Q7): independent from the maintenance read
# gate. closing/closed reject new business writes with 503 backup_in_progress
# while reads stay available; the worker drains in-flight writes between
# closing and closed.
backup_write_gate_table = Table(
    "backup_write_gate",
    backup_metadata,
    Column("id", Integer, primary_key=True),
    Column("state", String(16), nullable=False),
    Column("backup_id", String(128), nullable=True),
    Column("updated_at_utc", DateTime(timezone=True), nullable=False),
    CheckConstraint("id = 1", name="ck_backup_write_gate_single_row"),
    CheckConstraint("state IN ('open','closing','closed')", name="ck_backup_write_gate_state"),
)

# Durable retention cleanup targets (Q6): provider deletion retries survive
# process restarts; only unfinished deletions are retried.
backup_cleanup_targets_table = Table(
    "backup_cleanup_targets",
    backup_metadata,
    Column("id", String(128), primary_key=True),
    Column("backup_id", String(128), ForeignKey("backup_sets.id"), nullable=False),
    Column("status", String(16), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("next_retry_at_utc", DateTime(timezone=True), nullable=True),
    Column("last_error", String(512), nullable=True),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("updated_at_utc", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "status IN ('pending','deleting','done','failed')",
        name="ck_backup_cleanup_targets_status",
    ),
    CheckConstraint("attempts >= 0", name="ck_backup_cleanup_targets_attempts"),
)
