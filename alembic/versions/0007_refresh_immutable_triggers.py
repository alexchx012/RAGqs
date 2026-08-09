"""Refresh PostgreSQL immutability triggers.

Migration 0005 originally emitted triggers that returned NULL on allowed
operations, silently skipping UPDATE/DELETE. It was fixed in place, but an
existing head database still carries the old function bodies. This migration
re-runs CREATE OR REPLACE so the corrected trigger functions (which RETURN
NEW/OLD on allowed operations) are installed on every database, fresh or
already upgraded.

Revision ID: 0007_refresh_immutable_triggers
Revises: 0006_outbox_retirement_tombstone
"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0007_refresh_immutable_triggers"
down_revision: str | None = "0006_outbox_retirement_tombstone"
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
