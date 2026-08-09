"""Refresh PostgreSQL immutability triggers with JSON-safe comparisons and
strict attempt terminal summaries.

0005 originally generated `NEW.payload_json IS DISTINCT FROM OLD.payload_json`
and a whole-row `NEW IS NOT DISTINCT FROM OLD` no-op allowance on compacted
events. PostgreSQL json has no `=` operator, so those guards fail at runtime
with `operator does not exist: json = json` on any UPDATE of a row that
carries a JSON column, and a whole-row comparison of a compacted event row
fails the same way. This revision re-runs the corrected bodies (loaded from
the 0005 helpers) on every database, fresh or already upgraded:

- payload_json is compared via `::text` (canonical textual comparison);
- compacted event rows reject ANY update (no whole-row comparison);
- delivered attempts require BOTH error fields NULL; failed/expired attempts
  require BOTH error fields set.

Revision ID: 0009_refresh_immutable_triggers
Revises: 0008_refresh_immutable_triggers
"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0009_refresh_immutable_triggers"
down_revision: str | None = "0008_refresh_immutable_triggers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HELPERS_PATH = Path(__file__).parent / "0005_outbox_immutable_triggers.py"


def _load_0005_helpers():
    spec = importlib.util.spec_from_file_location("m0005_helpers", _HELPERS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    helpers = _load_0005_helpers()
    op.execute(f"""
        CREATE OR REPLACE FUNCTION trg_fn_outbox_event_immutable() RETURNS trigger AS $$
        BEGIN
        {helpers._event_guard_body(helpers._IMMUTABLE_EVENT_COLUMNS)}
        END;
        $$ LANGUAGE plpgsql;
        """)
    op.execute(f"""
        CREATE OR REPLACE FUNCTION trg_fn_outbox_attempt_immutable() RETURNS trigger AS $$
        BEGIN
        {helpers._attempt_guard_body(helpers._ATTEMPT_IMMUTABLE_COLUMNS)}
        END;
        $$ LANGUAGE plpgsql;
        """)


def downgrade() -> None:
    # No downgrade: the trigger functions are replaced, not dropped.
    return
