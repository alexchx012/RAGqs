"""Add the compacted-fields-on-full-events CHECK constraint and refresh the
immutability triggers.

Full `outbox_event` rows must never carry compacted facts: `compacted_at_utc`
and `compacted_delivery_summary_json` stay NULL until the single
full -> compacted transition. This revision:

- adds the database CHECK constraint `ck_outbox_event_compacted_fields_full_null`
  on already-upgraded PostgreSQL databases (fresh schemas get it from 0003);
- re-runs the corrected event trigger body (loaded from the 0005 helpers) so
  the full -> full branch explicitly rejects non-NULL compacted fields with a
  clear error even before the constraint fires.

Revision ID: 0010_compacted_fields_check
Revises: 0009_refresh_immutable_triggers
"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0010_compacted_fields_check"
down_revision: str | None = "0009_refresh_immutable_triggers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HELPERS_PATH = Path(__file__).parent / "0005_outbox_immutable_triggers.py"

_CHECK_NAME = "ck_outbox_event_compacted_fields_full_null"


def _load_0005_helpers():
    spec = importlib.util.spec_from_file_location("m0005_helpers", _HELPERS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(f"""
            ALTER TABLE outbox_event DROP CONSTRAINT IF EXISTS {_CHECK_NAME}
            """)
        op.execute(f"""
            ALTER TABLE outbox_event ADD CONSTRAINT {_CHECK_NAME} CHECK (
                storage_state = 'compacted'
                OR (compacted_at_utc IS NULL AND compacted_delivery_summary_json IS NULL)
            )
            """)
        helpers = _load_0005_helpers()
        op.execute(f"""
            CREATE OR REPLACE FUNCTION trg_fn_outbox_event_immutable() RETURNS trigger AS $$
            BEGIN
            {helpers._event_guard_body(helpers._IMMUTABLE_EVENT_COLUMNS)}
            END;
            $$ LANGUAGE plpgsql;
            """)
        return
    if bind.dialect.name == "sqlite":
        # SQLite cannot ALTER TABLE ... ADD CONSTRAINT: rebuild the table via
        # batch_alter_table. Fresh schemas already carry the constraint from
        # 0003, so drop it first (when present) to avoid a duplicate.
        import sqlalchemy as sa

        inspector = sa.inspect(bind)
        has_check = any(
            c["name"] == _CHECK_NAME for c in inspector.get_check_constraints("outbox_event")
        )
        with op.batch_alter_table("outbox_event") as batch_op:
            if has_check:
                batch_op.drop_constraint(_CHECK_NAME, type_="check")
            batch_op.create_check_constraint(
                _CHECK_NAME,
                "storage_state = 'compacted' OR "
                "(compacted_at_utc IS NULL AND compacted_delivery_summary_json IS NULL)",
            )
        return


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(f"ALTER TABLE outbox_event DROP CONSTRAINT IF EXISTS {_CHECK_NAME}")
        return
    if bind.dialect.name == "sqlite":
        import sqlalchemy as sa

        inspector = sa.inspect(bind)
        has_check = any(
            c["name"] == _CHECK_NAME for c in inspector.get_check_constraints("outbox_event")
        )
        if has_check:
            with op.batch_alter_table("outbox_event") as batch_op:
                batch_op.drop_constraint(_CHECK_NAME, type_="check")
