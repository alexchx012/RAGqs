from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
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

documents_metadata = MetaData()


def _timestamps() -> tuple[Column, Column]:
    return (
        Column("created_at_utc", DateTime(timezone=True), nullable=False),
        Column("updated_at_utc", DateTime(timezone=True), nullable=False),
    )


documents_table = Table(
    "documents",
    documents_metadata,
    Column("id", String(128), primary_key=True),
    Column("space_id", String(128), nullable=True),
    Column("lifecycle_status", String(32), nullable=False),
    Column("active_version_id", String(128), nullable=True),
    Column("pending_version_id", String(128), nullable=True),
    Column("active_operation_job_id", String(128), nullable=True),
    Column("deletion_id", String(128), nullable=True),
    Column("version", Integer, nullable=False),
    Column("name", String(512), nullable=True),
    Column("normalized_name", String(512), nullable=True),
    Column("media_kind", String(128), nullable=True),
    Column("uploaded_at_utc", DateTime(timezone=True), nullable=False),
    Column("created_by_user_id", String(64), nullable=True),
    *_timestamps(),
    CheckConstraint(
        "lifecycle_status IN ('active','pending_delete','deleted')",
        name="ck_documents_lifecycle",
    ),
    CheckConstraint("version >= 1", name="ck_documents_version_positive"),
    UniqueConstraint("space_id", "id", name="uq_documents_space_id"),
)
Index(
    "ix_documents_space_lifecycle", documents_table.c.space_id, documents_table.c.lifecycle_status
)


