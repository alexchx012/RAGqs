"""Create outbox and notification domain tables.

Revision ID: 0003_outbox_notifications
Revises: 0002_identity_access
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_outbox_notifications"
down_revision: str | None = "0002_identity_access"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outbox_event",
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=True),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.String(length=128), nullable=False),
        sa.Column("transition_version", sa.BigInteger(), nullable=False),
        sa.Column("occurred_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("payload_fingerprint", sa.String(length=128), nullable=False),
        sa.Column("trace_id", sa.String(length=128), nullable=True),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("storage_state", sa.String(length=16), nullable=False),
        sa.Column("compact_after_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("compacted_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("compacted_delivery_summary_json", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint(
            "event_type",
            "aggregate_type",
            "aggregate_id",
            "transition_version",
            name="uq_outbox_event_key",
        ),
        sa.CheckConstraint(
            "storage_state IN ('full', 'compacted')", name="ck_outbox_event_storage_state"
        ),
        sa.CheckConstraint(
            "storage_state = 'compacted' OR (compacted_at_utc IS NULL AND compacted_delivery_summary_json IS NULL)",
            name="ck_outbox_event_compacted_fields_full_null",
        ),
    )
    op.create_table(
        "outbox_recipient",
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("recipient_user_id", sa.String(length=64), nullable=False),
        sa.Column("recipient_kind", sa.String(length=32), nullable=False),
        sa.Column("required_role", sa.String(length=32), nullable=True),
        sa.Column("selection_reason", sa.String(length=256), nullable=False),
        sa.Column("role_snapshot", sa.String(length=32), nullable=False),
        sa.Column("lifecycle_snapshot", sa.String(length=32), nullable=False),
        sa.Column("selected_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["outbox_event.event_id"]),
        sa.PrimaryKeyConstraint("event_id", "recipient_user_id"),
        sa.CheckConstraint(
            "recipient_kind IN ('identity', 'role_snapshot')",
            name="ck_outbox_recipient_kind",
        ),
    )
    op.create_table(
        "outbox_delivery",
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("consumer_name", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("replay_generation", sa.Integer(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("cycle_attempt_number", sa.Integer(), nullable=False),
        sa.Column("error_category", sa.String(length=32), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("next_attempt_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fence_token", sa.BigInteger(), nullable=True),
        sa.Column("delivered_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["event_id"], ["outbox_event.event_id"]),
        sa.PrimaryKeyConstraint("event_id", "consumer_name"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'retry_wait', 'delivered', 'dead_letter')",
            name="ck_outbox_delivery_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_outbox_delivery_version_positive"),
        sa.CheckConstraint("attempt_number >= 0", name="ck_outbox_delivery_attempt_nonnegative"),
        sa.CheckConstraint(
            "cycle_attempt_number >= 0", name="ck_outbox_delivery_cycle_nonnegative"
        ),
    )
    op.create_index(
        "ix_outbox_delivery_claim", "outbox_delivery", ["status", "next_attempt_at_utc"]
    )
    op.create_table(
        "outbox_delivery_attempt",
        sa.Column("delivery_attempt_id", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("consumer_name", sa.String(length=64), nullable=False),
        sa.Column("replay_generation", sa.Integer(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("cycle_attempt_number", sa.Integer(), nullable=False),
        sa.Column("fence_token", sa.BigInteger(), nullable=False),
        sa.Column("started_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_category", sa.String(length=32), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint("delivery_attempt_id"),
        sa.CheckConstraint(
            "status IN ('running', 'delivered', 'failed', 'expired')",
            name="ck_outbox_delivery_attempt_status",
        ),
    )
    op.create_table(
        "notification",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("recipient_user_id", sa.String(length=64), nullable=False),
        sa.Column("notification_type", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("document_id", sa.String(length=128), nullable=True),
        sa.Column("document_version_id", sa.String(length=128), nullable=True),
        sa.Column("event_occurred_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("materialized_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notification_seq", sa.BigInteger(), nullable=False),
        sa.Column("read_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retire_after_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("redacted", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("notification_seq >= 1", name="ck_notification_seq_positive"),
        sa.UniqueConstraint(
            "event_id", "recipient_user_id", name="uq_notification_event_recipient"
        ),
        sa.UniqueConstraint(
            "recipient_user_id", "notification_seq", name="uq_notification_recipient_seq"
        ),
    )
    op.create_index(
        "ix_notification_recipient_seq", "notification", ["recipient_user_id", "notification_seq"]
    )
    op.create_index(
        "ix_notification_document", "notification", ["document_id", "document_version_id"]
    )
    op.create_table(
        "notification_inbox",
        sa.Column("recipient_user_id", sa.String(length=64), nullable=False),
        sa.Column("next_notification_seq", sa.BigInteger(), nullable=False),
        sa.Column("read_through_seq", sa.BigInteger(), nullable=False),
        sa.Column("read_all_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("retired", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("recipient_user_id"),
        sa.CheckConstraint("next_notification_seq >= 1", name="ck_inbox_next_seq_positive"),
        sa.CheckConstraint("read_through_seq >= 0", name="ck_inbox_read_through_nonnegative"),
        sa.CheckConstraint("version >= 1", name="ck_inbox_version_positive"),
    )
    op.create_table(
        "notification_suppression",
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("recipient_user_id", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("suppressed_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id", "recipient_user_id"),
        sa.CheckConstraint(
            "reason IN ('recipient_inactive', 'recipient_unauthorized')",
            name="ck_notification_suppression_reason",
        ),
    )
    op.create_table(
        "notification_context_ack",
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("recipient_user_id", sa.String(length=64), nullable=False),
        sa.Column("acked_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id", "recipient_user_id"),
    )
    op.create_table(
        "notification_delivery_receipt",
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("recipient_user_id", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("original_notification_seq", sa.BigInteger(), nullable=True),
        sa.Column("occurred_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("materialized_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fingerprint", sa.String(length=128), nullable=False),
        sa.PrimaryKeyConstraint("event_id", "recipient_user_id"),
        sa.CheckConstraint(
            "outcome IN ('materialized', 'recipient_inactive', 'recipient_unauthorized')",
            name="ck_notification_receipt_outcome",
        ),
    )
    op.create_table(
        "outbox_redaction_receipt",
        sa.Column("operation_id", sa.String(length=128), nullable=False),
        sa.Column("deletion_id", sa.String(length=128), nullable=False),
        sa.Column("document_id", sa.String(length=128), nullable=False),
        sa.Column("document_version_ids_json", sa.JSON(), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("redacted_notification_count", sa.Integer(), nullable=False),
        sa.Column("already_redacted_count", sa.Integer(), nullable=False),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("operation_id"),
    )
    op.create_table(
        "outbox_retirement_command",
        sa.Column("operation_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("deletion_id", sa.String(length=128), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=128), nullable=False),
        sa.Column("archive_ref", sa.String(length=512), nullable=False),
        sa.Column("archive_checksum", sa.String(length=128), nullable=False),
        sa.Column("archive_ref_fingerprint", sa.String(length=128), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("receipt_json", sa.JSON(), nullable=False),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("operation_id"),
    )
    op.create_table(
        "outbox_compaction_command",
        sa.Column("operation_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("deletion_id", sa.String(length=128), nullable=False),
        sa.Column("retirement_receipt_id", sa.String(length=128), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("receipt_json", sa.JSON(), nullable=False),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("operation_id"),
    )
    op.create_table(
        "outbox_document_tombstone",
        sa.Column("document_id", sa.String(length=128), nullable=False),
        sa.Column("document_version_id", sa.String(length=128), nullable=False),
        sa.Column("deletion_id", sa.String(length=128), nullable=False),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("document_id", "document_version_id"),
    )
    op.create_table(
        "outbox_replay_idempotency",
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("consumer_name", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column("request_hash", sa.String(length=128), nullable=False),
        sa.Column("completed", sa.Boolean(), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=True),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id", "consumer_name", "idempotency_key"),
    )
    op.create_table(
        "outbox_metric",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("metric_name", sa.String(length=64), nullable=False),
        sa.Column("observed_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("outbox_metric")
    op.drop_table("outbox_document_tombstone")
    op.drop_table("outbox_replay_idempotency")
    op.drop_table("outbox_compaction_command")
    op.drop_table("outbox_retirement_command")
    op.drop_table("outbox_redaction_receipt")
    op.drop_table("notification_delivery_receipt")
    op.drop_table("notification_context_ack")
    op.drop_table("notification_suppression")
    op.drop_table("notification_inbox")
    op.drop_index("ix_notification_document", table_name="notification")
    op.drop_index("ix_notification_recipient_seq", table_name="notification")
    op.drop_table("notification")
    op.drop_table("outbox_delivery_attempt")
    op.drop_index("ix_outbox_delivery_claim", table_name="outbox_delivery")
    op.drop_table("outbox_delivery")
    op.drop_table("outbox_recipient")
    op.drop_table("outbox_event")
