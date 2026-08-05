"""Create the documented RAGqs business schema.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-08-04

This is a handwritten baseline. It follows the backend and frontend
contracts directly and intentionally does not import the current application
storage implementation.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ID = sa.String(128)
TS = sa.DateTime(timezone=True)
JSON = postgresql.JSONB


def _id(name: str, *, primary_key: bool = False, nullable: bool = False) -> sa.Column:
    return sa.Column(name, ID, primary_key=primary_key, nullable=nullable)


def _ts(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, TS, nullable=nullable)


def _json(name: str, *, nullable: bool = True) -> sa.Column:
    return sa.Column(name, JSON, nullable=nullable)


def upgrade() -> None:
    # Identity, access, and knowledge-space roots.
    op.create_table(
        "department",
        _id("department_id", primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("normalized_name", sa.String(200), nullable=False, unique=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        _ts("created_at"),
        _ts("deactivated_at", nullable=True),
        sa.CheckConstraint("status IN ('active', 'inactive')", name="ck_department_status"),
    )

    op.create_table(
        "user",
        _id("user_id", primary_key=True),
        sa.Column("username", sa.String(160), nullable=False, unique=True),
        sa.Column("real_name", sa.String(200), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("password_hash", sa.String(512), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("lifecycle_status", sa.String(32), nullable=False),
        _id("department_id", nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        _ts("created_at"),
        _ts("updated_at"),
        _ts("deletion_requested_at", nullable=True),
        _ts("purge_after_at", nullable=True),
        _ts("deleted_at", nullable=True),
        sa.ForeignKeyConstraint(["department_id"], ["department.department_id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "role IN ('user', 'minister', 'ops', 'admin')", name="ck_user_role"
        ),
        sa.CheckConstraint(
            "lifecycle_status IN ('active', 'pending_delete', 'deleted')",
            name="ck_user_lifecycle_status",
        ),
    )
    op.create_index("ix_user_department_status", "user", ["department_id", "lifecycle_status"])

    op.create_table(
        "user_preference",
        _id("user_id", primary_key=True),
        sa.Column("ab_opt_out", sa.Boolean(), nullable=False, server_default=sa.false()),
        _json("preferences"),
        _ts("created_at"),
        _ts("updated_at"),
        sa.ForeignKeyConstraint(["user_id"], ["user.user_id"], ondelete="CASCADE"),
    )

    op.create_table(
        "auth_session",
        _id("auth_session_id", primary_key=True),
        _id("user_id", nullable=False),
        sa.Column("device_label", sa.String(200), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        _ts("issued_at"),
        _ts("last_active_at"),
        _ts("expires_at"),
        _ts("revoked_at", nullable=True),
        sa.Column("revoked_reason", sa.String(128), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        _ts("created_at"),
        sa.ForeignKeyConstraint(["user_id"], ["user.user_id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "status IN ('active', 'revoked', 'expired')", name="ck_auth_session_status"
        ),
    )
    op.create_index("ix_auth_session_user_status", "auth_session", ["user_id", "status"])

    op.create_table(
        "auth_refresh_token",
        _id("auth_refresh_token_id", primary_key=True),
        _id("auth_session_id", nullable=False),
        _id("family_id", nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("token_hash", sa.String(512), nullable=False, unique=True),
        sa.Column("status", sa.String(32), nullable=False),
        _ts("issued_at"),
        _ts("consumed_at", nullable=True),
        _ts("family_expires_at"),
        _ts("invalidated_at", nullable=True),
        sa.Column("invalidated_reason", sa.String(128), nullable=True),
        sa.ForeignKeyConstraint(["auth_session_id"], ["auth_session.auth_session_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("auth_session_id", "sequence", name="uq_auth_refresh_session_sequence"),
        sa.CheckConstraint(
            "status IN ('active', 'consumed', 'invalidated')", name="ck_auth_refresh_status"
        ),
    )

    op.create_table(
        "knowledge_space",
        _id("space_id", primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        _id("owner_user_id", nullable=True),
        _id("department_id", nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        _ts("created_at"),
        _ts("deactivated_at", nullable=True),
        sa.ForeignKeyConstraint(["owner_user_id"], ["user.user_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["department_id"], ["department.department_id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "kind IN ('personal', 'department', 'public')", name="ck_knowledge_space_kind"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')", name="ck_knowledge_space_status"
        ),
    )
    op.create_index("ix_knowledge_space_kind_status", "knowledge_space", ["kind", "status"])

    # Conversation and answer read model. answer_mode is persisted here and
    # in generation_candidate/final_response so every response shape has one
    # canonical source.
    op.create_table(
        "conversation_group",
        _id("group_id", primary_key=True),
        _id("owner_user_id", nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        _ts("created_at"),
        _ts("updated_at"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["user.user_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("owner_user_id", "name", name="uq_conversation_group_owner_name"),
    )

    op.create_table(
        "conversation",
        _id("conversation_id", primary_key=True),
        _id("owner_user_id", nullable=False),
        _id("group_id", nullable=True),
        sa.Column("title", sa.String(500), nullable=True),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        _ts("created_at"),
        _ts("updated_at"),
        _ts("deleted_at", nullable=True),
        sa.ForeignKeyConstraint(["owner_user_id"], ["user.user_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["group_id"], ["conversation_group.group_id"], ondelete="SET NULL"),
    )
    op.create_index("ix_conversation_owner_updated", "conversation", ["owner_user_id", "updated_at"])

    op.create_table(
        "message",
        _id("message_id", primary_key=True),
        _id("conversation_id", nullable=False),
        _id("user_message_id", nullable=True),
        _id("generation_id", nullable=True),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("answer_mode", sa.String(32), nullable=True),
        sa.Column(
            "used_tools_without_knowledge_base",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("effort_level", sa.String(32), nullable=True),
        _json("final_response"),
        _json("citations"),
        _json("retrieval"),
        _json("notices"),
        _id("root_generation_id", nullable=True),
        _id("retry_of_generation_id", nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        _ts("terminal_at", nullable=True),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversation.conversation_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_message_id"], ["message.message_id"], ondelete="SET NULL"),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="ck_message_role"),
        sa.CheckConstraint(
            "status IN ('generating', 'completed', 'failed', 'stopped')",
            name="ck_message_status",
        ),
        sa.CheckConstraint(
            "answer_mode IS NULL OR answer_mode IN ('grounded', 'direct', 'no_context')",
            name="ck_message_answer_mode",
        ),
    )
    op.create_index("ix_message_conversation_created", "message", ["conversation_id", "created_at"])
    op.create_index("ix_message_generation", "message", ["generation_id"])

    op.create_table(
        "message_feedback",
        _id("feedback_id", primary_key=True),
        _id("message_id", nullable=False),
        _id("user_id", nullable=False),
        sa.Column("vote", sa.String(16), nullable=False),
        sa.Column("down_reason", sa.String(64), nullable=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("request_fingerprint", sa.String(128), nullable=False),
        _ts("created_at"),
        sa.ForeignKeyConstraint(["message_id"], ["message.message_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["user.user_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("message_id", name="uq_message_feedback_message"),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_message_feedback_idempotency"),
        sa.CheckConstraint("vote IN ('up', 'down')", name="ck_message_feedback_vote"),
        sa.CheckConstraint(
            "down_reason IS NULL OR down_reason IN ('no_grounding', 'wrong_citation')",
            name="ck_message_feedback_reason",
        ),
    )

    # Documents, uploads, and durable ingestion execution.
    op.create_table(
        "document",
        _id("document_id", primary_key=True),
        _id("space_id", nullable=False),
        _id("active_version_id", nullable=True),
        _id("pending_version_id", nullable=True),
        _id("active_operation_job_id", nullable=True),
        _id("deletion_id", nullable=True),
        sa.Column("lifecycle_status", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        _ts("created_at"),
        _ts("updated_at"),
        _ts("deletion_requested_at", nullable=True),
        _ts("deleted_at", nullable=True),
        sa.ForeignKeyConstraint(["space_id"], ["knowledge_space.space_id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "lifecycle_status IN ('active', 'pending_delete', 'deleted')",
            name="ck_document_lifecycle_status",
        ),
    )
    op.create_index("ix_document_space_lifecycle", "document", ["space_id", "lifecycle_status"])

    op.create_table(
        "document_version",
        _id("document_version_id", primary_key=True),
        _id("document_id", nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("normalized_filename", sa.String(512), nullable=False),
        sa.Column("content_hash", sa.String(128), nullable=False),
        sa.Column("media_kind", sa.String(64), nullable=False),
        sa.Column("content_type", sa.String(200), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("source_object_id", sa.String(1024), nullable=True),
        _id("restored_from_version_id", nullable=True),
        _ts("created_at"),
        _ts("activated_at", nullable=True),
        _ts("terminal_at", nullable=True),
        _ts("superseded_at", nullable=True),
        _ts("purge_after_at", nullable=True),
        _ts("purged_at", nullable=True),
        sa.Column("content_available", sa.Boolean(), nullable=False, server_default=sa.true()),
        _json("metadata"),
        sa.ForeignKeyConstraint(["document_id"], ["document.document_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["restored_from_version_id"],
            ["document_version.document_version_id"],
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("document_id", "version_number", name="uq_document_version_number"),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'superseded', 'failed', 'cancelled', 'purging', 'purged')",
            name="ck_document_version_status",
        ),
    )
    op.create_index("ix_document_version_document_status", "document_version", ["document_id", "status"])

    op.create_table(
        "upload_dedup_claim",
        _id("claim_id", primary_key=True),
        _id("space_id", nullable=False),
        sa.Column("normalized_filename", sa.String(512), nullable=False),
        sa.Column("content_hash", sa.String(128), nullable=False),
        _id("document_id", nullable=False),
        _id("document_version_id", nullable=True),
        sa.Column("state", sa.String(32), nullable=False),
        _ts("created_at"),
        _ts("released_at", nullable=True),
        sa.ForeignKeyConstraint(["space_id"], ["knowledge_space.space_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["document_id"], ["document.document_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_version.document_version_id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "space_id", "normalized_filename", "content_hash", name="uq_upload_dedup_key"
        ),
        sa.CheckConstraint("state IN ('pending', 'active', 'released')", name="ck_upload_claim_state"),
    )

    op.create_table(
        "upload_batch",
        _id("upload_batch_id", primary_key=True),
        _id("owner_user_id", nullable=False),
        _id("space_id", nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("succeeded_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("deduplicated_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        _ts("created_at"),
        _ts("completed_at", nullable=True),
        sa.ForeignKeyConstraint(["owner_user_id"], ["user.user_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["space_id"], ["knowledge_space.space_id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "state IN ('pending', 'running', 'succeeded', 'partial', 'failed')",
            name="ck_upload_batch_state",
        ),
    )

    op.create_table(
        "upload_batch_item",
        _id("item_id", primary_key=True),
        _id("upload_batch_id", nullable=False),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("content_hash", sa.String(128), nullable=True),
        _id("document_id", nullable=True),
        _id("job_id", nullable=True),
        sa.Column("result_state", sa.String(32), nullable=False),
        sa.Column("error_code", sa.String(128), nullable=True),
        _ts("created_at"),
        _ts("completed_at", nullable=True),
        sa.ForeignKeyConstraint(["upload_batch_id"], ["upload_batch.upload_batch_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["document.document_id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "result_state IN ('pending', 'running', 'succeeded', 'deduplicated', 'failed', 'cancelled')",
            name="ck_upload_batch_item_state",
        ),
    )

    op.create_table(
        "knowledge_submission",
        _id("submission_id", primary_key=True),
        _id("submitter_user_id", nullable=False),
        _id("target_space_id", nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("submitter_role_snapshot", sa.String(32), nullable=False),
        _id("submitter_department_id_snapshot", nullable=True),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("media_kind", sa.String(64), nullable=False),
        sa.Column("content_type", sa.String(200), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_hash", sa.String(128), nullable=False),
        sa.Column("source_object_id", sa.String(1024), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reject_reason", sa.String(128), nullable=True),
        sa.Column("invalidated_reason", sa.String(128), nullable=True),
        _id("approved_by", nullable=True),
        _id("document_id", nullable=True),
        _id("document_version_id", nullable=True),
        _id("job_id", nullable=True),
        _id("submission_execution_grant_id", nullable=True),
        _ts("created_at"),
        _ts("reviewed_at", nullable=True),
        _ts("invalidated_at", nullable=True),
        _ts("deleted_at", nullable=True),
        sa.ForeignKeyConstraint(["submitter_user_id"], ["user.user_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["target_space_id"], ["knowledge_space.space_id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'withdrawn', 'invalidated')",
            name="ck_knowledge_submission_status",
        ),
    )
    op.create_index(
        "ix_knowledge_submission_space_status",
        "knowledge_submission",
        ["target_space_id", "status"],
    )

    op.create_table(
        "submission_execution_grant",
        _id("submission_execution_grant_id", primary_key=True),
        _id("submission_id", nullable=False),
        _id("submitter_user_id", nullable=False),
        _id("approved_by", nullable=False),
        sa.Column("approved_by_role_snapshot", sa.String(32), nullable=False),
        _id("approved_by_department_id_snapshot", nullable=True),
        _id("target_space_id", nullable=False),
        sa.Column("capability", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.String(128), nullable=False),
        sa.Column("grant_fingerprint", sa.String(128), nullable=False, unique=True),
        _ts("granted_at"),
        sa.ForeignKeyConstraint(["submission_id"], ["knowledge_submission.submission_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["submitter_user_id"], ["user.user_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["approved_by"], ["user.user_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["target_space_id"], ["knowledge_space.space_id"], ondelete="RESTRICT"),
        sa.CheckConstraint("capability = 'shared_submission_ingest'", name="ck_submission_grant_capability"),
    )

    op.create_table(
        "ingestion_job",
        _id("job_id", primary_key=True),
        _id("actor_user_id", nullable=True),
        _id("execution_actor_user_id", nullable=True),
        _id("space_id", nullable=False),
        _id("document_id", nullable=True),
        _id("document_version_id", nullable=True),
        _id("upload_batch_id", nullable=True),
        _id("submission_id", nullable=True),
        _id("submission_execution_grant_id", nullable=True),
        _id("active_attempt_id", nullable=True),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("stage", sa.String(32), nullable=True),
        sa.Column("failure_reason", sa.String(128), nullable=True),
        sa.Column("quota_exempt_reason", sa.String(128), nullable=True),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("replay_generation", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("idempotency_key", sa.String(256), nullable=True),
        sa.Column("request_fingerprint", sa.String(128), nullable=True),
        _id("expected_generation_id", nullable=True),
        _ts("next_attempt_at", nullable=True),
        _ts("created_at"),
        _ts("started_at", nullable=True),
        _ts("completed_at", nullable=True),
        _ts("terminal_at", nullable=True),
        _ts("updated_at"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["user.user_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["execution_actor_user_id"], ["user.user_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["space_id"], ["knowledge_space.space_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["document_id"], ["document.document_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["submission_execution_grant_id"],
            ["submission_execution_grant.submission_execution_grant_id"],
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "operation IN ('initial', 'replace', 'reindex')", name="ck_ingestion_job_operation"
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'running', 'retry_wait', 'succeeded', 'failed', 'cancelled', 'dead_letter')",
            name="ck_ingestion_job_state",
        ),
    )
    op.create_index("ix_ingestion_job_state_next_attempt", "ingestion_job", ["state", "next_attempt_at"])
    op.create_index("ix_ingestion_job_document", "ingestion_job", ["document_id", "created_at"])

    op.create_table(
        "ingestion_attempt",
        _id("attempt_id", primary_key=True),
        _id("job_id", nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("cycle_attempt_number", sa.Integer(), nullable=False),
        sa.Column("replay_generation", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("lease_owner", sa.String(256), nullable=True),
        _ts("lease_expires_at", nullable=True),
        _ts("heartbeat_at", nullable=True),
        _ts("next_attempt_at", nullable=True),
        sa.Column("failure_reason", sa.String(128), nullable=True),
        _id("execution_actor_user_id", nullable=True),
        _json("checkpoint"),
        _ts("started_at", nullable=True),
        _ts("completed_at", nullable=True),
        _ts("terminal_at", nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.ForeignKeyConstraint(["job_id"], ["ingestion_job.job_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["execution_actor_user_id"], ["user.user_id"], ondelete="SET NULL"),
        sa.UniqueConstraint("job_id", "attempt_number", name="uq_ingestion_attempt_number"),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'expired', 'cancelled')",
            name="ck_ingestion_attempt_status",
        ),
    )

    op.create_table(
        "publication",
        _id("publication_id", primary_key=True),
        _id("document_id", nullable=False),
        _id("document_version_id", nullable=False),
        _id("job_id", nullable=False),
        _id("attempt_id", nullable=False),
        _id("space_id", nullable=False),
        _id("expected_generation_id", nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("backend_kind", sa.String(64), nullable=True),
        sa.Column("quota_charge_status", sa.String(32), nullable=True),
        sa.Column("quota_exempt_reason", sa.String(128), nullable=True),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        _ts("created_at"),
        _ts("published_at", nullable=True),
        _ts("discarded_at", nullable=True),
        sa.ForeignKeyConstraint(["document_id"], ["document.document_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_version.document_version_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["job_id"], ["ingestion_job.job_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["attempt_id"], ["ingestion_attempt.attempt_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["space_id"], ["knowledge_space.space_id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "status IN ('staged', 'active', 'discarded')", name="ck_publication_status"
        ),
    )
    op.create_index("ix_publication_document_version_status", "publication", ["document_version_id", "status"])

    op.create_table(
        "chunk",
        _id("chunk_id", primary_key=True),
        _id("publication_id", nullable=False),
        _id("document_id", nullable=False),
        _id("document_version_id", nullable=False),
        _id("space_id", nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("snippet", sa.Text(), nullable=True),
        _json("locator", nullable=False),
        _json("metadata"),
        _ts("created_at"),
        sa.ForeignKeyConstraint(["publication_id"], ["publication.publication_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["document_id"], ["document.document_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_version.document_version_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["space_id"], ["knowledge_space.space_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("publication_id", "chunk_index", name="uq_chunk_publication_index"),
    )
    op.create_index("ix_chunk_document_version", "chunk", ["document_id", "document_version_id"])

    # Calibration windows, durable generation execution, and A/B facts.
    op.create_table(
        "calibration_window",
        _id("window_id", primary_key=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("window_kind", sa.String(32), nullable=False),
        sa.Column("policy_version", sa.String(128), nullable=False),
        sa.Column("sample_rate", sa.Numeric(8, 7), nullable=False),
        sa.Column("pairs_collected", sa.Integer(), nullable=False, server_default=sa.text("0")),
        _id("opened_by", nullable=False),
        _ts("opened_at"),
        _id("closed_by", nullable=True),
        _ts("closed_at", nullable=True),
        _ts("close_deadline_at", nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        _ts("created_at"),
        _ts("updated_at"),
        sa.ForeignKeyConstraint(["opened_by"], ["user.user_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["closed_by"], ["user.user_id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "status IN ('open', 'closing', 'closed')", name="ck_calibration_window_status"
        ),
        sa.CheckConstraint(
            "window_kind IN ('cold_start', 'sentinel', 'manual')",
            name="ck_calibration_window_kind",
        ),
        sa.CheckConstraint("sample_rate >= 0 AND sample_rate <= 1", name="ck_calibration_window_rate"),
        sa.CheckConstraint("pairs_collected >= 0", name="ck_calibration_window_pairs"),
    )
    op.create_index(
        "uq_calibration_window_open",
        "calibration_window",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )

    op.create_table(
        "calibration_window_command",
        _id("command_id", primary_key=True),
        _id("operator_user_id", nullable=False),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("request_hash", sa.String(128), nullable=False),
        _id("target_window_id", nullable=True),
        _json("response_snapshot", nullable=False),
        _ts("created_at"),
        _ts("completed_at", nullable=True),
        sa.ForeignKeyConstraint(["operator_user_id"], ["user.user_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["target_window_id"], ["calibration_window.window_id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "operator_user_id", "idempotency_key", name="uq_calibration_window_command_key"
        ),
        sa.CheckConstraint("action IN ('open', 'close')", name="ck_calibration_window_command_action"),
    )

    op.create_table(
        "generation",
        _id("generation_id", primary_key=True),
        _id("conversation_id", nullable=False),
        _id("user_message_id", nullable=False),
        _id("assistant_message_id", nullable=False),
        _id("owner_user_id", nullable=False),
        _id("root_generation_id", nullable=True),
        _id("retry_of_generation_id", nullable=True),
        _id("window_id", nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("effort_level", sa.String(32), nullable=False),
        sa.Column("rag_budget_policy_version", sa.String(128), nullable=False),
        sa.Column("window_policy_version", sa.String(128), nullable=True),
        sa.Column("window_kind", sa.String(32), nullable=True),
        sa.Column("window_sample_rate", sa.Numeric(8, 7), nullable=True),
        sa.Column("scope", JSON, nullable=False),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("request_fingerprint", sa.String(128), nullable=False),
        _ts("deadline_at"),
        _ts("stop_requested_at", nullable=True),
        _ts("terminal_at", nullable=True),
        sa.Column("error_code", sa.String(128), nullable=True),
        _json("final_response"),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        _ts("created_at"),
        _ts("updated_at"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversation.conversation_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_message_id"], ["message.message_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["assistant_message_id"], ["message.message_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["user.user_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["window_id"], ["calibration_window.window_id"], ondelete="SET NULL"),
        sa.UniqueConstraint("owner_user_id", "idempotency_key", name="uq_generation_idempotency"),
        sa.CheckConstraint(
            "status IN ('running', 'stop_requested', 'completed', 'failed', 'stopped')",
            name="ck_generation_status",
        ),
        sa.CheckConstraint(
            "effort_level IN ('quick', 'think', 'deep')", name="ck_generation_effort_level"
        ),
        sa.CheckConstraint(
            "window_sample_rate IS NULL OR (window_sample_rate >= 0 AND window_sample_rate <= 1)",
            name="ck_generation_window_rate",
        ),
    )
    op.create_index("ix_generation_conversation_created", "generation", ["conversation_id", "created_at"])
    op.create_index("ix_generation_status_deadline", "generation", ["status", "deadline_at"])

    op.create_table(
        "generation_execution",
        _id("execution_id", primary_key=True),
        _id("generation_id", nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("execution_attempt_number", sa.Integer(), nullable=False),
        sa.Column("cycle_attempt_number", sa.Integer(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("lease_owner", sa.String(256), nullable=True),
        _ts("lease_expires_at", nullable=True),
        _ts("heartbeat_at", nullable=True),
        _ts("next_attempt_at", nullable=True),
        sa.Column("failure_reason", sa.String(128), nullable=True),
        _json("checkpoint"),
        _ts("started_at", nullable=True),
        _ts("completed_at", nullable=True),
        _ts("terminal_at", nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.ForeignKeyConstraint(["generation_id"], ["generation.generation_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("generation_id", "execution_attempt_number", name="uq_generation_execution_attempt"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'retry_wait', 'provider_reconciling', 'expired', 'completed', 'failed', 'cancelled')",
            name="ck_generation_execution_status",
        ),
    )
    op.create_index(
        "ix_generation_execution_claim",
        "generation_execution",
        ["status", "next_attempt_at", "lease_expires_at"],
    )

    op.create_table(
        "generation_event",
        _id("event_id", primary_key=True),
        _id("generation_id", nullable=False),
        sa.Column("event_seq", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        _json("payload", nullable=False),
        _ts("occurred_at"),
        _ts("created_at"),
        sa.ForeignKeyConstraint(["generation_id"], ["generation.generation_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("generation_id", "event_seq", name="uq_generation_event_sequence"),
        sa.CheckConstraint(
            "event_type IN ('start', 'stage', 'step', 'notice', 'ab_start', 'answer', 'done', 'error', 'stopped')",
            name="ck_generation_event_type",
        ),
    )
    op.create_index("ix_generation_event_replay", "generation_event", ["generation_id", "event_seq"])

    op.create_table(
        "generation_candidate",
        _id("candidate_id", primary_key=True),
        _id("generation_id", nullable=False),
        sa.Column("candidate", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        _json("citations", nullable=False),
        sa.Column("answer_mode", sa.String(32), nullable=False),
        sa.Column("effort_level", sa.String(32), nullable=False),
        sa.Column("upgraded_from", sa.String(32), nullable=True),
        _ts("selected_at", nullable=True),
        _ts("created_at"),
        sa.ForeignKeyConstraint(["generation_id"], ["generation.generation_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("generation_id", "candidate", name="uq_generation_candidate_number"),
        sa.CheckConstraint("candidate >= 0", name="ck_generation_candidate_nonnegative"),
        sa.CheckConstraint(
            "status IN ('planned', 'published', 'discarded')", name="ck_generation_candidate_status"
        ),
        sa.CheckConstraint(
            "answer_mode IN ('grounded', 'direct', 'no_context')",
            name="ck_generation_candidate_answer_mode",
        ),
    )

    op.create_table(
        "ab_pair",
        _id("ab_pair_id", primary_key=True),
        _id("message_id", nullable=False),
        _id("generation_id", nullable=False),
        _id("window_id", nullable=False),
        _id("owner_user_id", nullable=False),
        _id("space_id", nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("voted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("choice", sa.String(16), nullable=True),
        _id("voter_user_id", nullable=True),
        _ts("voted_at", nullable=True),
        _ts("expires_at"),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        _ts("created_at"),
        _ts("updated_at"),
        sa.ForeignKeyConstraint(["message_id"], ["message.message_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["generation_id"], ["generation.generation_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["window_id"], ["calibration_window.window_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["user.user_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["voter_user_id"], ["user.user_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["space_id"], ["knowledge_space.space_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("message_id", name="uq_ab_pair_message"),
        sa.UniqueConstraint("generation_id", name="uq_ab_pair_generation"),
        sa.CheckConstraint(
            "status IN ('pending', 'open', 'voted', 'expired')", name="ck_ab_pair_status"
        ),
        sa.CheckConstraint(
            "choice IS NULL OR choice IN ('0', '1', 'neither')", name="ck_ab_pair_choice"
        ),
        sa.CheckConstraint("(status = 'voted') = voted", name="ck_ab_pair_voted_status"),
    )
    op.create_index("ix_ab_pair_window_status_expiry", "ab_pair", ["window_id", "status", "expires_at"])

    op.create_table(
        "ab_pair_candidate",
        _id("ab_pair_id", nullable=False),
        sa.Column("candidate", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        _json("citations"),
        sa.Column("answer_mode", sa.String(32), nullable=True),
        sa.Column("effort_level", sa.String(32), nullable=True),
        _ts("created_at"),
        sa.ForeignKeyConstraint(["ab_pair_id"], ["ab_pair.ab_pair_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("ab_pair_id", "candidate"),
        sa.CheckConstraint("candidate IN (0, 1)", name="ck_ab_pair_candidate_number"),
        sa.CheckConstraint(
            "status IN ('planned', 'published', 'discarded')", name="ck_ab_pair_candidate_status"
        ),
        sa.CheckConstraint(
            "answer_mode IS NULL OR answer_mode IN ('grounded', 'direct', 'no_context')",
            name="ck_ab_pair_candidate_answer_mode",
        ),
    )

    op.create_table(
        "ab_vote",
        _id("ab_vote_id", primary_key=True),
        _id("ab_pair_id", nullable=False),
        _id("voter_user_id", nullable=False),
        sa.Column("operation_kind", sa.String(32), nullable=False, server_default=sa.text("'ab_vote'")),
        sa.Column("choice", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("request_fingerprint", sa.String(128), nullable=False),
        _ts("voted_at"),
        _ts("created_at"),
        sa.ForeignKeyConstraint(["ab_pair_id"], ["ab_pair.ab_pair_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["voter_user_id"], ["user.user_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("ab_pair_id", name="uq_ab_vote_pair"),
        sa.UniqueConstraint(
            "voter_user_id", "operation_kind", "idempotency_key", name="uq_ab_vote_idempotency"
        ),
        sa.CheckConstraint("operation_kind = 'ab_vote'", name="ck_ab_vote_operation_kind"),
        sa.CheckConstraint("choice IN ('0', '1', 'neither')", name="ck_ab_vote_choice"),
    )

    # Calendar, quota, and immutable resource-accounting facts.
    op.create_table(
        "business_calendar_version",
        _id("business_calendar_version_id", primary_key=True),
        sa.Column("business_timezone", sa.String(128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        _ts("effective_from"),
        _ts("retired_at", nullable=True),
        _ts("created_at"),
        sa.UniqueConstraint("business_timezone", "version", name="uq_business_calendar_version"),
    )

    op.create_table(
        "quota_projection",
        _id("quota_projection_id", primary_key=True),
        _id("quota_subject_user_id", nullable=False),
        sa.Column("quota_period", sa.String(7), nullable=False),
        _id("business_calendar_version_id", nullable=False),
        sa.Column("business_timezone", sa.String(128), nullable=False),
        sa.Column("base_limit", sa.BigInteger(), nullable=False),
        sa.Column("extra_granted", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("used", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("effective_limit", sa.BigInteger(), nullable=False),
        _ts("reset_at"),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        _ts("created_at"),
        _ts("updated_at"),
        sa.ForeignKeyConstraint(["quota_subject_user_id"], ["user.user_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["business_calendar_version_id"],
            ["business_calendar_version.business_calendar_version_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("quota_subject_user_id", "quota_period", name="uq_quota_projection_period"),
        sa.CheckConstraint("quota_period ~ '^[0-9]{4}-[0-9]{2}$'", name="ck_quota_projection_period"),
        sa.CheckConstraint("base_limit >= 0 AND extra_granted >= 0 AND used >= 0", name="ck_quota_projection_nonnegative"),
    )

    op.create_table(
        "quota_request",
        _id("quota_request_id", primary_key=True),
        _id("applicant_user_id", nullable=False),
        sa.Column("applicant_role_snapshot", sa.String(32), nullable=False),
        _id("applicant_department_id_snapshot", nullable=True),
        sa.Column("quota_period", sa.String(7), nullable=False),
        _id("business_calendar_version_id", nullable=False),
        sa.Column("requested_pages", sa.Integer(), nullable=False),
        sa.Column("approved_pages", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        _id("reviewer_user_id", nullable=True),
        sa.Column("reviewer_role_snapshot", sa.String(32), nullable=True),
        _id("reviewer_department_id_snapshot", nullable=True),
        _id("credit_entry_id", nullable=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("request_fingerprint", sa.String(128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("cancellation_reason", sa.String(128), nullable=True),
        _ts("created_at"),
        _ts("reviewed_at", nullable=True),
        _ts("updated_at"),
        sa.ForeignKeyConstraint(["applicant_user_id"], ["user.user_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["business_calendar_version_id"],
            ["business_calendar_version.business_calendar_version_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["reviewer_user_id"], ["user.user_id"], ondelete="SET NULL"),
        sa.UniqueConstraint("applicant_user_id", "idempotency_key", name="uq_quota_request_idempotency"),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'cancelled')", name="ck_quota_request_status"
        ),
        sa.CheckConstraint("requested_pages BETWEEN 1 AND 500", name="ck_quota_request_pages"),
        sa.CheckConstraint(
            "approved_pages IS NULL OR approved_pages BETWEEN 1 AND requested_pages",
            name="ck_quota_request_approved_pages",
        ),
    )
    op.create_index(
        "uq_quota_request_pending_period",
        "quota_request",
        ["applicant_user_id", "quota_period"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "quota_debit",
        _id("quota_debit_id", primary_key=True),
        sa.Column("entry_kind", sa.String(32), nullable=False),
        _id("quota_operation_id", nullable=True),
        _id("quota_subject_user_id", nullable=True),
        sa.Column("quota_exempt_reason", sa.String(128), nullable=True),
        sa.Column("page_delta", sa.BigInteger(), nullable=False),
        _id("original_debit_id", nullable=True),
        _id("quota_request_id", nullable=True),
        _id("publication_id", nullable=True),
        sa.Column("adjustment_source_namespace", sa.String(128), nullable=True),
        sa.Column("adjustment_source_id", sa.String(256), nullable=True),
        sa.Column("adjustment_allocation_key", sa.String(256), nullable=True),
        _id("effective_calendar_version_id", nullable=False),
        _ts("effective_at"),
        sa.Column("effective_period", sa.String(7), nullable=False),
        _id("recorded_calendar_version_id", nullable=False),
        _ts("recorded_at"),
        sa.Column("recorded_period", sa.String(7), nullable=False),
        sa.Column("event_fingerprint", sa.String(128), nullable=False),
        _ts("created_at"),
        sa.ForeignKeyConstraint(
            ["quota_subject_user_id"], ["user.user_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["original_debit_id"], ["quota_debit.quota_debit_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["quota_request_id"], ["quota_request.quota_request_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["publication_id"], ["publication.publication_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["effective_calendar_version_id"],
            ["business_calendar_version.business_calendar_version_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["recorded_calendar_version_id"],
            ["business_calendar_version.business_calendar_version_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("quota_operation_id", name="uq_quota_debit_operation"),
        sa.UniqueConstraint(
            "entry_kind", "adjustment_source_namespace", "adjustment_source_id",
            name="uq_quota_debit_adjustment_source",
        ),
        sa.CheckConstraint(
            "entry_kind IN ('debit', 'reversal', 'supplement', 'credit')",
            name="ck_quota_debit_entry_kind",
        ),
        sa.CheckConstraint(
            "effective_period ~ '^[0-9]{4}-[0-9]{2}$' AND recorded_period ~ '^[0-9]{4}-[0-9]{2}$'",
            name="ck_quota_debit_periods",
        ),
    )
    op.create_index("ix_quota_debit_subject_effective", "quota_debit", ["quota_subject_user_id", "effective_period"])

    op.create_table(
        "provider_call",
        _id("provider_call_id", primary_key=True),
        sa.Column("execution_kind", sa.String(64), nullable=False),
        _id("execution_id", nullable=True),
        _id("attempt_id", nullable=True),
        _id("generation_id", nullable=True),
        sa.Column("provider", sa.String(128), nullable=False),
        sa.Column("model", sa.String(256), nullable=False),
        sa.Column("operation", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(128), nullable=False),
        sa.Column("provider_request_id", sa.String(512), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        _ts("prepared_at"),
        _ts("dispatching_at", nullable=True),
        _ts("completed_at", nullable=True),
        _ts("not_sent_at", nullable=True),
        _ts("unknown_at", nullable=True),
        sa.Column("failure_reason", sa.String(128), nullable=True),
        _ts("created_at"),
        sa.UniqueConstraint("provider", "request_fingerprint", name="uq_provider_call_request_fingerprint"),
        sa.CheckConstraint(
            "status IN ('prepared', 'dispatching', 'completed', 'not_sent', 'unknown')",
            name="ck_provider_call_status",
        ),
    )
    op.create_index("ix_provider_call_status", "provider_call", ["status", "created_at"])

    op.create_table(
        "usage_event",
        _id("usage_event_id", primary_key=True),
        sa.Column("event_kind", sa.String(32), nullable=False),
        sa.Column("execution_kind", sa.String(64), nullable=False),
        _id("execution_id", nullable=True),
        _id("attempt_id", nullable=True),
        _id("generation_id", nullable=True),
        _id("job_id", nullable=True),
        _id("graph_build_id", nullable=True),
        _id("provider_call_id", nullable=True),
        sa.Column("provider", sa.String(128), nullable=True),
        sa.Column("model", sa.String(256), nullable=True),
        sa.Column("operation", sa.String(128), nullable=True),
        sa.Column("provider_request_id", sa.String(512), nullable=True),
        sa.Column("stage", sa.String(128), nullable=True),
        sa.Column("resource_kind", sa.String(128), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("reasoning_tokens", sa.BigInteger(), nullable=True),
        sa.Column("embedding_input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("visual_input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("prompt_cache_hit_tokens", sa.BigInteger(), nullable=True),
        sa.Column("prompt_cache_miss_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cpu_milliseconds", sa.BigInteger(), nullable=True),
        sa.Column("gpu_milliseconds", sa.BigInteger(), nullable=True),
        sa.Column("peak_vram_bytes", sa.BigInteger(), nullable=True),
        sa.Column("estimated_cost_amount", sa.Numeric(24, 8), nullable=True),
        sa.Column("currency_code", sa.String(3), nullable=True),
        _id("price_version_id", nullable=True),
        sa.Column("adjustment_source_namespace", sa.String(128), nullable=True),
        sa.Column("adjustment_source_id", sa.String(256), nullable=True),
        sa.Column("adjustment_allocation_key", sa.String(256), nullable=True),
        sa.Column("cost_center_key", sa.String(256), nullable=True),
        _id("effective_calendar_version_id", nullable=False),
        _ts("effective_at"),
        sa.Column("effective_period", sa.String(7), nullable=False),
        _id("recorded_calendar_version_id", nullable=False),
        _ts("recorded_at"),
        sa.Column("recorded_period", sa.String(7), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("event_fingerprint", sa.String(128), nullable=False),
        _ts("started_at"),
        _ts("completed_at", nullable=True),
        _ts("created_at"),
        sa.ForeignKeyConstraint(["provider_call_id"], ["provider_call.provider_call_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["effective_calendar_version_id"],
            ["business_calendar_version.business_calendar_version_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["recorded_calendar_version_id"],
            ["business_calendar_version.business_calendar_version_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "event_kind", "adjustment_source_namespace", "adjustment_source_id", "adjustment_allocation_key",
            name="uq_usage_event_adjustment_source",
        ),
        sa.UniqueConstraint("provider_call_id", name="uq_usage_event_provider_call"),
        sa.CheckConstraint(
            "event_kind IN ('provider_usage', 'local_usage', 'usage_adjustment', 'cost_adjustment')",
            name="ck_usage_event_kind",
        ),
        sa.CheckConstraint(
            "outcome IN ('succeeded', 'failed', 'cancelled', 'unknown')", name="ck_usage_event_outcome"
        ),
    )
    op.create_index("ix_usage_event_effective_period", "usage_event", ["effective_period", "event_kind"])
    op.create_index("ix_usage_event_execution", "usage_event", ["execution_kind", "execution_id"])

    op.create_table(
        "local_usage_meter",
        _id("meter_id", primary_key=True),
        sa.Column("execution_kind", sa.String(64), nullable=False),
        _id("execution_id", nullable=False),
        sa.Column("stage", sa.String(128), nullable=False),
        sa.Column("resource_kind", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("checkpoint_input_tokens", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("checkpoint_cpu_milliseconds", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("checkpoint_gpu_milliseconds", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("checkpoint_peak_vram_bytes", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        _ts("started_at"),
        _ts("last_checkpoint_at"),
        _ts("completed_at", nullable=True),
        _ts("abandoned_at", nullable=True),
        sa.UniqueConstraint(
            "execution_kind", "execution_id", "stage", "resource_kind", name="uq_local_usage_meter_scope"
        ),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'abandoned')", name="ck_local_usage_meter_status"
        ),
    )

    op.create_table(
        "local_usage",
        _id("local_usage_id", primary_key=True),
        _id("meter_id", nullable=False),
        sa.Column("execution_kind", sa.String(64), nullable=False),
        _id("execution_id", nullable=False),
        sa.Column("stage", sa.String(128), nullable=False),
        sa.Column("resource_kind", sa.String(128), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cpu_milliseconds", sa.BigInteger(), nullable=True),
        sa.Column("gpu_milliseconds", sa.BigInteger(), nullable=True),
        sa.Column("peak_vram_bytes", sa.BigInteger(), nullable=True),
        _ts("started_at"),
        _ts("completed_at"),
        sa.ForeignKeyConstraint(["meter_id"], ["local_usage_meter.meter_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "execution_kind", "execution_id", "stage", "resource_kind", name="uq_local_usage_scope"
        ),
    )

    # Evaluation and index-generation facts.
    op.create_table(
        "shadow_evaluation_run",
        _id("run_id", primary_key=True),
        _id("space_id", nullable=False),
        _id("owner_user_id", nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("source_scope", JSON, nullable=False),
        sa.Column("question_snapshot", JSON, nullable=False),
        sa.Column("candidate_configs", JSON, nullable=False),
        _id("index_generation_id", nullable=True),
        sa.Column("evaluation_policy_version", sa.String(128), nullable=False),
        sa.Column("model_version", sa.String(256), nullable=False),
        sa.Column("prompt_version", sa.String(256), nullable=False),
        sa.Column("session_prefix", sa.String(256), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("lease_owner", sa.String(256), nullable=True),
        _ts("lease_expires_at", nullable=True),
        _ts("heartbeat_at", nullable=True),
        _ts("next_attempt_at", nullable=True),
        sa.Column("failure_reason", sa.String(128), nullable=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("request_fingerprint", sa.String(128), nullable=False),
        _json("report_reference"),
        _ts("created_at"),
        _ts("started_at", nullable=True),
        _ts("completed_at", nullable=True),
        sa.ForeignKeyConstraint(["space_id"], ["knowledge_space.space_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["user.user_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("owner_user_id", "idempotency_key", name="uq_shadow_evaluation_idempotency"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'retry_wait', 'succeeded', 'failed', 'cancelled')",
            name="ck_shadow_evaluation_status",
        ),
    )

    op.create_table(
        "index_generation",
        _id("index_generation_id", primary_key=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("base_revision", sa.BigInteger(), nullable=False),
        sa.Column("applied_revision", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("rollback_applied_revision", sa.BigInteger(), nullable=True),
        sa.Column("embedding_model", sa.String(256), nullable=False),
        sa.Column("embedding_dimension", sa.Integer(), nullable=False),
        sa.Column("metric", sa.String(32), nullable=False),
        _json("component_manifest", nullable=False),
        _ts("created_at"),
        _ts("activated_at", nullable=True),
        _ts("retired_at", nullable=True),
        _ts("rollback_until", nullable=True),
        _ts("purged_at", nullable=True),
        sa.CheckConstraint(
            "status IN ('staging', 'active', 'retired', 'failed', 'purging', 'purged')",
            name="ck_index_generation_status",
        ),
    )
    op.create_index(
        "uq_index_generation_active",
        "index_generation",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "index_change",
        _id("index_change_id", primary_key=True),
        sa.Column("revision", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("change_type", sa.String(32), nullable=False),
        _id("space_id", nullable=False),
        _id("document_id", nullable=False),
        _id("document_version_id", nullable=True),
        _id("publication_id", nullable=True),
        _json("source_locator"),
        _ts("created_at"),
        sa.ForeignKeyConstraint(["space_id"], ["knowledge_space.space_id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "change_type IN ('publish', 'replace', 'reindex', 'delete')", name="ck_index_change_type"
        ),
    )

    op.create_table(
        "graph_generation",
        _id("graph_generation_id", primary_key=True),
        _id("space_id", nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("source_revision", sa.BigInteger(), nullable=False),
        sa.Column("provider", sa.String(128), nullable=False),
        sa.Column("model", sa.String(256), nullable=False),
        sa.Column("prompt_version", sa.String(256), nullable=False),
        _ts("built_at", nullable=True),
        _ts("activated_at", nullable=True),
        sa.ForeignKeyConstraint(["space_id"], ["knowledge_space.space_id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "status IN ('staging', 'active', 'stale', 'retired', 'failed', 'purged')",
            name="ck_graph_generation_status",
        ),
    )

    op.create_table(
        "graph_build_run",
        _id("graph_build_id", primary_key=True),
        _id("operator_user_id", nullable=False),
        _id("space_id", nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("source_revision", sa.BigInteger(), nullable=False),
        sa.Column("estimated_primary_model_calls", sa.Integer(), nullable=False),
        sa.Column("actual_primary_model_calls", sa.Integer(), nullable=True),
        sa.Column("actual_provider_calls", sa.Integer(), nullable=True),
        _id("graph_generation_id", nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("request_fingerprint", sa.String(128), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("lease_owner", sa.String(256), nullable=True),
        _ts("lease_expires_at", nullable=True),
        _ts("created_at"),
        _ts("started_at", nullable=True),
        _ts("completed_at", nullable=True),
        sa.Column("failure_reason", sa.String(128), nullable=True),
        sa.ForeignKeyConstraint(["operator_user_id"], ["user.user_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["space_id"], ["knowledge_space.space_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["graph_generation_id"], ["graph_generation.graph_generation_id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("operator_user_id", "idempotency_key", name="uq_graph_build_idempotency"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_graph_build_status",
        ),
    )
    op.create_index(
        "uq_graph_build_nonterminal",
        "graph_build_run",
        ["space_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )

    op.create_table(
        "execution_authorization_fence",
        _id("fence_id", primary_key=True),
        sa.Column("authorization_source", sa.String(64), nullable=False),
        _id("actor_user_id", nullable=True),
        _id("submission_execution_grant_id", nullable=True),
        _id("space_id", nullable=False),
        _id("document_id", nullable=True),
        _id("document_version_id", nullable=True),
        _id("job_id", nullable=True),
        _id("attempt_id", nullable=True),
        _id("publication_id", nullable=True),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("allowed_actions", JSON, nullable=False),
        _ts("created_at"),
        _ts("revoked_at", nullable=True),
        sa.ForeignKeyConstraint(["actor_user_id"], ["user.user_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["submission_execution_grant_id"],
            ["submission_execution_grant.submission_execution_grant_id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["space_id"], ["knowledge_space.space_id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "authorization_source IN ('direct', 'shared_submission_ingest')",
            name="ck_authorization_fence_source",
        ),
    )

    op.create_table(
        "document_deletion",
        _id("deletion_id", primary_key=True),
        _id("document_id", nullable=False),
        _id("requested_by", nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        _ts("requested_at"),
        _ts("completed_at", nullable=True),
        sa.Column("failure_reason", sa.String(128), nullable=True),
        sa.ForeignKeyConstraint(["document_id"], ["document.document_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["requested_by"], ["user.user_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("document_id", name="uq_document_deletion_document"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')", name="ck_document_deletion_status"
        ),
    )

    op.create_table(
        "document_deletion_target",
        _id("target_id", primary_key=True),
        _id("deletion_id", nullable=False),
        _id("document_version_id", nullable=True),
        sa.Column("backend_kind", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(1024), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("failure_reason", sa.String(128), nullable=True),
        _ts("completed_at", nullable=True),
        sa.ForeignKeyConstraint(["deletion_id"], ["document_deletion.deletion_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_version.document_version_id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("deletion_id", "backend_kind", "resource_id", name="uq_document_deletion_target"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')", name="ck_document_deletion_target_status"
        ),
    )

    op.create_table(
        "audit_event",
        _id("event_id", primary_key=True),
        _id("actor_user_id", nullable=True),
        sa.Column("actor_role_snapshot", sa.String(32), nullable=True),
        _id("actor_department_id_snapshot", nullable=True),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("aggregate_type", sa.String(128), nullable=True),
        sa.Column("aggregate_id", sa.String(256), nullable=True),
        sa.Column("request_id", sa.String(256), nullable=True),
        sa.Column("trace_id", sa.String(256), nullable=True),
        _json("details", nullable=False),
        _ts("occurred_at"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["user.user_id"], ondelete="SET NULL"),
    )
    op.create_index("ix_audit_event_aggregate_time", "audit_event", ["aggregate_type", "aggregate_id", "occurred_at"])

    # Transactional outbox and its durable delivery state.
    op.create_table(
        "outbox_event",
        _id("event_id", primary_key=True),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("aggregate_type", sa.String(128), nullable=False),
        sa.Column("aggregate_id", sa.String(256), nullable=False),
        sa.Column("transition_version", sa.BigInteger(), nullable=False),
        _ts("occurred_at"),
        _json("payload", nullable=False),
        sa.Column("payload_fingerprint", sa.String(128), nullable=False),
        sa.Column("trace_id", sa.String(256), nullable=True),
        sa.Column("storage_state", sa.String(32), nullable=False, server_default=sa.text("'full'")),
        _ts("compact_after_at", nullable=True),
        _ts("compacted_at", nullable=True),
        _json("consumer_summaries"),
        _ts("created_at"),
        sa.UniqueConstraint(
            "event_type", "aggregate_type", "aggregate_id", "transition_version",
            name="uq_outbox_event_transition",
        ),
        sa.CheckConstraint(
            "storage_state IN ('full', 'compacted')", name="ck_outbox_event_storage_state"
        ),
    )
    op.create_index("ix_outbox_event_delivery", "outbox_event", ["storage_state", "created_at"])

    op.create_table(
        "outbox_recipient",
        _id("recipient_id", primary_key=True),
        _id("event_id", nullable=False),
        _id("recipient_user_id", nullable=True),
        sa.Column("recipient_kind", sa.String(32), nullable=False),
        sa.Column("required_role", sa.String(32), nullable=True),
        _ts("created_at"),
        sa.ForeignKeyConstraint(["event_id"], ["outbox_event.event_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["recipient_user_id"], ["user.user_id"], ondelete="SET NULL"),
        sa.UniqueConstraint("event_id", "recipient_user_id", name="uq_outbox_recipient_user"),
        sa.CheckConstraint(
            "recipient_kind IN ('identity', 'role_snapshot')", name="ck_outbox_recipient_kind"
        ),
    )

    op.create_table(
        "outbox_delivery",
        _id("delivery_id", primary_key=True),
        _id("event_id", nullable=False),
        sa.Column("consumer_name", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("replay_generation", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("lease_owner", sa.String(256), nullable=True),
        _ts("lease_expires_at", nullable=True),
        _ts("next_attempt_at", nullable=True),
        sa.Column("last_error_kind", sa.String(128), nullable=True),
        sa.Column("last_error_code", sa.String(128), nullable=True),
        _ts("delivered_at", nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.ForeignKeyConstraint(["event_id"], ["outbox_event.event_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("event_id", "consumer_name", name="uq_outbox_delivery_consumer"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'retry_wait', 'delivered', 'dead_letter')",
            name="ck_outbox_delivery_status",
        ),
    )
    op.create_index("ix_outbox_delivery_claim", "outbox_delivery", ["status", "next_attempt_at"])

    op.create_table(
        "outbox_delivery_attempt",
        _id("delivery_attempt_id", primary_key=True),
        _id("delivery_id", nullable=False),
        _id("event_id", nullable=False),
        sa.Column("consumer_name", sa.String(128), nullable=False),
        sa.Column("replay_generation", sa.Integer(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("cycle_attempt_number", sa.Integer(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("failure_kind", sa.String(128), nullable=True),
        sa.Column("failure_code", sa.String(128), nullable=True),
        _ts("started_at"),
        _ts("completed_at", nullable=True),
        sa.ForeignKeyConstraint(["delivery_id"], ["outbox_delivery.delivery_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["event_id"], ["outbox_event.event_id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "status IN ('running', 'delivered', 'failed', 'expired')",
            name="ck_outbox_delivery_attempt_status",
        ),
    )
    op.create_index(
        "ix_outbox_delivery_attempt_delivery",
        "outbox_delivery_attempt",
        ["delivery_id", "attempt_number"],
    )

    # Notification read model, in-page acknowledgements, suppression, and
    # permanent delivery receipts.
    op.create_table(
        "notification_inbox",
        _id("recipient_user_id", primary_key=True),
        sa.Column("next_notification_seq", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column("read_through_seq", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        _ts("read_all_at", nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.ForeignKeyConstraint(["recipient_user_id"], ["user.user_id"], ondelete="RESTRICT"),
        sa.CheckConstraint("next_notification_seq >= 1", name="ck_notification_inbox_next_seq"),
        sa.CheckConstraint("read_through_seq >= 0", name="ck_notification_inbox_read_seq"),
    )

    op.create_table(
        "notification",
        _id("notification_id", primary_key=True),
        _id("event_id", nullable=False),
        _id("recipient_user_id", nullable=False),
        sa.Column("notification_seq", sa.BigInteger(), nullable=False),
        sa.Column("type", sa.String(128), nullable=False),
        sa.Column("title", sa.String(1000), nullable=False),
        _json("payload", nullable=False),
        _ts("event_occurred_at"),
        _ts("materialized_at"),
        _ts("read_at", nullable=True),
        _ts("retire_after_at"),
        sa.ForeignKeyConstraint(["event_id"], ["outbox_event.event_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["recipient_user_id"], ["user.user_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("event_id", "recipient_user_id", name="uq_notification_event_recipient"),
        sa.UniqueConstraint("recipient_user_id", "notification_seq", name="uq_notification_user_sequence"),
    )
    op.create_index(
        "ix_notification_user_retention",
        "notification",
        ["recipient_user_id", "retire_after_at", "notification_seq"],
    )

    op.create_table(
        "notification_context_ack",
        _id("ack_id", primary_key=True),
        _id("event_id", nullable=False),
        _id("recipient_user_id", nullable=False),
        _ts("acked_at"),
        sa.ForeignKeyConstraint(["event_id"], ["outbox_event.event_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["recipient_user_id"], ["user.user_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("event_id", "recipient_user_id", name="uq_notification_context_ack"),
    )

    op.create_table(
        "notification_suppression",
        _id("suppression_id", primary_key=True),
        _id("event_id", nullable=False),
        _id("recipient_user_id", nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        _ts("created_at"),
        sa.ForeignKeyConstraint(["event_id"], ["outbox_event.event_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["recipient_user_id"], ["user.user_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("event_id", "recipient_user_id", name="uq_notification_suppression"),
        sa.CheckConstraint(
            "outcome IN ('recipient_inactive', 'recipient_unauthorized')",
            name="ck_notification_suppression_outcome",
        ),
    )

    op.create_table(
        "notification_delivery_receipt",
        _id("receipt_id", primary_key=True),
        _id("event_id", nullable=False),
        _id("recipient_user_id", nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("notification_seq", sa.BigInteger(), nullable=True),
        _ts("event_occurred_at"),
        _ts("materialized_at", nullable=True),
        _ts("retired_at"),
        sa.Column("receipt_fingerprint", sa.String(128), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["outbox_event.event_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["recipient_user_id"], ["user.user_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("event_id", "recipient_user_id", name="uq_notification_delivery_receipt"),
        sa.CheckConstraint(
            "outcome IN ('materialized', 'recipient_inactive', 'recipient_unauthorized')",
            name="ck_notification_receipt_outcome",
        ),
    )

    # Account deletion is a lifecycle fact; the user row remains as an
    # opaque tombstone so historical IDs and immutable receipts remain valid.
    op.create_table(
        "user_deletion",
        _id("deletion_id", primary_key=True),
        _id("user_id", nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        _ts("requested_at"),
        _ts("purge_after_at", nullable=True),
        _ts("completed_at", nullable=True),
        sa.Column("failure_reason", sa.String(128), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.user_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("user_id", name="uq_user_deletion_user"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')", name="ck_user_deletion_status"
        ),
    )

    op.create_table(
        "user_archive",
        _id("archive_id", primary_key=True),
        _id("user_id", nullable=False),
        _id("deletion_id", nullable=False),
        sa.Column("manifest_version", sa.String(128), nullable=False),
        sa.Column("object_id", sa.String(1024), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(128), nullable=False),
        _ts("archive_completed_at"),
        sa.ForeignKeyConstraint(["user_id"], ["user.user_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["deletion_id"], ["user_deletion.deletion_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("deletion_id", name="uq_user_archive_deletion"),
    )


def downgrade() -> None:
    # Drop in reverse dependency order so the baseline can be removed in
    # development and CI without relying on CASCADE.
    for table in (
        "user_archive",
        "user_deletion",
        "notification_delivery_receipt",
        "notification_suppression",
        "notification_context_ack",
        "notification",
        "notification_inbox",
        "outbox_delivery_attempt",
        "outbox_delivery",
        "outbox_recipient",
        "outbox_event",
        "audit_event",
        "document_deletion_target",
        "document_deletion",
        "execution_authorization_fence",
        "graph_build_run",
        "graph_generation",
        "index_change",
        "index_generation",
        "shadow_evaluation_run",
        "local_usage",
        "local_usage_meter",
        "usage_event",
        "provider_call",
        "quota_debit",
        "quota_request",
        "quota_projection",
        "business_calendar_version",
        "ab_vote",
        "ab_pair_candidate",
        "ab_pair",
        "generation_candidate",
        "generation_event",
        "generation_execution",
        "generation",
        "calibration_window_command",
        "calibration_window",
        "chunk",
        "publication",
        "ingestion_attempt",
        "ingestion_job",
        "submission_execution_grant",
        "knowledge_submission",
        "upload_batch_item",
        "upload_batch",
        "upload_dedup_claim",
        "document_version",
        "document",
        "message_feedback",
        "message",
        "conversation",
        "conversation_group",
        "knowledge_space",
        "auth_refresh_token",
        "auth_session",
        "user_preference",
        "user",
        "department",
    ):
        op.drop_table(table)
