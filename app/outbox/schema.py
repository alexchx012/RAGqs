"""Outbox and notification persistence model.

The outbox owns event, recipient, delivery, attempt, notification, inbox, ack,
suppression, receipt, lifecycle-command and metric tables. Identity and
notification domain state never shares tables with other domains.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)

outbox_metadata = MetaData()

outbox_event_table = Table(
    "outbox_event",
    outbox_metadata,
    Column("event_id", String(64), primary_key=True),
    Column("event_type", String(64), nullable=False),
    Column("schema_version", Integer, nullable=True),
    Column("aggregate_type", String(64), nullable=False),
    Column("aggregate_id", String(128), nullable=False),
    Column("transition_version", BigInteger, nullable=False),
    Column("occurred_at_utc", DateTime(timezone=True), nullable=False),
    Column("payload_json", JSON(none_as_null=True), nullable=True),
    Column("payload_fingerprint", String(128), nullable=False),
    Column("trace_id", String(128), nullable=True),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("storage_state", String(16), nullable=False),
    Column("compact_after_at_utc", DateTime(timezone=True), nullable=True),
    Column("compacted_at_utc", DateTime(timezone=True), nullable=True),
    Column("compacted_delivery_summary_json", JSON(none_as_null=True), nullable=True),
    UniqueConstraint(
        "event_type",
        "aggregate_type",
        "aggregate_id",
        "transition_version",
        name="uq_outbox_event_key",
    ),
    CheckConstraint("storage_state IN ('full', 'compacted')", name="ck_outbox_event_storage_state"),
    # Full events must never carry compacted facts: the only path that sets
    # compacted_at_utc / compacted_delivery_summary_json is full -> compacted.
    CheckConstraint(
        "storage_state = 'compacted' OR (compacted_at_utc IS NULL AND compacted_delivery_summary_json IS NULL)",
        name="ck_outbox_event_compacted_fields_full_null",
    ),
)

outbox_recipient_table = Table(
    "outbox_recipient",
    outbox_metadata,
    Column("event_id", String(64), ForeignKey("outbox_event.event_id"), primary_key=True),
    Column("recipient_user_id", String(64), primary_key=True),
    Column("recipient_kind", String(32), nullable=False),
    Column("required_role", String(32), nullable=True),
    Column("selection_reason", String(256), nullable=False),
    Column("role_snapshot", String(32), nullable=False),
    Column("lifecycle_snapshot", String(32), nullable=False),
    Column("selected_at_utc", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "recipient_kind IN ('identity', 'role_snapshot')",
        name="ck_outbox_recipient_kind",
    ),
)

outbox_delivery_table = Table(
    "outbox_delivery",
    outbox_metadata,
    Column("event_id", String(64), ForeignKey("outbox_event.event_id"), primary_key=True),
    Column("consumer_name", String(64), primary_key=True),
    Column("status", String(32), nullable=False),
    Column("version", Integer, nullable=False),
    Column("replay_generation", Integer, nullable=False),
    Column("attempt_number", Integer, nullable=False),
    Column("cycle_attempt_number", Integer, nullable=False),
    Column("error_category", String(32), nullable=True),
    Column("error_code", String(128), nullable=True),
    Column("next_attempt_at_utc", DateTime(timezone=True), nullable=True),
    Column("lease_owner", String(128), nullable=True),
    Column("lease_expires_at_utc", DateTime(timezone=True), nullable=True),
    Column("fence_token", BigInteger, nullable=True),
    Column("delivered_at_utc", DateTime(timezone=True), nullable=True),
    CheckConstraint("version >= 1", name="ck_outbox_delivery_version_positive"),
    CheckConstraint("attempt_number >= 0", name="ck_outbox_delivery_attempt_nonnegative"),
    CheckConstraint("cycle_attempt_number >= 0", name="ck_outbox_delivery_cycle_nonnegative"),
    Index("ix_outbox_delivery_claim", "status", "next_attempt_at_utc"),
    CheckConstraint(
        "status IN ('pending', 'running', 'retry_wait', 'delivered', 'dead_letter')",
        name="ck_outbox_delivery_status",
    ),
)

outbox_delivery_attempt_table = Table(
    "outbox_delivery_attempt",
    outbox_metadata,
    Column("delivery_attempt_id", String(64), primary_key=True),
    Column("event_id", String(64), nullable=False),
    Column("consumer_name", String(64), nullable=False),
    Column("replay_generation", Integer, nullable=False),
    Column("attempt_number", Integer, nullable=False),
    Column("cycle_attempt_number", Integer, nullable=False),
    Column("fence_token", BigInteger, nullable=False),
    Column("started_at_utc", DateTime(timezone=True), nullable=False),
    Column("ended_at_utc", DateTime(timezone=True), nullable=True),
    Column("status", String(32), nullable=False),
    Column("error_category", String(32), nullable=True),
    Column("error_code", String(128), nullable=True),
    CheckConstraint(
        "status IN ('running', 'delivered', 'failed', 'expired')",
        name="ck_outbox_delivery_attempt_status",
    ),
    # fence 失效/compaction 按 event_id 扫描 attempt 历史。
    Index("ix_outbox_delivery_attempt_event", "event_id"),
)

notification_table = Table(
    "notification",
    outbox_metadata,
    Column("id", String(64), primary_key=True),
    Column("event_id", String(64), nullable=False),
    Column("recipient_user_id", String(64), nullable=False),
    Column("notification_type", String(64), nullable=False),
    Column("title", String(512), nullable=False),
    Column("payload_json", JSON, nullable=False),
    Column("document_id", String(128), nullable=True),
    Column("document_version_id", String(128), nullable=True),
    Column("event_occurred_at_utc", DateTime(timezone=True), nullable=False),
    Column("materialized_at_utc", DateTime(timezone=True), nullable=False),
    Column("notification_seq", BigInteger, nullable=False),
    Column("read_at_utc", DateTime(timezone=True), nullable=True),
    Column("retire_after_at_utc", DateTime(timezone=True), nullable=False),
    Column("redacted", Boolean, nullable=False),
    CheckConstraint("notification_seq >= 1", name="ck_notification_seq_positive"),
    UniqueConstraint("event_id", "recipient_user_id", name="uq_notification_event_recipient"),
    # uq_notification_recipient_seq 的唯一约束自带 (recipient_user_id,
    # notification_seq) 索引；不再保留重复的普通索引（A30）。
    UniqueConstraint("recipient_user_id", "notification_seq", name="uq_notification_recipient_seq"),
    Index("ix_notification_document", "document_id", "document_version_id"),
)

notification_inbox_table = Table(
    "notification_inbox",
    outbox_metadata,
    Column("recipient_user_id", String(64), primary_key=True),
    Column("next_notification_seq", BigInteger, nullable=False),
    Column("read_through_seq", BigInteger, nullable=False),
    Column("read_all_at_utc", DateTime(timezone=True), nullable=True),
    Column("version", Integer, nullable=False),
    Column("retired", Boolean, nullable=False),
    CheckConstraint("next_notification_seq >= 1", name="ck_inbox_next_seq_positive"),
    CheckConstraint("read_through_seq >= 0", name="ck_inbox_read_through_nonnegative"),
    CheckConstraint("version >= 1", name="ck_inbox_version_positive"),
)

outbox_account_retirement_tombstone_table = Table(
    "outbox_account_retirement_tombstone",
    outbox_metadata,
    Column("recipient_user_id", String(64), primary_key=True),
    Column("next_notification_seq", BigInteger, nullable=False),
    Column("read_through_seq", BigInteger, nullable=False),
    Column("retired_at_utc", DateTime(timezone=True), nullable=False),
)

notification_suppression_table = Table(
    "notification_suppression",
    outbox_metadata,
    Column("event_id", String(64), primary_key=True),
    Column("recipient_user_id", String(64), primary_key=True),
    Column("reason", String(32), nullable=False),
    Column("suppressed_at_utc", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "reason IN ('recipient_inactive', 'recipient_unauthorized')",
        name="ck_notification_suppression_reason",
    ),
)

notification_context_ack_table = Table(
    "notification_context_ack",
    outbox_metadata,
    Column("event_id", String(64), primary_key=True),
    Column("recipient_user_id", String(64), primary_key=True),
    Column("acked_at_utc", DateTime(timezone=True), nullable=False),
)

notification_delivery_receipt_table = Table(
    "notification_delivery_receipt",
    outbox_metadata,
    Column("event_id", String(64), primary_key=True),
    Column("recipient_user_id", String(64), primary_key=True),
    Column("outcome", String(32), nullable=False),
    Column("original_notification_seq", BigInteger, nullable=True),
    Column("occurred_at_utc", DateTime(timezone=True), nullable=False),
    Column("materialized_at_utc", DateTime(timezone=True), nullable=True),
    Column("retired_at_utc", DateTime(timezone=True), nullable=True),
    Column("fingerprint", String(128), nullable=False),
    CheckConstraint(
        "outcome IN ('materialized', 'recipient_inactive', 'recipient_unauthorized')",
        name="ck_notification_receipt_outcome",
    ),
)

outbox_redaction_receipt_table = Table(
    "outbox_redaction_receipt",
    outbox_metadata,
    Column("operation_id", String(128), primary_key=True),
    Column("deletion_id", String(128), nullable=False),
    Column("document_id", String(128), nullable=False),
    Column("document_version_ids_json", JSON, nullable=False),
    Column("input_fingerprint", String(128), nullable=False),
    Column("state", String(16), nullable=False),
    Column("redacted_notification_count", Integer, nullable=False),
    Column("already_redacted_count", Integer, nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
)

outbox_document_tombstone_table = Table(
    "outbox_document_tombstone",
    outbox_metadata,
    Column("document_id", String(128), primary_key=True),
    Column("document_version_id", String(128), primary_key=True),
    Column("deletion_id", String(128), nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
)

outbox_retirement_command_table = Table(
    "outbox_retirement_command",
    outbox_metadata,
    Column("operation_id", String(128), primary_key=True),
    Column("user_id", String(64), nullable=False),
    Column("deletion_id", String(128), nullable=False),
    Column("input_fingerprint", String(128), nullable=False),
    Column("archive_ref", String(512), nullable=False),
    Column("archive_checksum", String(128), nullable=False),
    Column("archive_ref_fingerprint", String(128), nullable=False),
    Column("mode", String(16), nullable=False),
    Column("state", String(16), nullable=False),
    Column("receipt_json", JSON, nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("completed_at_utc", DateTime(timezone=True), nullable=True),
)

outbox_compaction_command_table = Table(
    "outbox_compaction_command",
    outbox_metadata,
    Column("operation_id", String(128), primary_key=True),
    Column("user_id", String(64), nullable=False),
    Column("deletion_id", String(128), nullable=False),
    Column("retirement_receipt_id", String(128), nullable=False),
    Column("input_fingerprint", String(128), nullable=False),
    Column("state", String(16), nullable=False),
    Column("receipt_json", JSON, nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("completed_at_utc", DateTime(timezone=True), nullable=True),
)

outbox_replay_idempotency_table = Table(
    "outbox_replay_idempotency",
    outbox_metadata,
    Column("event_id", String(64), primary_key=True),
    Column("consumer_name", String(64), primary_key=True),
    Column("idempotency_key", String(256), primary_key=True),
    Column("request_hash", String(128), nullable=False),
    Column("completed", Boolean, nullable=False),
    Column("response_json", JSON, nullable=True),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
)

outbox_metric_table = Table(
    "outbox_metric",
    outbox_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("metric_name", String(64), nullable=False),
    Column("observed_at_utc", DateTime(timezone=True), nullable=False),
    Column("value", Float, nullable=False),
    Column("event_id", String(64), nullable=True),
)

OUTBOX_TABLE_NAMES = frozenset(outbox_metadata.tables)
