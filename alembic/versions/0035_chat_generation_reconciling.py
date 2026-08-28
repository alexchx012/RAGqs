"""Allow chat executions to remain pending while a provider call is reconciled.

The provider-call ledger keeps its existing five-state contract.  This change
only extends the chat execution lifecycle with the recovery projection used by
the generation worker.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0035_chat_generation_reconciling"
down_revision: str | None = "0034_backup_skipped_missed"
branch_labels: tuple[str, ...] | Sequence[str] | None = None
depends_on: tuple[str, ...] | Sequence[str] | None = None

_TABLE = "chat_generation_execution"
_CONSTRAINT = "ck_chat_generation_execution_status"
_EXTENDED = (
    "status IN ('queued','running','retry_wait','expired','provider_reconciling',"
    "'completed','failed','cancelled')"
)
_ORIGINAL = "status IN ('queued','running','retry_wait','expired','completed','failed','cancelled')"


def _constraint_text(bind) -> str:
    inspector = sa.inspect(bind)
    for constraint in inspector.get_check_constraints(_TABLE):
        if constraint["name"] == _CONSTRAINT:
            return str(constraint.get("sqltext", ""))
    return ""


def upgrade() -> None:
    bind = op.get_bind()
    if "provider_reconciling" in _constraint_text(bind):
        return
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_constraint(_CONSTRAINT, type_="check")
        batch.create_check_constraint(_CONSTRAINT, sa.text(_EXTENDED))


def downgrade() -> None:
    bind = op.get_bind()
    if "provider_reconciling" not in _constraint_text(bind):
        return
    op.execute(
        sa.text(
            "UPDATE chat_generation_execution "
            "SET status = 'failed' WHERE status = 'provider_reconciling'"
        )
    )
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_constraint(_CONSTRAINT, type_="check")
        batch.create_check_constraint(_CONSTRAINT, sa.text(_ORIGINAL))
