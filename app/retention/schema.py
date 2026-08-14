"""Retention-owned persistence facts.

Only orchestration bookkeeping lives here: reconciliation runs and findings,
plus opaque hook receipts. Business facts stay with their owning domains.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
)

retention_metadata = MetaData()

retention_reconciliation_runs_table = Table(
    "retention_reconciliation_runs",
    retention_metadata,
    Column("id", String(128), primary_key=True),
    Column("scope", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("source_snapshot_json", JSON, nullable=False),
    Column("finding_counts_json", JSON, nullable=False),
    Column("started_at_utc", DateTime(timezone=True), nullable=False),
    Column("completed_at_utc", DateTime(timezone=True), nullable=True),
    CheckConstraint(
        "status IN ('running','completed','failed')",
        name="ck_retention_reconciliation_runs_status",
    ),
)

retention_reconciliation_findings_table = Table(
    "retention_reconciliation_findings",
    retention_metadata,
    Column("id", String(128), primary_key=True),
    Column("run_id", String(128), nullable=False),
    Column("category", String(32), nullable=False),
    Column("resource_type", String(64), nullable=False),
    Column("resource_id", String(128), nullable=False),
    Column("detail", String(512), nullable=False),
    Column("repairable", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("hook_operation_id", String(128), nullable=True),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("updated_at_utc", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "category IN ('info','repairable','blocking')",
        name="ck_retention_reconciliation_findings_category",
    ),
    CheckConstraint("repairable IN (0, 1)", name="ck_retention_reconciliation_findings_repairable"),
    CheckConstraint(
        "status IN ('open','repaired','ignored')",
        name="ck_retention_reconciliation_findings_status",
    ),
    Index("ix_retention_findings_run", "run_id"),
    Index("ix_retention_findings_status", "status"),
)

retention_hook_receipts_table = Table(
    "retention_hook_receipts",
    retention_metadata,
    Column("operation_id", String(128), primary_key=True),
    Column("kind", String(32), nullable=False),
    Column("target_id", String(128), nullable=False),
    Column("receipt_json", JSON, nullable=False),
    Column("state", String(32), nullable=False),
    Column("last_error", String(256), nullable=True),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("updated_at_utc", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "kind IN ('index_gc','graph_component_gc','account_compaction')",
        name="ck_retention_hook_receipts_kind",
    ),
    CheckConstraint(
        "state IN ('accepted','blocked','completed','terminal','purged')",
        name="ck_retention_hook_receipts_state",
    ),
)
