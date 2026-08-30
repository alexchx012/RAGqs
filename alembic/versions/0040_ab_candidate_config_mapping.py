"""Persist blind A/B candidate configuration mappings and vote provenance."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0040_ab_candidate_config_mapping"
down_revision: str | None = "0039_merge_chat_recovery_and_release_gate"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def _columns(bind: sa.Connection, table: str) -> set[str]:
    return {str(column["name"]) for column in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if "candidate_config_version" not in _columns(bind, "chat_ab_candidate"):
        with op.batch_alter_table("chat_ab_candidate") as batch:
            batch.add_column(sa.Column("candidate_config_version", sa.String(length=64)))
    seed_columns = _columns(bind, "evaluation_ab_golden_seed")
    with op.batch_alter_table("evaluation_ab_golden_seed") as batch:
        if "preferred_candidate_config_version" not in seed_columns:
            batch.add_column(sa.Column("preferred_candidate_config_version", sa.String(length=64)))
        if "rejected_candidate_config_version" not in seed_columns:
            batch.add_column(sa.Column("rejected_candidate_config_version", sa.String(length=64)))


def downgrade() -> None:
    bind = op.get_bind()
    seed_columns = _columns(bind, "evaluation_ab_golden_seed")
    with op.batch_alter_table("evaluation_ab_golden_seed") as batch:
        if "rejected_candidate_config_version" in seed_columns:
            batch.drop_column("rejected_candidate_config_version")
        if "preferred_candidate_config_version" in seed_columns:
            batch.drop_column("preferred_candidate_config_version")
    if "candidate_config_version" in _columns(bind, "chat_ab_candidate"):
        with op.batch_alter_table("chat_ab_candidate") as batch:
            batch.drop_column("candidate_config_version")