document_versions_table = Table(
    "document_versions",
    documents_metadata,
    Column("id", String(128), primary_key=True),
    Column("document_id", String(128), ForeignKey("documents.id"), nullable=False),
    Column("version_number", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("content_hash_sha256", String(64), nullable=True),
    Column("object_manifest_json", JSON, nullable=False),
    Column("original_object_key", String(1024), nullable=True),
    Column("file_name", String(512), nullable=True),
    Column("media_kind", String(128), nullable=True),
    Column("size_bytes", BigInteger, nullable=False),
    Column("created_by_user_id", String(64), nullable=True),
    Column("activated_at_utc", DateTime(timezone=True), nullable=True),
    Column("terminal_at_utc", DateTime(timezone=True), nullable=True),
    Column("superseded_at_utc", DateTime(timezone=True), nullable=True),
    Column("purge_after_at_utc", DateTime(timezone=True), nullable=True),
    Column("purged_at_utc", DateTime(timezone=True), nullable=True),
    Column("restored_from_version_id", String(128), nullable=True),
    *_timestamps(),
    CheckConstraint(
        "status IN ('pending','active','superseded','failed','cancelled','purging','purged')",
        name="ck_document_versions_status",
    ),
    CheckConstraint("version_number >= 1", name="ck_document_versions_number_positive"),
    CheckConstraint("size_bytes >= 0", name="ck_document_versions_size_nonnegative"),
    UniqueConstraint("document_id", "version_number", name="uq_document_versions_number"),
)
Index(
    "ix_document_versions_document_status",
    document_versions_table.c.document_id,
    document_versions_table.c.status,
)


document_read_leases_table = Table(
    "document_read_leases",
    documents_metadata,
    Column("id", String(128), primary_key=True),
    Column("document_id", String(128), ForeignKey("documents.id"), nullable=False),
    Column("document_version_id", String(128), ForeignKey("document_versions.id"), nullable=False),
    Column("principal_id", String(64), nullable=False),
    Column("lease_token", String(128), nullable=False),
    Column("expires_at_utc", DateTime(timezone=True), nullable=False),
    *_timestamps(),
    UniqueConstraint(
        "document_version_id", "principal_id", name="uq_document_read_lease_principal"
    ),
)
Index(
    "ix_document_read_leases_expiry",
    document_read_leases_table.c.document_version_id,
    document_read_leases_table.c.expires_at_utc,
)


document_version_restore_holds_table = Table(
    "document_version_restore_holds",
    documents_metadata,
    Column("id", String(128), primary_key=True),
    Column("document_version_id", String(128), ForeignKey("document_versions.id"), nullable=False),
    Column("job_id", String(128), ForeignKey("ingestion_jobs.id"), nullable=False, unique=True),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
)


document_version_cleanup_targets_table = Table(
    "document_version_cleanup_targets",
    documents_metadata,
    Column(
        "document_version_id", String(128), ForeignKey("document_versions.id"), primary_key=True
    ),
    Column("backend_kind", String(64), primary_key=True),
    Column("resource_id", String(1024), primary_key=True),
    Column("state", String(32), nullable=False),
    Column("attempt_count", Integer, nullable=False),
    Column("last_error", String(256), nullable=True),
    *_timestamps(),
    CheckConstraint(
        "state IN ('pending','failed','completed')", name="ck_document_version_cleanup_target_state"
    ),
    CheckConstraint("attempt_count >= 0", name="ck_document_version_cleanup_target_attempts"),
)


upload_batches_table = Table(
    "upload_batches",
    documents_metadata,
    Column("id", String(128), primary_key=True),
    Column("actor_user_id", String(64), nullable=False),
    Column("space_id", String(128), nullable=False),
    Column("state", String(32), nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("updated_at_utc", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "state IN ('pending','running','succeeded','partial','failed')",
        name="ck_upload_batches_state",
    ),
)


upload_batch_items_table = Table(
    "upload_batch_items",
    documents_metadata,
    Column("id", String(128), primary_key=True),
    Column("upload_batch_id", String(128), ForeignKey("upload_batches.id"), nullable=False),
    Column("document_id", String(128), ForeignKey("documents.id"), nullable=True),
    Column("submission_id", String(128), ForeignKey("knowledge_submissions.id"), nullable=True),
    Column("file_name", String(512), nullable=False),
    Column("content_hash_sha256", String(64), nullable=False),
    Column("result_state", String(32), nullable=False),
    Column("deduplicated", Boolean, nullable=False),
    Column("job_id", String(128), ForeignKey("ingestion_jobs.id"), nullable=True),
    Column("rejection_reason", String(256), nullable=True),
    *_timestamps(),
    CheckConstraint(
        "result_state IN ('pending','running','retry_wait','succeeded','failed','cancelled',"
        "'dead_letter','rejected','deduplicated')",
        name="ck_upload_batch_items_state",
    ),
    CheckConstraint(
        "NOT (document_id IS NOT NULL AND submission_id IS NOT NULL)",
        name="ck_upload_batch_items_one_target",
    ),
)
# 批次状态 API 按批次聚合 item，批次轮询不能随 items 表无界增长退化为全表扫描。
Index("ix_upload_batch_items_batch", upload_batch_items_table.c.upload_batch_id)


upload_dedup_claims_table = Table(
    "upload_dedup_claims",
    documents_metadata,
    Column("space_id", String(128), primary_key=True),
    Column("normalized_filename", String(512), primary_key=True),
    Column("content_hash_sha256", String(64), primary_key=True),
    Column("document_id", String(128), ForeignKey("documents.id"), nullable=False),
    Column(
        "document_version_id",
        String(128),
        ForeignKey("document_versions.id"),
        nullable=True,
    ),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
)


knowledge_submissions_table = Table(
    "knowledge_submissions",
    documents_metadata,
    Column("id", String(128), primary_key=True),
    Column("space_id", String(128), nullable=False),
    Column("submitter_user_id", String(64), nullable=False),
    Column("submitter_role_snapshot", String(32), nullable=True),
    Column("submitter_department_snapshot", String(64), nullable=True),
    Column("submitter_display_name_snapshot", String(256), nullable=True),
    Column("submitter_department_name_snapshot", String(256), nullable=True),
    Column("version", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("file_name", String(512), nullable=False),
    Column("media_kind", String(128), nullable=False),
    Column("content_hash_sha256", String(64), nullable=False),
    Column("private_object_key", String(1024), nullable=False),
    Column("object_manifest_json", JSON, nullable=False),
    Column("private_object_cleanup_requested_at_utc", DateTime(timezone=True), nullable=True),
    Column("private_object_cleaned_at_utc", DateTime(timezone=True), nullable=True),
    Column("reviewer_user_id", String(64), nullable=True),
    Column("reviewer_role_snapshot", String(32), nullable=True),
    Column("review_reason", String(256), nullable=True),
    Column("invalidated_reason", String(256), nullable=True),
    Column("invalidated_at", DateTime(timezone=True), nullable=True),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("reviewed_at_utc", DateTime(timezone=True), nullable=True),
    Column("updated_at_utc", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "status IN ('pending','approved','rejected','withdrawn','invalidated')",
        name="ck_knowledge_submissions_status",
    ),
    CheckConstraint("version >= 1", name="ck_knowledge_submissions_version_positive"),
)


submission_execution_grants_table = Table(
    "submission_execution_grants",
    documents_metadata,
    Column("id", String(128), primary_key=True),
    Column(
        "submission_id",
        String(128),
        ForeignKey("knowledge_submissions.id"),
        nullable=False,
        unique=True,
    ),
    Column("document_id", String(128), ForeignKey("documents.id"), nullable=False),
    Column("document_version_id", String(128), ForeignKey("document_versions.id"), nullable=False),
    Column("job_id", String(128), ForeignKey("ingestion_jobs.id"), nullable=False),
    Column("submitter_user_id_snapshot", String(64), nullable=False),
    Column("reviewer_user_id_snapshot", String(64), nullable=False),
    Column("reviewer_role_snapshot", String(32), nullable=False),
    Column("department_id_snapshot", String(64), nullable=True),
    Column("space_id_snapshot", String(128), nullable=False),
    Column("policy_version", String(64), nullable=False),
    Column("capability", String(128), nullable=False),
    Column("fingerprint", String(128), nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
)


ingestion_jobs_table = Table(
    "ingestion_jobs",
    documents_metadata,
    Column("id", String(128), primary_key=True),
    Column("document_id", String(128), ForeignKey("documents.id"), nullable=False),
    Column("document_version_id", String(128), ForeignKey("document_versions.id"), nullable=True),
    Column("operation", String(32), nullable=False),
    Column("state", String(32), nullable=False),
    Column("stage", String(32), nullable=True),
    Column("base_active_version_id", String(128), nullable=True),
    Column("upload_batch_id", String(128), ForeignKey("upload_batches.id"), nullable=True),
    Column("active_attempt_id", String(128), nullable=True),
    Column("active_publication_id", String(128), nullable=True),
    Column("version", Integer, nullable=False),
    Column("replay_generation", Integer, nullable=False),
    Column("next_attempt_at_utc", DateTime(timezone=True), nullable=True),
    Column("failure_reason", String(256), nullable=True),
    Column("cancelled_by_user_id", String(64), nullable=True),
    Column("cancelled_at_utc", DateTime(timezone=True), nullable=True),
    Column("replayed_by_user_id", String(64), nullable=True),
    Column("degradations_json", JSON, nullable=False),
    Column("processing_summary_json", JSON, nullable=False),
    # 人工重放事务固化的当时生效处理配置快照（§2.3 L129）；重放各 attempt
    # 的 staging request 共用该快照，不再逐 attempt 重新解析。
    Column("replay_config_snapshot_json", JSON, nullable=True),
    Column("usage_json", JSON, nullable=True),
    Column("ocr_low_confidence", Boolean, nullable=False),
    Column("notification_event_ids_json", JSON, nullable=False),
    Column("created_by_user_id", String(64), nullable=False),
    Column("quota_role_snapshot", String(32), nullable=False),
    Column("quota_department_id_snapshot", String(64), nullable=True),
    Column("quota_exempt_reason", String(64), nullable=True),
    Column("quota_charge_status", String(16), nullable=False, server_default="pending"),
    Column("quota_charge_reason", String(64), nullable=True),
    *_timestamps(),
    CheckConstraint(
        "operation IN ('initial','replace','reindex')",
        name="ck_ingestion_jobs_operation",
    ),
    CheckConstraint(
        "state IN ('pending','running','retry_wait','succeeded','failed','cancelled','dead_letter')",
        name="ck_ingestion_jobs_state",
    ),
    CheckConstraint(
        "stage IS NULL OR stage IN ('queued','parsing','indexing')",
        name="ck_ingestion_jobs_stage",
    ),
    CheckConstraint("version >= 1", name="ck_ingestion_jobs_version_positive"),
    CheckConstraint("replay_generation >= 0", name="ck_ingestion_jobs_replay_nonnegative"),
)
Index(
    "ix_ingestion_jobs_state_schedule",
    ingestion_jobs_table.c.state,
    ingestion_jobs_table.c.next_attempt_at_utc,
)
# 文档删除/软删路径按 document_id 过滤 ingestion_jobs 加行锁。
Index("ix_ingestion_jobs_document", ingestion_jobs_table.c.document_id)


ingestion_attempts_table = Table(
    "ingestion_attempts",
    documents_metadata,
    Column("id", String(128), primary_key=True),
    Column("job_id", String(128), ForeignKey("ingestion_jobs.id"), nullable=False),
    Column("attempt_number", Integer, nullable=False),
    Column("cycle_attempt_number", Integer, nullable=False),
    Column("replay_generation", Integer, nullable=False),
    Column("state", String(32), nullable=False),
    Column("lease_owner", String(128), nullable=True),
    Column("lease_expires_at_utc", DateTime(timezone=True), nullable=True),
    Column("fencing_token", BigInteger, nullable=False),
    Column("publication_id", String(128), ForeignKey("publications.id"), nullable=True),
    Column("staging_request_json", JSON, nullable=False),
    Column("processing_receipt_json", JSON, nullable=True),
    Column("failure_class", String(64), nullable=True),
    Column("failure_reason", String(256), nullable=True),
    *_timestamps(),
    CheckConstraint(
        "state IN ('running','succeeded','failed','expired','cancelled')",
        name="ck_ingestion_attempts_state",
    ),
    CheckConstraint("attempt_number >= 1", name="ck_ingestion_attempts_number_positive"),
    UniqueConstraint("job_id", "attempt_number", name="uq_ingestion_attempts_number"),
)


publications_table = Table(
    "publications",
    documents_metadata,
    Column("id", String(128), primary_key=True),
    Column("document_id", String(128), ForeignKey("documents.id"), nullable=False),
    Column("document_version_id", String(128), ForeignKey("document_versions.id"), nullable=False),
    Column("job_id", String(128), ForeignKey("ingestion_jobs.id"), nullable=False),
    Column("attempt_id", String(128), nullable=False),
    Column("generation_id", String(128), nullable=False),
    Column("status", String(32), nullable=False),
    Column("resource_manifest_json", JSON, nullable=False),
    Column("quota_charge_status", String(16), nullable=False, server_default="pending"),
    Column("quota_charge_reason", String(64), nullable=True),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("activated_at_utc", DateTime(timezone=True), nullable=True),
    Column("superseded_at_utc", DateTime(timezone=True), nullable=True),
    Column("discarded_at_utc", DateTime(timezone=True), nullable=True),
    CheckConstraint(
        "status IN ('staged','active','superseded','discarded')",
        name="ck_publications_status",
    ),
    UniqueConstraint(
        "document_version_id",
        "generation_id",
        "status",
        name="uq_publications_version_generation_status",
    ),
)


document_deletions_table = Table(
    "document_deletions",
    documents_metadata,
    Column("id", String(128), primary_key=True),
    Column("document_id", String(128), ForeignKey("documents.id"), nullable=False, unique=True),
    Column("requested_by_user_id", String(64), nullable=False),
    Column("version", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("requested_at_utc", DateTime(timezone=True), nullable=False),
    Column("completed_at_utc", DateTime(timezone=True), nullable=True),
    Column("notification_redaction_operation_id", String(128), nullable=False, unique=True),
    Column("notification_redaction_receipt_json", JSON, nullable=False),
    Column("physical_cleanup_json", JSON, nullable=False),
    CheckConstraint(
        "status IN ('pending_delete','cleaning','completed')",
        name="ck_document_deletions_status",
    ),
)


document_deletion_cleanup_targets_table = Table(
    "document_deletion_cleanup_targets",
    documents_metadata,
    Column("deletion_id", String(128), ForeignKey("document_deletions.id"), primary_key=True),
    Column("backend_kind", String(64), primary_key=True),
    Column("resource_id", String(1024), primary_key=True),
    Column("state", String(32), nullable=False),
    Column("attempt_count", Integer, nullable=False),
    Column("last_error", String(256), nullable=True),
    *_timestamps(),
    CheckConstraint(
        "state IN ('pending','failed','completed')",
        name="ck_document_deletion_cleanup_target_state",
    ),
    CheckConstraint("attempt_count >= 0", name="ck_document_deletion_cleanup_target_attempts"),
)


documents_instance_counters_table = Table(
    "documents_instance_counters",
    documents_metadata,
    Column("counter_name", String(64), primary_key=True),
    Column("value", BigInteger, nullable=False),
    CheckConstraint("value >= 0", name="ck_documents_instance_counters_value"),
)


index_revisions_table = Table(
    "index_revisions",
    documents_metadata,
    Column("id", String(128), primary_key=True),
    Column("document_id", String(128), ForeignKey("documents.id"), nullable=False),
    Column("revision", BigInteger, nullable=False),
    Column("generation_id", String(128), nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    UniqueConstraint("revision", name="uq_index_revisions_revision"),
)


index_changes_table = Table(
    "index_changes",
    documents_metadata,
    Column("id", String(128), primary_key=True),
    Column("document_id", String(128), ForeignKey("documents.id"), nullable=False),
    Column("document_version_id", String(128), ForeignKey("document_versions.id"), nullable=True),
    Column("publication_id", String(128), ForeignKey("publications.id"), nullable=True),
    Column("revision_id", String(128), ForeignKey("index_revisions.id"), nullable=False),
    Column("change_type", String(32), nullable=False),
    Column("space_id", String(128), nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "change_type IN ('publish','replace','reindex','delete')",
        name="ck_index_changes_type",
    ),
)


public_graph_source_manifests_table = Table(
    "public_graph_source_manifests",
    documents_metadata,
    Column("id", String(128), primary_key=True),
    Column("source_revision", BigInteger, nullable=False, unique=True),
    Column("source_manifest_hash", String(128), nullable=False, unique=True),
    Column("schema_version", Integer, nullable=False),
    Column("publications_json", JSON, nullable=False),
    Column("source_head_fence", BigInteger, nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    CheckConstraint("source_revision >= 1", name="ck_public_source_revision_positive"),
    CheckConstraint("schema_version = 1", name="ck_public_source_schema_version"),
)
Index("ix_public_graph_source_revision", public_graph_source_manifests_table.c.source_revision)


public_graph_source_changes_table = Table(
    "public_graph_source_changes",
    documents_metadata,
    Column("id", String(128), primary_key=True),
    Column("source_revision", BigInteger, nullable=False, unique=True),
    Column("space_id", String(128), nullable=False),
    Column("document_id", String(128), ForeignKey("documents.id"), nullable=False),
    Column("change_type", String(32), nullable=False),
    Column(
        "manifest_id", String(128), ForeignKey("public_graph_source_manifests.id"), nullable=False
    ),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "change_type IN ('publish','replace','reindex','restore','delete')",
        name="ck_public_source_changes_type",
    ),
)


public_graph_source_heads_table = Table(
    "public_graph_source_heads",
    documents_metadata,
    Column("id", String(32), primary_key=True),
    Column("source_revision", BigInteger, nullable=False),
    Column("source_manifest_id", String(128), nullable=True),
    Column("source_manifest_hash", String(128), nullable=True),
    Column("source_head_fence", BigInteger, nullable=False),
    Column("updated_at_utc", DateTime(timezone=True), nullable=False),
    CheckConstraint("source_revision >= 0", name="ck_public_source_head_revision_nonnegative"),
    CheckConstraint("source_head_fence >= 0", name="ck_public_source_head_fence_nonnegative"),
)


public_graph_source_consumers_table = Table(
    "public_graph_source_consumers",
    documents_metadata,
    Column("id", String(128), primary_key=True),
    Column("consumer_kind", String(32), nullable=False),
    Column("consumer_id", String(128), nullable=False),
    Column("source_revision", BigInteger, nullable=False),
    Column("source_manifest_hash", String(128), nullable=False),
    Column("source_head_fence", BigInteger, nullable=True),
    Column("purpose", String(32), nullable=False),
    Column("operation_id", String(128), nullable=False, unique=True),
    Column("state", String(32), nullable=False),
    Column("acknowledged_at_utc", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "consumer_kind IN ('indexing','public_graph')",
        name="ck_public_source_consumer_kind",
    ),
    CheckConstraint(
        "purpose IN ('stage','release','rollback','discard')",
        name="ck_public_source_consumer_purpose",
    ),
    CheckConstraint(
        "state IN ('held','discarded')",
        name="ck_public_source_consumer_state",
    ),
    UniqueConstraint(
        "consumer_kind",
        "consumer_id",
        "source_revision",
        "purpose",
        name="uq_public_source_consumer_ack",
    ),
)


documents_idempotency_table = Table(
    "documents_idempotency",
    documents_metadata,
    Column("actor_id", String(64), primary_key=True),
    Column("endpoint", String(256), primary_key=True),
    Column("target_id", String(128), primary_key=True),
    Column("idempotency_key_hash", String(64), primary_key=True),
    Column("request_fingerprint", String(128), nullable=False),
    Column("status", String(32), nullable=False),
    Column("response_json", JSON, nullable=True),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("completed_at_utc", DateTime(timezone=True), nullable=True),
    CheckConstraint(
        "status IN ('reserved','completed')",
        name="ck_documents_idempotency_status",
    ),
)


DOCUMENTS_TABLE_NAMES = frozenset(documents_metadata.tables)

__all__ = ["DOCUMENTS_TABLE_NAMES", "documents_metadata"]
