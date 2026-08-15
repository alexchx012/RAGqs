"""Merge usage-projection and retention-receipt migration heads.

Revision ID: 0020_merge_usage_retention
Revises: 0019_simplify_retention_receipts, 0019_usage_projection_bigint
"""

revision: str = "0020_merge_usage_retention"
down_revision: tuple[str, str] = (
    "0019_simplify_retention_receipts",
    "0019_usage_projection_bigint",
)
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
