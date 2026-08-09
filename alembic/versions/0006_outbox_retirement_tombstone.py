"""Separate permanent account-retirement tombstone.

Migration 0003 originally planned an inbox-retired flag; the final semantic is
a dedicated permanent tombstone table that survives after the inbox row is
removed, preventing any later materialization from rebuilding notifications
or reusing sequences.

Revision ID: 0006_outbox_retirement_tombstone
Revises: 0005_outbox_immutable_triggers
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_outbox_retirement_tombstone"
down_revision: str | None = "0005_outbox_immutable_triggers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outbox_account_retirement_tombstone",
        sa.Column("recipient_user_id", sa.String(length=64), nullable=False),
        sa.Column("next_notification_seq", sa.BigInteger(), nullable=False),
        sa.Column("read_through_seq", sa.BigInteger(), nullable=False),
        sa.Column("retired_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("recipient_user_id"),
    )


def downgrade() -> None:
    op.drop_table("outbox_account_retirement_tombstone")
