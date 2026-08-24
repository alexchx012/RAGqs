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
)

backup_metadata = MetaData()

backup_sets_table = Table(
    "backup_sets",
    backup_metadata,
    Column("id", String(128), primary_key=True),
    Column("status", String(32), nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("completed_at_utc", DateTime(timezone=True), nullable=True),
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
