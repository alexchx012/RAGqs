"""Drop the unimplemented chat provider reconciliation placeholder.

``provider_reconciling`` only ever existed as a status enum value in the
execution CHECK constraint, and ``provider_reconciliation_state`` columns were
written as NULL and read by nobody: no worker transition, lease rule, or read
model implements provider reconciliation for chat generations. Keeping the
placeholder invites callers to report a reconciliation state that does not
exist, so this revision removes both columns and tightens the status
constraint. Databases that already migrated get the cleanup here; fresh
schemas build the new shape directly from ``app.chat.schema`` (0016 creates
chat tables from the live metadata), so every step is guarded on presence.

Revision ID: 0024_chat_reconcile_drop
Revises: 0023_ledger_secondary_indexes
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0024_chat_reconcile_drop"
down_revision: str | None = "0023_ledger_secondary_indexes"
branch_labels: tuple[str, ...] | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUS_CONSTRAINT = "ck_chat_generation_execution_status"
_PLACEHOLDER_COLUMN = "provider_reconciliation_state"
_NEW_STATUS_ENUM = (
    "status IN ('queued','running','retry_wait','expired','completed','failed','cancelled')"
)
_OLD_STATUS_ENUM = (
    "status IN ('queued','running','retry_wait','provider_reconciling','expired',"
    "'completed','failed','cancelled')"
)


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return column in {item["name"] for item in inspector.get_columns(table)}


def upgrade() -> None:
    if _has_column("chat_generation", _PLACEHOLDER_COLUMN):
        with op.batch_alter_table("chat_generation") as batch_op:
            batch_op.drop_column(_PLACEHOLDER_COLUMN)
    with op.batch_alter_table("chat_generation_execution") as batch_op:
        if _has_column("chat_generation_execution", _PLACEHOLDER_COLUMN):
            batch_op.drop_column(_PLACEHOLDER_COLUMN)
        batch_op.drop_constraint(_STATUS_CONSTRAINT, type_="check")
        batch_op.create_check_constraint(_STATUS_CONSTRAINT, _NEW_STATUS_ENUM)


def downgrade() -> None:
    with op.batch_alter_table("chat_generation_execution") as batch_op:
        batch_op.drop_constraint(_STATUS_CONSTRAINT, type_="check")
        batch_op.create_check_constraint(_STATUS_CONSTRAINT, _OLD_STATUS_ENUM)
        batch_op.add_column(sa.Column(_PLACEHOLDER_COLUMN, sa.String(32), nullable=True))
    with op.batch_alter_table("chat_generation") as batch_op:
        batch_op.add_column(sa.Column(_PLACEHOLDER_COLUMN, sa.String(32), nullable=True))
