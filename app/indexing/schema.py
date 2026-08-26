from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)

indexing_metadata = MetaData()


index_generations_table = Table(
    "index_generations",
    indexing_metadata,
    Column("id", String(128), primary_key=True),
    Column("status", String(32), nullable=False),
    Column("base_revision", BigInteger, nullable=False),
    Column("applied_revision", BigInteger, nullable=False),
    Column("manifest_json", JSON, nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("activated_at_utc", DateTime(timezone=True), nullable=True),
    Column("retired_at_utc", DateTime(timezone=True), nullable=True),
    Column("rollback_until_utc", DateTime(timezone=True), nullable=True),
    Column("rollback_applied_revision", BigInteger, nullable=True),
    CheckConstraint(
        "status IN ('staging','active','retired','failed','purging','purged')",
        name="ck_index_generations_status",
    ),
    CheckConstraint("base_revision >= 0", name="ck_index_generations_base_revision"),
    CheckConstraint(
        "applied_revision >= base_revision", name="ck_index_generations_applied_revision"
    ),
)


index_generation_heads_table = Table(
    "index_generation_heads",
    indexing_metadata,
    Column("id", String(32), primary_key=True),
    Column("active_generation_id", String(128), nullable=False),
    Column("rollback_candidate_id", String(128), nullable=True),
    Column("current_revision", BigInteger, nullable=False),
    Column("updated_at_utc", DateTime(timezone=True), nullable=False),
    CheckConstraint("id = 'instance'", name="ck_index_generation_heads_singleton"),
    CheckConstraint("current_revision >= 0", name="ck_index_generation_heads_revision"),
)


index_generation_changes_table = Table(
    "index_generation_changes",
    indexing_metadata,
    Column("generation_id", String(128), nullable=False),
    Column("revision", BigInteger, nullable=False),
    Column("change_type", String(32), nullable=False),
    Column("change_json", JSON, nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    UniqueConstraint("generation_id", "revision", name="uq_index_generation_change_revision"),
    CheckConstraint("revision >= 1", name="ck_index_generation_change_revision"),
    CheckConstraint(
        "change_type IN ('publish','replace','reindex','delete')",
        name="ck_index_generation_change_type",
    ),
)


index_chunks_table = Table(
    "index_chunks",
    indexing_metadata,
    Column("id", String(128), primary_key=True),
    Column("generation_id", String(128), primary_key=True),
    Column("publication_id", String(128), primary_key=True),
    Column("document_id", String(128), nullable=False),
    Column("document_version_id", String(128), nullable=False),
    Column("space_id", String(128), nullable=False),
    Column("text", String, nullable=False),
    Column("embedding_text", String, nullable=False),
    Column("sparse_text", String, nullable=True),
    Column("locator_json", JSON, nullable=False),
    Column("snippet", String, nullable=True),
    Column("media_kind", String(128), nullable=False),
    Column("manifest_hash", String(128), nullable=False),
    Column("metadata_json", JSON, nullable=False),
    Column("indexable", Boolean, nullable=False),
)
Index(
    "ix_index_chunks_generation_space",
    index_chunks_table.c.generation_id,
    index_chunks_table.c.space_id,
)
Index(
    "ix_index_chunks_document_version",
    index_chunks_table.c.document_id,
    index_chunks_table.c.document_version_id,
)


index_generation_leases_table = Table(
    "index_generation_leases",
    indexing_metadata,
    Column("id", String(128), primary_key=True),
    Column("generation_id", String(128), nullable=False),
    Column("component_kind", String(64), nullable=True),
    Column("manifest_hash", String(128), nullable=True),
    Column("source_head_fence", BigInteger, nullable=True),
    Column("expires_at_utc", DateTime(timezone=True), nullable=False),
    Column("released_at_utc", DateTime(timezone=True), nullable=True),
    Column("lease_kind", String(32), nullable=False, server_default="reference"),
    Column("owner_id", String(128), nullable=False, server_default="indexing"),
    Column("fence_token", BigInteger, nullable=False, server_default="1"),
)
Index(
    "ix_index_generation_leases_expiry",
    index_generation_leases_table.c.generation_id,
    index_generation_leases_table.c.expires_at_utc,
)


index_graph_components_table = Table(
    "index_graph_components",
    indexing_metadata,
    Column("id", String(128), primary_key=True),
    Column("generation_id", String(128), nullable=False),
    Column("component_state", String(32), nullable=False),
    Column("target_generation_fence", String(128), nullable=False),
    Column("source_revision", BigInteger, nullable=False),
    Column("source_manifest_hash", String(128), nullable=False),
    Column("source_head_fence", BigInteger, nullable=False),
    Column("manifest_json", JSON, nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "component_state IN ('staged','ready','disabled','stale','failed')",
        name="ck_index_graph_component_state",
    ),
)
Index("ix_index_graph_components_generation", index_graph_components_table.c.generation_id)


retrieval_releases_table = Table(
    "retrieval_releases",
    indexing_metadata,
    Column("id", String(128), primary_key=True),
    Column("generation_id", String(128), nullable=False),
    Column("profile_id", String(128), nullable=False),
    Column("version", String(64), nullable=False),
    Column("profile_json", JSON, nullable=False),
    Column("acceptance_suite_json", JSON, nullable=False),
    Column("gate_version_id", String(128), nullable=True),
    Column("gate_judgment_json", JSON, nullable=True),
    Column("state", String(32), nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    CheckConstraint("state IN ('staged','released','failed')", name="ck_retrieval_releases_state"),
    UniqueConstraint("generation_id", "profile_id", "version", name="uq_retrieval_release_version"),
)


retrieval_release_gate_table = Table(
    "retrieval_release_gates",
    indexing_metadata,
    Column("id", String(128), primary_key=True),
    Column("version", String(64), nullable=False),
    Column("hardware_profile_json", JSON, nullable=False),
    Column("concurrency", Integer, nullable=False),
    Column("effective_from_utc", DateTime(timezone=True), nullable=False),
    Column("effective_to_utc", DateTime(timezone=True), nullable=True),
    Column("supersedes_version_id", String(128), nullable=True),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    # 注意：与 price_catalog 同模式——append-only 语义（不可 DELETE；UPDATE 仅允许
    # 一次性 close 且其余字段逐列不变）由 0038 迁移的 DB trigger 强制，metadata
    # 无法表达 trigger 行为，此处不虚构。开放版本唯一性由服务注册事务保证
    # （部署侧发布，无运行时并发注册路径）。
    CheckConstraint(
        "effective_to_utc IS NULL OR effective_to_utc > effective_from_utc",
        name="ck_retrieval_release_gate_interval",
    ),
    CheckConstraint("concurrency >= 1", name="ck_retrieval_release_gate_concurrency"),
    UniqueConstraint("version", name="uq_retrieval_release_gate_version"),
)


retrieval_release_gate_metric_table = Table(
    "retrieval_release_gate_metrics",
    indexing_metadata,
    Column("id", String(128), primary_key=True),
    Column("gate_version_id", String(128), nullable=False),
    Column("metric", String(32), nullable=False),
    Column("direction", String(8), nullable=False),
    Column("absolute_threshold", Float, nullable=False),
    Column("allowed_regression", Float, nullable=False),
    Column("min_samples", Integer, nullable=False),
    Column("aggregation", String(16), nullable=False),
    Column("severity", String(16), nullable=False),
    CheckConstraint("direction IN ('above','below')", name="ck_retrieval_gate_metric_direction"),
    CheckConstraint(
        "absolute_threshold >= 0", name="ck_retrieval_gate_metric_threshold_nonnegative"
    ),
    CheckConstraint(
        "allowed_regression >= 0 AND allowed_regression < 1",
        name="ck_retrieval_gate_metric_regression_range",
    ),
    CheckConstraint("min_samples >= 1", name="ck_retrieval_gate_metric_min_samples"),
    CheckConstraint(
        "aggregation IN ('mean','max','p50','p95','p99','rate')",
        name="ck_retrieval_gate_metric_aggregation",
    ),
    CheckConstraint(
        "severity IN ('blocking','advisory')", name="ck_retrieval_gate_metric_severity"
    ),
    UniqueConstraint("gate_version_id", "metric", name="uq_retrieval_gate_metric"),
)


index_operations_table = Table(
    "index_operations",
    indexing_metadata,
    Column("operation_id", String(128), primary_key=True),
    Column("operation_kind", String(64), nullable=False),
    Column("request_fingerprint", String(128), nullable=False),
    Column("state", String(32), nullable=False),
    Column("response_json", JSON, nullable=True),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("completed_at_utc", DateTime(timezone=True), nullable=True),
    CheckConstraint(
        "state IN ('reserved','accepted','blocked','completed','failed')",
        name="ck_index_operations_state",
    ),
)


INDEXING_TABLE_NAMES = frozenset(indexing_metadata.tables)


__all__ = [
    "INDEXING_TABLE_NAMES",
    "index_chunks_table",
    "index_generation_changes_table",
    "index_generation_heads_table",
    "index_generation_leases_table",
    "index_generations_table",
    "index_graph_components_table",
    "index_operations_table",
    "indexing_metadata",
    "retrieval_releases_table",
]
