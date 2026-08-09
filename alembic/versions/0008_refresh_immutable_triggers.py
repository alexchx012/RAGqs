"""Refresh PostgreSQL immutability triggers with the complete invariant set.

Migration 0005 originally emitted incomplete guards: the full->full update
path protected only a subset of the immutable event columns (trace_id and
created_at_utc were missing), full->compacted did not protect every permanent
identity fact, compacted rows were not locked against modification, event
DELETE was still allowed once compacted, attempt identity did not include
started_at_utc, and the attempt terminal-state machine was not enforced. 0007
re-ran the then-current 0005 bodies, so databases already at 0007 still carry
the old guards. This revision re-runs the corrected bodies (loaded from the
0005 helpers) on every database, fresh or already upgraded.

Revision ID: 0008_refresh_immutable_triggers
Revises: 0007_refresh_immutable_triggers
"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0008_refresh_immutable_triggers"
down_revision: str | None = "0007_refresh_immutable_triggers"
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
        CREATE OR REPLACE FUNCTION trg_fn_outbox_recipient_immutable() RETURNS trigger AS $$
        BEGIN
        {helpers._recipient_guard_body()}
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
