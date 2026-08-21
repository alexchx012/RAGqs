"""A/B vote idempotency columns and pair space isolation.

Revision ID: 0026_ab_vote_idempotency_space_isolation
Revises: 0023_ledger_secondary_indexes
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0026_ab_vote_idempotency_space_isolation"
down_revision: str | None = "0023_ledger_secondary_indexes"
branch_labels: tuple[str, ...] | Sequence[str] | None = None
depends_on: tuple[str, ...] | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("chat_ab_pair") as batch:
        batch.add_column(sa.Column("space_id", sa.String(length=64), nullable=False,
                                   server_default=""))
        batch.create_index("ix_chat_ab_pair_space", ["space_id"])
    with op.batch_alter_table("chat_ab_vote") as batch:
        batch.add_column(
            sa.Column("operation_kind", sa.String(length=32), nullable=False,
                      server_default="ab_vote")
        )
        batch.add_column(
            sa.Column("idempotency_key", sa.String(length=256), nullable=False,
                      server_default="")
        )
    # Legacy rows predate the idempotency column; pair_id is unique per voter,
    # so it keeps the new unique constraint satisfiable without merging votes.
    op.execute("UPDATE chat_ab_vote SET idempotency_key = pair_id WHERE idempotency_key = ''")
    with op.batch_alter_table("chat_ab_vote") as batch:
        batch.create_unique_constraint(
            "uq_chat_ab_vote_idempotency",
            ["voter_user_id", "operation_kind", "idempotency_key"],
        )


def downgrade() -> None:
    with op.batch_alter_table("chat_ab_vote") as batch:
        batch.drop_constraint("uq_chat_ab_vote_idempotency")
        batch.drop_column("idempotency_key")
        batch.drop_column("operation_kind")
    with op.batch_alter_table("chat_ab_pair") as batch:
        batch.drop_index("ix_chat_ab_pair_space")
        batch.drop_column("space_id")
