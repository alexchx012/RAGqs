"""Create the A/B preference-pair golden seed pool table.

Revision ID: 0035_ab_golden_seed
Revises: 0034_backup_skipped_missed

Voted A/B pairs become candidate seeds for the deployment-side
``publish_golden_set`` flow (后端设计 §8.6). The shared ``evaluation_metadata``
already carries the new table for fresh databases, so ``create_all`` only
creates it where it is missing.
"""

from __future__ import annotations

from alembic import op
from app.evaluation.schema import evaluation_ab_golden_seed_table, evaluation_metadata

revision: str = "0035_ab_golden_seed"
down_revision: str | None = "0034_backup_skipped_missed"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    evaluation_metadata.create_all(op.get_bind(), tables=[evaluation_ab_golden_seed_table])


def downgrade() -> None:
    evaluation_ab_golden_seed_table.drop(op.get_bind())
