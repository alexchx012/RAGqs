"""Extend the schedule-occurrence outcome check with 'skipped_missed'.

Revision ID: 0034_backup_skipped_missed
Revises: 0033_backup_ops_layer

Q5: a recovering worker only re-runs the most recent missed schedule window;
earlier missed windows are recorded with outcome 'skipped_missed'. 0033 creates
its tables from the shared ``backup_metadata``, so fresh databases already carry
the extended constraint; the inspect guard keeps this a no-op there and only
rewrites the check on databases created before the extension.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0034_backup_skipped_missed"
down_revision: str | None = "0033_backup_ops_layer"
branch_labels: tuple[str, ...] | Sequence[str] | None = None
depends_on: tuple[str, ...] | Sequence[str] | None = None

_TABLE = "backup_schedule_occurrences"
_CONSTRAINT = "ck_backup_schedule_occurrences_outcome"
_EXTENDED = "outcome IN ('executed','skipped_active_backup','skipped_disabled','skipped_missed')"
_ORIGINAL = "outcome IN ('executed','skipped_active_backup','skipped_disabled')"


def _constraint_text(bind) -> str:
    inspector = sa.inspect(bind)
    for constraint in inspector.get_check_constraints(_TABLE):
        if constraint["name"] == _CONSTRAINT:
            return str(constraint.get("sqltext", ""))
    return ""


def upgrade() -> None:
    bind = op.get_bind()
    if "skipped_missed" in _constraint_text(bind):
        return
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_constraint(_CONSTRAINT, type_="check")
        batch.create_check_constraint(_CONSTRAINT, sa.text(_EXTENDED))


def downgrade() -> None:
    bind = op.get_bind()
    if "skipped_missed" not in _constraint_text(bind):
        return
    # Rows using the extended outcome must be removed before downgrading.
    op.execute(sa.text("DELETE FROM backup_schedule_occurrences WHERE outcome = 'skipped_missed'"))
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_constraint(_CONSTRAINT, type_="check")
        batch.create_check_constraint(_CONSTRAINT, sa.text(_ORIGINAL))
