"""Merge ab-vote idempotency and quota-ingestion-cancel migration heads.

Revision ID: 0027_merge_ab_vote_idempotency
Revises: 0025_merge_quota_ingest_cancel, 0026_ab_vote_idem_space
"""

revision: str = "0027_merge_ab_vote_idempotency"
down_revision: tuple[str, str] = (
    "0025_merge_quota_ingest_cancel",
    "0026_ab_vote_idem_space",
)
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
