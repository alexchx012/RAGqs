"""Create the greenfield retention & operations orchestration tables."""

from __future__ import annotations

from alembic import op
from app.retention.schema import retention_metadata

revision: str = "0018_retention_ops"
down_revision: str | None = "0017_evaluation_calibration"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    retention_metadata.create_all(op.get_bind())


def downgrade() -> None:
    retention_metadata.drop_all(op.get_bind())
