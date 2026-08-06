"""Create identity and access-control domain tables.

Revision ID: 0002_identity_access
Revises: 0001_core_platform_initial
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_identity_access"
down_revision: str | None = "0001_core_platform_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "identity_department",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("normalized_name", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deactivated_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("normalized_name"),
    )
    op.create_table(
        "identity_user",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("normalized_username", sa.String(length=128), nullable=False),
        sa.Column("password_hash", sa.String(length=512), nullable=False),
        sa.Column("real_name", sa.String(length=256), nullable=False),
        sa.Column("display_name", sa.String(length=256), nullable=False),
        sa.Column("department_id", sa.String(length=64), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("lifecycle_status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("avatar_url", sa.String(length=1024), nullable=True),
        sa.Column("preferences_json", sa.JSON(), nullable=False),
        sa.Column("transition_version", sa.Integer(), nullable=False),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deletion_requested_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purge_after_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["department_id"], ["identity_department.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("normalized_username"),
    )
    op.create_table(
        "identity_deletion_workflow",
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("requested_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("purge_after_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cleanup_operation_id", sa.String(length=128), nullable=False),
        sa.Column("cleanup_reference", sa.String(length=256), nullable=True),
        sa.Column("cleanup_completed_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["identity_user.id"]),
        sa.PrimaryKeyConstraint("user_id"),
        sa.UniqueConstraint("cleanup_operation_id"),
    )
    op.create_table(
        "identity_space",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("owner_user_id", sa.String(length=64), nullable=True),
        sa.Column("department_id", sa.String(length=64), nullable=True),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["department_id"], ["identity_department.id"]),
        sa.ForeignKeyConstraint(["owner_user_id"], ["identity_user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "auth_session",
        sa.Column("id", sa.String(length=96), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("device", sa.String(length=256), nullable=False),
        sa.Column("current_sequence", sa.Integer(), nullable=False),
        sa.Column("family_expires_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_active_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=128), nullable=True),
        sa.Column("identity_transition_version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["identity_user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "auth_refresh_token",
        sa.Column("auth_session_id", sa.String(length=96), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("issued_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replay_payload", sa.String(length=4096), nullable=True),
        sa.Column("replay_expires_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["auth_session_id"], ["auth_session.id"]),
        sa.PrimaryKeyConstraint("auth_session_id", "sequence"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_table(
        "identity_revocation_command",
        sa.Column("operation_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("auth_session_id", sa.String(length=96), nullable=True),
        sa.Column("reason", sa.String(length=128), nullable=False),
        sa.Column("identity_transition_version", sa.Integer(), nullable=False),
        sa.Column("receipt_reference", sa.String(length=256), nullable=False),
        sa.Column("receipt_state", sa.String(length=32), nullable=False),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("operation_id"),
    )
    op.create_table(
        "identity_object_cleanup",
        sa.Column("operation_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["identity_user.id"]),
        sa.PrimaryKeyConstraint("operation_id"),
        sa.UniqueConstraint("object_key"),
    )
    op.create_table(
        "identity_login_throttle",
        sa.Column("normalized_username", sa.String(length=128), nullable=False),
        sa.Column("failed_attempts", sa.Integer(), nullable=False),
        sa.Column("locked_until_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("normalized_username"),
    )
    op.create_table(
        "identity_idempotency",
        sa.Column("actor_id", sa.String(length=64), nullable=False),
        sa.Column("endpoint", sa.String(length=256), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column("request_hash", sa.String(length=128), nullable=False),
        sa.Column("completed", sa.Boolean(), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=True),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("actor_id", "endpoint", "target_id", "idempotency_key"),
    )


def downgrade() -> None:
    op.drop_table("identity_idempotency")
    op.drop_table("identity_login_throttle")
    op.drop_table("identity_object_cleanup")
    op.drop_table("identity_revocation_command")
    op.drop_table("auth_refresh_token")
    op.drop_table("auth_session")
    op.drop_table("identity_space")
    op.drop_table("identity_deletion_workflow")
    op.drop_table("identity_user")
    op.drop_table("identity_department")
