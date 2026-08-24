"""Create the backup/restore orchestration tables."""

from __future__ import annotations

from alembic import op
from app.backup.schema import backup_metadata

revision: str = "0032_backup_restore_ops"
down_revision: str | None = "0031_public_graph_active_facts"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    backup_metadata.create_all(op.get_bind())


def downgrade() -> None:
    backup_metadata.drop_all(op.get_bind())
