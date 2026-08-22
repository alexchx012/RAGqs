"""A/B vote idempotency columns and pair space isolation.

Revision ID: 0026_ab_vote_idem_space
Revises: 0023_ledger_secondary_indexes
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0026_ab_vote_idem_space"
down_revision: str | None = "0023_ledger_secondary_indexes"
branch_labels: tuple[str, ...] | Sequence[str] | None = None
depends_on: tuple[str, ...] | Sequence[str] | None = None


def _columns(bind, table: str) -> set[str]:
    # 0012 用各域 metadata.create_all 建表：新装库的 chat 表已随 schema.py
    # 含这些列和约束，只有从旧版本升级的库需要补齐，因此本迁移必须幂等。
    inspector = sa.inspect(bind)
    return {column["name"] for column in inspector.get_columns(table)}


def _unique_constraints(bind, table: str) -> set[str]:
    inspector = sa.inspect(bind)
    return {
        constraint["name"]
        for constraint in inspector.get_unique_constraints(table)
        if constraint.get("name")
    }


def _indexes(bind, table: str) -> set[str]:
    inspector = sa.inspect(bind)
    return {index["name"] for index in inspector.get_indexes(table) if index.get("name")}


def upgrade() -> None:
    bind = op.get_bind()
    with op.batch_alter_table("chat_ab_pair") as batch:
        if "space_id" not in _columns(bind, "chat_ab_pair"):
            batch.add_column(
                sa.Column("space_id", sa.String(length=64), nullable=False, server_default="")
            )
    if "ix_chat_ab_pair_space" not in _indexes(bind, "chat_ab_pair"):
        op.create_index("ix_chat_ab_pair_space", "chat_ab_pair", ["space_id"])
    with op.batch_alter_table("chat_ab_vote") as batch:
        vote_columns = _columns(bind, "chat_ab_vote")
        if "operation_kind" not in vote_columns:
            batch.add_column(
                sa.Column(
                    "operation_kind", sa.String(length=32), nullable=False,
                    server_default="ab_vote",
                )
            )
        if "idempotency_key" not in vote_columns:
            batch.add_column(
                sa.Column(
                    "idempotency_key", sa.String(length=256), nullable=False, server_default=""
                )
            )
    # Legacy rows predate the idempotency column; pair_id is unique per voter,
    # so it keeps the unique constraint satisfiable without merging votes.
    op.execute("UPDATE chat_ab_vote SET idempotency_key = pair_id WHERE idempotency_key = ''")
    if "uq_chat_ab_vote_idempotency" not in _unique_constraints(bind, "chat_ab_vote"):
        with op.batch_alter_table("chat_ab_vote") as batch:
            batch.create_unique_constraint(
                "uq_chat_ab_vote_idempotency",
                ["voter_user_id", "operation_kind", "idempotency_key"],
            )


def downgrade() -> None:
    bind = op.get_bind()
    if "uq_chat_ab_vote_idempotency" in _unique_constraints(bind, "chat_ab_vote"):
        with op.batch_alter_table("chat_ab_vote") as batch:
            batch.drop_constraint("uq_chat_ab_vote_idempotency")
    with op.batch_alter_table("chat_ab_vote") as batch:
        vote_columns = _columns(bind, "chat_ab_vote")
        if "idempotency_key" in vote_columns:
            batch.drop_column("idempotency_key")
        if "operation_kind" in vote_columns:
            batch.drop_column("operation_kind")
    if "ix_chat_ab_pair_space" in _indexes(bind, "chat_ab_pair"):
        op.drop_index("ix_chat_ab_pair_space", table_name="chat_ab_pair")
    with op.batch_alter_table("chat_ab_pair") as batch:
        if "space_id" in _columns(bind, "chat_ab_pair"):
            batch.drop_column("space_id")
