"""Persist active public graph entity and relation facts."""

from __future__ import annotations

from alembic import op
from app.graph.schema import graph_entities_table, graph_relations_table

revision: str = "0031_public_graph_active_facts"
down_revision: str | None = "0030_usage_meter_budget"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    graph_entities_table.create(op.get_bind(), checkfirst=True)
    graph_relations_table.create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    graph_relations_table.drop(op.get_bind(), checkfirst=True)
    graph_entities_table.drop(op.get_bind(), checkfirst=True)
