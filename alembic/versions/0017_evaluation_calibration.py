"""Create the greenfield evaluation & calibration domain tables."""

from __future__ import annotations

from alembic import op
from app.evaluation.schema import evaluation_metadata

revision: str = "0017_evaluation_calibration"
down_revision: str | None = "0016_chat_generation"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    evaluation_metadata.create_all(op.get_bind())


def downgrade() -> None:
    evaluation_metadata.drop_all(op.get_bind())
