from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Index,
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
    Column("state", String(32), nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    CheckConstraint("state IN ('staged','released','failed')", name="ck_retrieval_releases_state"),
    UniqueConstraint("generation_id", "profile_id", "version", name="uq_retrieval_release_version"),
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
