"""Persist quota settlement, generation ownership, and replay facts.

Revision ID: 0024_fix_quota_transaction
Revises: 0023_ledger_secondary_indexes
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0024_fix_quota_transaction"
down_revision: str | None = "0023_ledger_secondary_indexes"
branch_labels: tuple[str, ...] | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _add_column_if_absent(table_name: str, column: sa.Column) -> None:
    columns = {
        column_info["name"] for column_info in sa.inspect(op.get_bind()).get_columns(table_name)
    }
    if column.name not in columns:
        op.add_column(table_name, column)


def upgrade() -> None:
    _add_column_if_absent(
        "provider_call",
        sa.Column("replay_generation", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    _add_column_if_absent(
        "usage_event",
        sa.Column("replay_generation", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    _add_column_if_absent(
        "ingestion_jobs",
        sa.Column(
            "quota_charge_status", sa.String(length=16), nullable=False, server_default="pending"
        ),
    )
    _add_column_if_absent(
        "ingestion_jobs",
        sa.Column("quota_charge_reason", sa.String(length=64), nullable=True),
    )
    _add_column_if_absent(
        "publications",
        sa.Column(
            "quota_charge_status", sa.String(length=16), nullable=False, server_default="pending"
        ),
    )
    _add_column_if_absent(
        "publications",
        sa.Column("quota_charge_reason", sa.String(length=64), nullable=True),
    )
    _add_column_if_absent("chat_generation", sa.Column("actor_role_snapshot", sa.String(length=32)))
    _add_column_if_absent(
        "chat_generation", sa.Column("actor_department_id_snapshot", sa.String(length=64))
    )
    _add_column_if_absent(
        "chat_generation", sa.Column("quota_subject_user_id", sa.String(length=64))
    )
    _add_column_if_absent("chat_generation", sa.Column("cost_center_key", sa.String(length=128)))
    _add_column_if_absent("chat_generation", sa.Column("source_space_ids_json", sa.JSON()))


def downgrade() -> None:
    op.drop_column("chat_generation", "source_space_ids_json")
    op.drop_column("chat_generation", "cost_center_key")
    op.drop_column("chat_generation", "quota_subject_user_id")
    op.drop_column("chat_generation", "actor_department_id_snapshot")
    op.drop_column("chat_generation", "actor_role_snapshot")
    op.drop_column("publications", "quota_charge_reason")
    op.drop_column("publications", "quota_charge_status")
    op.drop_column("ingestion_jobs", "quota_charge_reason")
    op.drop_column("ingestion_jobs", "quota_charge_status")
    op.drop_column("usage_event", "replay_generation")
    op.drop_column("provider_call", "replay_generation")
