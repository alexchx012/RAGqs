"""Remove the unused retention receipt retry ledger."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0019_simplify_retention_receipts"
down_revision: str | None = "0018_retention_ops"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    # 0018 creates from live metadata. Fresh installs already have the simplified shape.
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("retention_hook_receipts")
    }
    if "attempt_count" not in columns:
        return

    # `requested` was never produced by the live writers, but old rows using
    # it are still replayable. Normalize them to the remaining replayable state
    # before the replacement CHECK constraint is installed.
    op.execute(
        sa.text("UPDATE retention_hook_receipts SET state = 'accepted' WHERE state = 'requested'")
    )

    with op.batch_alter_table("retention_hook_receipts") as batch_op:
        batch_op.drop_constraint("ck_retention_hook_receipts_state", type_="check")
        batch_op.drop_constraint("ck_retention_hook_receipts_attempts", type_="check")
        batch_op.drop_index("ix_retention_receipts_kind_state")
        batch_op.drop_column("attempt_count")
        batch_op.create_check_constraint(
            "ck_retention_hook_receipts_state",
            "state IN ('accepted','blocked','completed','terminal','purged')",
        )


def downgrade() -> None:
    with op.batch_alter_table("retention_hook_receipts") as batch_op:
        batch_op.drop_constraint("ck_retention_hook_receipts_state", type_="check")
        batch_op.add_column(
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0"))
        )
        batch_op.create_check_constraint(
            "ck_retention_hook_receipts_state",
            "state IN ('requested','accepted','blocked','completed','terminal','purged')",
        )
        batch_op.create_check_constraint(
            "ck_retention_hook_receipts_attempts",
            "attempt_count >= 0",
        )
        batch_op.create_index("ix_retention_receipts_kind_state", ["kind", "state"], unique=False)
