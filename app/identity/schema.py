from __future__ import annotations

from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Integer, MetaData, String, Table

identity_metadata = MetaData()


identity_department_table = Table(
    "identity_department",
    identity_metadata,
    Column("id", String(64), primary_key=True),
    Column("name", String(256), nullable=False),
    Column("normalized_name", String(256), nullable=False, unique=True),
    Column("status", String(32), nullable=False),
    Column("version", Integer, nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("updated_at_utc", DateTime(timezone=True), nullable=False),
    Column("deactivated_at_utc", DateTime(timezone=True), nullable=True),
)


identity_user_table = Table(
    "identity_user",
    identity_metadata,
    Column("id", String(64), primary_key=True),
    Column("username", String(128), nullable=False),
    Column("normalized_username", String(128), nullable=False, unique=True),
    Column("password_hash", String(512), nullable=False),
    Column("real_name", String(256), nullable=False),
    Column("display_name", String(256), nullable=False),
    Column("directory_search_text", String(4096), nullable=False, server_default=""),
    Column("department_id", String(64), ForeignKey("identity_department.id"), nullable=True),
    Column("role", String(32), nullable=False),
    Column("lifecycle_status", String(32), nullable=False),
    Column("version", Integer, nullable=False),
    Column("avatar_url", String(1024), nullable=True),
    Column("preferences_json", JSON, nullable=False),
    Column("transition_version", Integer, nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("updated_at_utc", DateTime(timezone=True), nullable=False),
    Column("deletion_requested_at_utc", DateTime(timezone=True), nullable=True),
    Column("purge_after_at_utc", DateTime(timezone=True), nullable=True),
)


identity_deletion_workflow_table = Table(
    "identity_deletion_workflow",
    identity_metadata,
    Column("user_id", String(64), ForeignKey("identity_user.id"), primary_key=True),
    Column("status", String(32), nullable=False),
    Column("requested_at_utc", DateTime(timezone=True), nullable=False),
    Column("purge_after_at_utc", DateTime(timezone=True), nullable=False),
    Column("cleanup_operation_id", String(128), nullable=False, unique=True),
    Column("cleanup_reference", String(256), nullable=True),
    Column("cleanup_completed_at_utc", DateTime(timezone=True), nullable=True),
    Column("archive_ref", String(512), nullable=True),
    Column("archive_checksum", String(128), nullable=True),
    Column("archive_dir_snapshot", String(1024), nullable=True),
    Column("archive_file_name", String(256), nullable=True),
    Column("archive_size_bytes", Integer, nullable=True),
    Column("archive_sha256", String(128), nullable=True),
    Column("archive_completed_at_utc", DateTime(timezone=True), nullable=True),
    Column("archive_failed_at_utc", DateTime(timezone=True), nullable=True),
    Column("archive_alert", String(64), nullable=True),
    Column("retirement_receipt_id", String(128), nullable=True),
    Column("completed_at_utc", DateTime(timezone=True), nullable=True),
)


identity_account_cleanup_target_table = Table(
    "identity_account_cleanup_target",
    identity_metadata,
    Column(
        "deletion_id",
        String(128),
        primary_key=True,
    ),
    Column("backend_kind", String(64), primary_key=True),
    Column("resource_id", String(256), primary_key=True),
    Column("status", String(32), nullable=False),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("last_error", String(512), nullable=True),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("completed_at_utc", DateTime(timezone=True), nullable=True),
    Column("updated_at_utc", DateTime(timezone=True), nullable=False),
)


identity_space_table = Table(
    "identity_space",
    identity_metadata,
    Column("id", String(128), primary_key=True),
    Column("kind", String(32), nullable=False),
    Column("name", String(256), nullable=False),
    Column("owner_user_id", String(64), ForeignKey("identity_user.id"), nullable=True),
    Column("department_id", String(64), ForeignKey("identity_department.id"), nullable=True),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
)


auth_session_table = Table(
    "auth_session",
    identity_metadata,
    Column("id", String(96), primary_key=True),
    Column("user_id", String(64), ForeignKey("identity_user.id"), nullable=False),
    Column("device", String(256), nullable=False),
    Column("current_sequence", Integer, nullable=False),
    Column("family_expires_at_utc", DateTime(timezone=True), nullable=False),
    Column("last_active_at_utc", DateTime(timezone=True), nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("revoked_at_utc", DateTime(timezone=True), nullable=True),
    Column("revoked_reason", String(128), nullable=True),
    Column("identity_transition_version", Integer, nullable=False),
)


auth_refresh_token_table = Table(
    "auth_refresh_token",
    identity_metadata,
    Column("auth_session_id", String(96), ForeignKey("auth_session.id"), primary_key=True),
    Column("sequence", Integer, primary_key=True),
    Column("token_hash", String(128), nullable=False, unique=True),
    Column("issued_at_utc", DateTime(timezone=True), nullable=False),
    Column("consumed_at_utc", DateTime(timezone=True), nullable=True),
    Column("replay_payload", String(4096), nullable=True),
    Column("replay_expires_at_utc", DateTime(timezone=True), nullable=True),
)


identity_revocation_command_table = Table(
    "identity_revocation_command",
    identity_metadata,
    Column("operation_id", String(128), primary_key=True),
    Column("user_id", String(64), nullable=False),
    Column("auth_session_id", String(96), nullable=True),
    Column("reason", String(128), nullable=False),
    Column("identity_transition_version", Integer, nullable=False),
    Column("receipt_reference", String(256), nullable=False),
    Column("receipt_state", String(32), nullable=False),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
)


identity_object_cleanup_table = Table(
    "identity_object_cleanup",
    identity_metadata,
    Column("operation_id", String(128), primary_key=True),
    Column("user_id", String(64), ForeignKey("identity_user.id"), nullable=False),
    Column("object_key", String(1024), nullable=False, unique=True),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
    Column("completed_at_utc", DateTime(timezone=True), nullable=True),
)


identity_login_throttle_table = Table(
    "identity_login_throttle",
    identity_metadata,
    Column("normalized_username", String(128), primary_key=True),
    Column("failed_attempts", Integer, nullable=False),
    Column("locked_until_utc", DateTime(timezone=True), nullable=True),
    Column("updated_at_utc", DateTime(timezone=True), nullable=False),
)


identity_idempotency_table = Table(
    "identity_idempotency",
    identity_metadata,
    Column("actor_id", String(64), primary_key=True),
    Column("endpoint", String(256), primary_key=True),
    Column("target_id", String(128), primary_key=True),
    Column("idempotency_key", String(256), primary_key=True),
    Column("request_hash", String(128), nullable=False),
    Column("completed", Boolean, nullable=False),
    Column("response_json", JSON, nullable=True),
    Column("created_at_utc", DateTime(timezone=True), nullable=False),
)


IDENTITY_TABLE_NAMES = frozenset(identity_metadata.tables)
