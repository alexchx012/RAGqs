"""Public graph build-run persistence schema.

The public graph is a greenfield rewrite: these tables are created from an
empty schema and never import, migrate, dual-read or wrap any legacy graph,
graph service, API, response or persisted data.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    literal_column,
    text,
)

graph_metadata = MetaData()

graph_build_runs_table = Table(
    "graph_build_runs",
    graph_metadata,
    Column("id", String(64), primary_key=True),
    Column("version", Integer, nullable=False),
    Column("state", String(16), nullable=False),
    Column("initiator_identity_id", String(64), nullable=False),
    Column("source_revision", Integer, nullable=False),
    Column("source_manifest_id", String(64), nullable=False),
    Column("source_manifest_hash", String(64), nullable=False),
    Column("source_head_fence", Integer, nullable=False),
    Column("publications_json", JSON, nullable=False),
    Column("target_generation_id", String(64), nullable=False),
    Column("target_generation_fence", String(64), nullable=False),
    Column("component_manifest_slot", String(32), nullable=False),
    Column("component_stage_id", String(64), nullable=True),
    Column("grant_operation_id", String(64), nullable=False),
    Column("grant_expires_at_utc", DateTime(timezone=True), nullable=False),
    Column("config_snapshot_json", JSON, nullable=False),
    Column("plan_snapshot_json", JSON, nullable=False),
    Column("estimated_primary_model_calls", Integer, nullable=False),
    Column("actual_primary_model_calls", Integer, nullable=False),
    Column("actual_provider_calls", Integer, nullable=False),
    Column("current_attempt", Integer, nullable=False),
    Column("lease_owner", String(64), nullable=True),
    Column("lease_expires_at_utc", DateTime(timezone=True), nullable=True),
    Column("heartbeat_at_utc", DateTime(timezone=True), nullable=True),
    Column("fencing_token", String(64), nullable=True),
    Column("failure_class", String(64), nullable=True),
    Column("failure_reason", String(512), nullable=True),
    Column("graph_generation_id", String(64), nullable=True),
    Column("index_generation_id", String(64), nullable=True),
    Column("activation_receipt_id", String(64), nullable=True),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("started_at_utc", DateTime(timezone=True), nullable=True),
    Column("completed_at_utc", DateTime(timezone=True), nullable=True),
)

graph_build_attempts_table = Table(
    "graph_build_attempts",
    graph_metadata,
    Column("run_id", String(64), primary_key=True),
    Column("attempt", Integer, primary_key=True),
    Column("lease_owner", String(64), nullable=False),
    Column("lease_expires_at_utc", DateTime(timezone=True), nullable=False),
    Column("heartbeat_at_utc", DateTime(timezone=True), nullable=False),
    Column("fencing_token", String(64), nullable=False),
    Column("outcome", String(16), nullable=False),
    Column("started_at_utc", DateTime(timezone=True), nullable=False),
    Column("completed_at_utc", DateTime(timezone=True), nullable=True),
)

graph_staging_resources_table = Table(
    "graph_staging_resources",
    graph_metadata,
    Column("id", String(64), primary_key=True),
    Column("run_id", String(64), nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("fencing_token", String(64), nullable=False),
    Column("resource_kind", String(32), nullable=False),
    Column("resource_id", String(64), nullable=False),
    Column("payload_json", JSON, nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "run_id",
        "attempt",
        "resource_kind",
        "resource_id",
        name="uq_graph_staging_resource_identity",
    ),
)

graph_build_audit_table = Table(
    "graph_build_audit",
    graph_metadata,
    Column("id", String(64), primary_key=True),
    Column("run_id", String(64), nullable=False),
    Column("attempt", Integer, nullable=True),
    Column("version", Integer, nullable=False),
    Column("event_kind", String(64), nullable=False),
    Column("actor", String(64), nullable=False),
    Column("failure_class", String(64), nullable=True),
    Column("trace_id", String(64), nullable=True),
    Column("details_json", JSON, nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
)

graph_build_operations_table = Table(
    "graph_build_operations",
    graph_metadata,
    Column("operation_id", String(64), primary_key=True),
    Column("kind", String(32), nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("status", String(16), nullable=False),
    Column("response_json", JSON, nullable=True),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
)

Index(
    "ix_graph_build_runs_state_created",
    graph_build_runs_table.c.state,
    graph_build_runs_table.c.created_at_utc,
)
Index(
    "ix_graph_build_runs_lease_expiry",
    graph_build_runs_table.c.state,
    graph_build_runs_table.c.lease_expires_at_utc,
)
Index(
    "ix_graph_build_audit_run_created",
    graph_build_audit_table.c.run_id,
    graph_build_audit_table.c.created_at_utc,
)
Index(
    "uq_graph_build_single_active_run",
    literal_column("(1)"),
    unique=True,
    sqlite_where=text("state IN ('queued', 'running')"),
    postgresql_where=text("state IN ('queued', 'running')"),
)

__all__ = [
    "graph_build_attempts_table",
    "graph_build_audit_table",
    "graph_build_operations_table",
    "graph_build_runs_table",
    "graph_metadata",
    "graph_staging_resources_table",
]
