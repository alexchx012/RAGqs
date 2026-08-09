"""PostgreSQL trigger guards for immutable outbox artifacts.

For full events, the event identity/fingerprint/aggregate/occurred-time,
payload and recipient rows are immutable; delivery attempts are append-only
except for their controlled terminal-state updates. Triggers enforce this at
the database level while ALLOWED operations still return NEW/OLD (a BEFORE
trigger returning NULL would silently skip the row).

- outbox_event: full->full updates may touch scheduling/status fields but
  never the immutable identity; the only storage transition is full->compacted
  (controlled compaction clears full-only fields); the reverse transition and
  identity rewrites are rejected; deletion of a full event is rejected.
- outbox_recipient: UPDATE rejected; DELETE allowed only once the parent event
  is compacted (controlled compaction).
- outbox_delivery_attempt: identity + fence immutable; controlled terminal
  updates (status/error/ended_at) allowed; DELETE allowed only once the parent
  event is compacted.

Other dialects rely on application invariants plus CHECK constraints.

Revision ID: 0005_outbox_immutable_triggers
Revises: 0004_identity_archive_columns
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_outbox_immutable_triggers"
down_revision: str | None = "0004_identity_archive_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_IMMUTABLE_EVENT_COLUMNS = (
    "event_id",
    "event_type",
    "schema_version",
    "aggregate_type",
    "aggregate_id",
    "transition_version",
    "occurred_at_utc",
    "payload_fingerprint",
    "payload_json",
    "trace_id",
    "created_at_utc",
)

# Attempt identity + replay/attempt/fence counters and the started-at fact are
# immutable; status/error/ended_at are the controlled terminal-state fields
# the dispatcher may update.
_ATTEMPT_IMMUTABLE_COLUMNS = (
    "delivery_attempt_id",
    "event_id",
    "consumer_name",
    "replay_generation",
    "attempt_number",
    "cycle_attempt_number",
    "fence_token",
    "started_at_utc",
)


def _event_guard_body(columns: tuple[str, ...]) -> str:
    checks = "\n            ".join(
        (
            f"IF NEW.{column} IS DISTINCT FROM OLD.{column} THEN "
            f"RAISE EXCEPTION 'immutable column % on outbox_event', '{column}'; END IF;"
            if column != "payload_json"
            else (
                "IF NEW.payload_json::text IS DISTINCT FROM OLD.payload_json::text THEN "
                "RAISE EXCEPTION 'immutable column payload_json on outbox_event'; END IF;"
            )
        )
        for column in columns
    )
    return f"""
    IF TG_OP = 'UPDATE' THEN
        IF OLD.storage_state = 'full' AND NEW.storage_state = 'full' THEN
            -- full -> full: identity, schema, fingerprint, payload, trace and
            -- created-at are immutable; scheduling fields stay mutable. JSON
            -- columns are compared via ::text because json has no = operator.
            -- Full events must never carry compacted facts: compacted_at_utc
            -- and compacted_delivery_summary_json stay NULL until the single
            -- full -> compacted transition.
            {checks}
            IF NEW.compacted_at_utc IS NOT NULL THEN
                RAISE EXCEPTION 'full outbox_event rows must keep compacted_at_utc NULL';
            END IF;
            IF NEW.compacted_delivery_summary_json IS NOT NULL THEN
                RAISE EXCEPTION 'full outbox_event rows must keep compacted_delivery_summary_json NULL';
            END IF;
        ELSIF OLD.storage_state = 'full' AND NEW.storage_state = 'compacted' THEN
            -- Controlled compaction: permanent identity/aggregate/type/
            -- transition/fingerprint/occurred/created stay; full-only fields
            -- clear; storage transitions to compacted exactly once.
            IF NEW.event_id IS DISTINCT FROM OLD.event_id THEN
                RAISE EXCEPTION 'immutable column event_id on outbox_event';
            END IF;
            IF NEW.event_type IS DISTINCT FROM OLD.event_type THEN
                RAISE EXCEPTION 'immutable column event_type on outbox_event';
            END IF;
            IF NEW.aggregate_type IS DISTINCT FROM OLD.aggregate_type THEN
                RAISE EXCEPTION 'immutable column aggregate_type on outbox_event';
            END IF;
            IF NEW.aggregate_id IS DISTINCT FROM OLD.aggregate_id THEN
                RAISE EXCEPTION 'immutable column aggregate_id on outbox_event';
            END IF;
            IF NEW.transition_version IS DISTINCT FROM OLD.transition_version THEN
                RAISE EXCEPTION 'immutable column transition_version on outbox_event';
            END IF;
            IF NEW.occurred_at_utc IS DISTINCT FROM OLD.occurred_at_utc THEN
                RAISE EXCEPTION 'immutable column occurred_at_utc on outbox_event';
            END IF;
            IF NEW.payload_fingerprint IS DISTINCT FROM OLD.payload_fingerprint THEN
                RAISE EXCEPTION 'immutable column payload_fingerprint on outbox_event';
            END IF;
            IF NEW.created_at_utc IS DISTINCT FROM OLD.created_at_utc THEN
                RAISE EXCEPTION 'immutable column created_at_utc on outbox_event';
            END IF;
            IF NEW.payload_json IS NOT NULL OR NEW.trace_id IS NOT NULL OR NEW.schema_version IS NOT NULL THEN
                RAISE EXCEPTION 'compacted events must clear full-only payload/trace/schema';
            END IF;
            IF NEW.compacted_at_utc IS NULL THEN
                RAISE EXCEPTION 'compacted events must record compacted_at_utc';
            END IF;
        ELSE
            -- Compacted rows (and any reverse transition) are immutable:
            -- ANY update is rejected. No whole-row comparison is used here
            -- because a row with json columns has no json = json operator.
            RAISE EXCEPTION 'outbox_event storage may only transition full -> compacted once';
        END IF;
    ELSIF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'outbox_event rows may never be deleted; compaction is the only removal path';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        RETURN NEW;
    END IF;
    RETURN OLD;
    """


def _recipient_guard_body() -> str:
    return """
    IF TG_OP = 'UPDATE' THEN
        IF NEW IS NOT DISTINCT FROM OLD THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'outbox_recipient rows are immutable';
    END IF;
    IF TG_OP = 'DELETE' AND NOT EXISTS (
        SELECT 1 FROM outbox_event
        WHERE event_id = OLD.event_id AND storage_state = 'compacted'
    ) THEN
        RAISE EXCEPTION 'outbox_recipient rows may only be removed after their event is compacted';
    END IF;
    RETURN OLD;
    """


def _attempt_guard_body(columns: tuple[str, ...]) -> str:
    checks = "\n            ".join(
        f"IF NEW.{column} IS DISTINCT FROM OLD.{column} THEN "
        f"RAISE EXCEPTION 'immutable column % on outbox_delivery_attempt', '{column}'; END IF;"
        for column in columns
    )
    return f"""
    IF TG_OP = 'UPDATE' THEN
        IF OLD.status = 'running' THEN
            -- A no-op refresh of a running attempt is harmless.
            IF NEW IS NOT DISTINCT FROM OLD THEN
                RETURN NEW;
            END IF;
            {checks}
            IF NEW.status NOT IN ('delivered', 'failed', 'expired') THEN
                RAISE EXCEPTION 'running attempts may only end in delivered, failed or expired';
            END IF;
            IF NEW.ended_at_utc IS NULL THEN
                RAISE EXCEPTION 'terminal attempts must record ended_at_utc';
            END IF;
            IF NEW.status = 'delivered' AND (NEW.error_category IS NOT NULL OR NEW.error_code IS NOT NULL) THEN
                RAISE EXCEPTION 'delivered attempts must not carry any error summary';
            END IF;
            IF NEW.status IN ('failed', 'expired') AND (NEW.error_category IS NULL OR NEW.error_code IS NULL) THEN
                RAISE EXCEPTION 'failed/expired attempts must carry a complete error summary';
            END IF;
            RETURN NEW;
        END IF;
        -- Terminal attempts are immutable; only a no-op update is permitted.
        IF NEW IS NOT DISTINCT FROM OLD THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'terminal outbox_delivery_attempt rows are immutable';
    END IF;
    IF TG_OP = 'DELETE' AND NOT EXISTS (
        SELECT 1 FROM outbox_event
        WHERE event_id = OLD.event_id AND storage_state = 'compacted'
    ) THEN
        RAISE EXCEPTION 'outbox_delivery_attempt rows may only be removed after their event is compacted';
    END IF;
    RETURN OLD;
    """


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(f"""
        CREATE OR REPLACE FUNCTION trg_fn_outbox_event_immutable() RETURNS trigger AS $$
        BEGIN
        {_event_guard_body(_IMMUTABLE_EVENT_COLUMNS)}
        END;
        $$ LANGUAGE plpgsql;
        """)
    op.execute(
        "CREATE TRIGGER trg_outbox_event_immutable "
        "BEFORE UPDATE OR DELETE ON outbox_event "
        "FOR EACH ROW EXECUTE FUNCTION trg_fn_outbox_event_immutable();"
    )
    op.execute(f"""
        CREATE OR REPLACE FUNCTION trg_fn_outbox_recipient_immutable() RETURNS trigger AS $$
        BEGIN
        {_recipient_guard_body()}
        END;
        $$ LANGUAGE plpgsql;
        """)
    op.execute(
        "CREATE TRIGGER trg_outbox_recipient_immutable "
        "BEFORE UPDATE OR DELETE ON outbox_recipient "
        "FOR EACH ROW EXECUTE FUNCTION trg_fn_outbox_recipient_immutable();"
    )
    op.execute(f"""
        CREATE OR REPLACE FUNCTION trg_fn_outbox_attempt_immutable() RETURNS trigger AS $$
        BEGIN
        {_attempt_guard_body(_ATTEMPT_IMMUTABLE_COLUMNS)}
        END;
        $$ LANGUAGE plpgsql;
        """)
    op.execute(
        "CREATE TRIGGER trg_outbox_attempt_immutable "
        "BEFORE UPDATE OR DELETE ON outbox_delivery_attempt "
        "FOR EACH ROW EXECUTE FUNCTION trg_fn_outbox_attempt_immutable();"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("DROP TRIGGER IF EXISTS trg_outbox_attempt_immutable ON outbox_delivery_attempt")
    op.execute("DROP FUNCTION IF EXISTS trg_fn_outbox_attempt_immutable()")
    op.execute("DROP TRIGGER IF EXISTS trg_outbox_recipient_immutable ON outbox_recipient")
    op.execute("DROP FUNCTION IF EXISTS trg_fn_outbox_recipient_immutable()")
    op.execute("DROP TRIGGER IF EXISTS trg_outbox_event_immutable ON outbox_event")
    op.execute("DROP FUNCTION IF EXISTS trg_fn_outbox_event_immutable()")
