"""I12: migration behavior — CHECK constraints reject invalid state domains and
negative sequence/watermark values; PostgreSQL triggers protect immutable
full-event artifacts (verified when RAGQS_TEST_POSTGRES_URL is configured)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.outbox.schema import (
    notification_delivery_receipt_table,
    notification_inbox_table,
    notification_suppression_table,
    notification_table,
    outbox_delivery_table,
)
from tests._support import build_engine, build_identity_service, fixed_now, provision_user

# Migration guard tests: the PostgreSQL trigger legs skip themselves when
# RAGQS_TEST_POSTGRES_URL is unset; the marker keeps `-m "not integration"`
# runs from paying for them at all.
pytestmark = pytest.mark.integration


def test_check_rejects_invalid_delivery_status() -> None:
    engine = build_engine()
    with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(
                outbox_delivery_table.insert().values(
                    event_id="evt_x",
                    consumer_name="in_app_notification",
                    status="bogus",
                    version=1,
                    replay_generation=1,
                    attempt_number=0,
                    cycle_attempt_number=0,
                    error_category=None,
                    error_code=None,
                    next_attempt_at_utc=None,
                    lease_owner=None,
                    lease_expires_at_utc=None,
                    fence_token=None,
                    delivered_at_utc=None,
                )
            )


def test_check_rejects_negative_attempt_number() -> None:
    engine = build_engine()
    with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(
                outbox_delivery_table.insert().values(
                    event_id="evt_x",
                    consumer_name="in_app_notification",
                    status="pending",
                    version=1,
                    replay_generation=1,
                    attempt_number=-1,
                    cycle_attempt_number=0,
                    error_category=None,
                    error_code=None,
                    next_attempt_at_utc=None,
                    lease_owner=None,
                    lease_expires_at_utc=None,
                    fence_token=None,
                    delivered_at_utc=None,
                )
            )


def test_check_rejects_negative_inbox_watermark() -> None:
    engine = build_engine()
    with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(
                notification_inbox_table.insert().values(
                    recipient_user_id="user_x",
                    next_notification_seq=1,
                    read_through_seq=-1,
                    read_all_at_utc=None,
                    version=1,
                    retired=False,
                )
            )


def test_check_rejects_zero_notification_seq() -> None:
    engine = build_engine()
    with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(
                notification_table.insert().values(
                    id="notification_x",
                    event_id="evt_x",
                    recipient_user_id="user_x",
                    notification_type="ingestion_completed",
                    title="t",
                    payload_json={},
                    event_occurred_at_utc=fixed_now(),
                    materialized_at_utc=fixed_now(),
                    notification_seq=0,
                    read_at_utc=None,
                    retire_after_at_utc=fixed_now(),
                    redacted=False,
                )
            )


def test_check_rejects_unknown_suppression_reason() -> None:
    engine = build_engine()
    with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(
                notification_suppression_table.insert().values(
                    event_id="evt_x",
                    recipient_user_id="user_x",
                    reason="bogus",
                    suppressed_at_utc=fixed_now(),
                )
            )


def test_check_rejects_unknown_receipt_outcome() -> None:
    engine = build_engine()
    with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.execute(
                notification_delivery_receipt_table.insert().values(
                    event_id="evt_x",
                    recipient_user_id="user_x",
                    outcome="bogus",
                    original_notification_seq=None,
                    occurred_at_utc=fixed_now(),
                    materialized_at_utc=None,
                    retired_at_utc=fixed_now(),
                    fingerprint="fp",
                )
            )


def _publish_immutable_event(engine, *, event_id: str, user_id: str) -> None:
    from app.outbox.ports import OutboxPublishCommand, RecipientSelection
    from tests._support import make_publisher

    publisher = make_publisher(engine)
    with engine.begin() as connection:
        publisher.publish(
            OutboxPublishCommand(
                event_id=event_id,
                caller_principal="ingestion",
                event_type="ingestion_completed",
                schema_version=1,
                aggregate_type="ingestion_job",
                aggregate_id=f"job_{event_id}",
                transition_version=1,
                occurred_at=fixed_now(),
                payload={
                    "job_id": f"job_{event_id}",
                    "document_id": f"doc_{event_id}",
                    "document_version_id": f"docv_{event_id}",
                    "publication_id": f"pub_{event_id}",
                },
                trace_id="t",
                recipients=(RecipientSelection(recipient_user_id=user_id),),
            ),
            connection=connection,
        )


def test_postgres_triggers_protect_full_event_artifacts_when_configured() -> None:
    import os
    import uuid

    if not os.environ.get("RAGQS_TEST_POSTGRES_URL"):
        pytest.skip("PostgreSQL integration environment is not configured")

    from sqlalchemy.exc import ProgrammingError

    from tests._support import pg_schema_context

    context = pg_schema_context()
    try:
        engine = context.engine
        identity = build_identity_service(engine)
        suffix = uuid.uuid4().hex[:8]
        alice = provision_user(identity, username=f"alice_{suffix}")
        event_id = f"evt_immutable_{suffix}"
        attempt_id = f"attempt_immutable_{suffix}"
        failed_attempt_id = f"attempt_failed_{suffix}"

        _publish_immutable_event(engine, event_id=event_id, user_id=alice)

        # 1. Full-event identity is immutable (occurred_at, trace_id, created_at).
        for column, value in (
            ("occurred_at_utc", "now()"),
            ("trace_id", "'other'"),
            ("created_at_utc", "now()"),
        ):
            with pytest.raises(ProgrammingError):
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            f"UPDATE outbox_event SET {column} = {value} " "WHERE event_id = :eid"
                        ),
                        {"eid": event_id},
                    )
        # 1b. A no-op update and a scheduling-field update on a full event are allowed.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE outbox_event SET occurred_at_utc = occurred_at_utc WHERE event_id = :eid"
                ),
                {"eid": event_id},
            )
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE outbox_event SET compact_after_at_utc = now() WHERE event_id = :eid"),
                {"eid": event_id},
            )
        # 1c. Recipient rows: UPDATE (even identity change) is rejected while full.
        with pytest.raises(ProgrammingError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE outbox_recipient SET selection_reason = 'other' WHERE event_id = :eid"
                    ),
                    {"eid": event_id},
                )
        # 1d. A no-op recipient update is allowed.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE outbox_recipient SET selection_reason = selection_reason "
                    "WHERE event_id = :eid"
                ),
                {"eid": event_id},
            )
        # 2. Recipient rows cannot be deleted while the event is full.
        with pytest.raises(ProgrammingError):
            with engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM outbox_recipient WHERE event_id = :eid"),
                    {"eid": event_id},
                )
        # 3. Attempt identity + started_at are immutable.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO outbox_delivery_attempt "
                    "(delivery_attempt_id, event_id, consumer_name, replay_generation, "
                    "attempt_number, cycle_attempt_number, fence_token, started_at_utc, status) "
                    "VALUES (:aid, :eid, 'in_app_notification', 1, 1, 1, 1, now(), 'running')"
                ),
                {"aid": attempt_id, "eid": event_id},
            )
        for column, value in (
            ("fence_token", "99"),
            ("started_at_utc", "now()"),
            ("attempt_number", "5"),
        ):
            with pytest.raises(ProgrammingError):
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            f"UPDATE outbox_delivery_attempt SET {column} = {value} "
                            "WHERE delivery_attempt_id = :aid"
                        ),
                        {"aid": attempt_id},
                    )
        # 3b. running -> running is not a legal terminal transition (a real change
        #     that leaves the status running must be rejected).
        with pytest.raises(ProgrammingError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE outbox_delivery_attempt SET status = 'running', "
                        "error_category = 'retryable' "
                        "WHERE delivery_attempt_id = :aid"
                    ),
                    {"aid": attempt_id},
                )
        # 3c. running -> delivered requires ended_at and no error summary.
        with pytest.raises(ProgrammingError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE outbox_delivery_attempt SET status = 'delivered' "
                        "WHERE delivery_attempt_id = :aid"
                    ),
                    {"aid": attempt_id},
                )
        with pytest.raises(ProgrammingError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE outbox_delivery_attempt SET status = 'delivered', ended_at_utc = now(), "
                        "error_category = 'retryable' "
                        "WHERE delivery_attempt_id = :aid"
                    ),
                    {"aid": attempt_id},
                )
        # 3c2. delivered with error_code only is also rejected (BOTH error fields
        #      must be NULL on a delivered attempt).
        with pytest.raises(ProgrammingError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE outbox_delivery_attempt SET status = 'delivered', ended_at_utc = now(), "
                        "error_code = 'boom' "
                        "WHERE delivery_attempt_id = :aid"
                    ),
                    {"aid": attempt_id},
                )
        # 3d. The legal delivered transition succeeds.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE outbox_delivery_attempt SET status = 'delivered', ended_at_utc = now() "
                    "WHERE delivery_attempt_id = :aid"
                ),
                {"aid": attempt_id},
            )
        # 3e. A terminal attempt is immutable: no state change, no re-run to running.
        with pytest.raises(ProgrammingError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE outbox_delivery_attempt SET status = 'running' WHERE delivery_attempt_id = :aid"
                    ),
                    {"aid": attempt_id},
                )
        with pytest.raises(ProgrammingError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE outbox_delivery_attempt SET error_code = 'x' WHERE delivery_attempt_id = :aid"
                    ),
                    {"aid": attempt_id},
                )
        # 3f. A no-op update on a terminal attempt is allowed.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE outbox_delivery_attempt SET ended_at_utc = ended_at_utc "
                    "WHERE delivery_attempt_id = :aid"
                ),
                {"aid": attempt_id},
            )
        # 3g. Attempts cannot be deleted while the event is full.
        with pytest.raises(ProgrammingError):
            with engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM outbox_delivery_attempt WHERE delivery_attempt_id = :aid"),
                    {"aid": attempt_id},
                )
        # 4. failed terminal transition requires an error summary.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO outbox_delivery_attempt "
                    "(delivery_attempt_id, event_id, consumer_name, replay_generation, "
                    "attempt_number, cycle_attempt_number, fence_token, started_at_utc, status) "
                    "VALUES (:aid, :eid, 'in_app_notification', 1, 2, 2, 2, now(), 'running')"
                ),
                {"aid": failed_attempt_id, "eid": event_id},
            )
        with pytest.raises(ProgrammingError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE outbox_delivery_attempt SET status = 'failed', ended_at_utc = now() "
                        "WHERE delivery_attempt_id = :aid"
                    ),
                    {"aid": failed_attempt_id},
                )
        # 4b. failed with only the category (no code) is an incomplete summary.
        with pytest.raises(ProgrammingError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE outbox_delivery_attempt SET status = 'failed', ended_at_utc = now(), "
                        "error_category = 'retryable' "
                        "WHERE delivery_attempt_id = :aid"
                    ),
                    {"aid": failed_attempt_id},
                )
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE outbox_delivery_attempt SET status = 'failed', ended_at_utc = now(), "
                    "error_category = 'retryable', error_code = 'boom' "
                    "WHERE delivery_attempt_id = :aid"
                ),
                {"aid": failed_attempt_id},
            )
        # 5. full -> compacted must clear full-only fields and record compacted_at.
        with pytest.raises(ProgrammingError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE outbox_event SET storage_state = 'compacted', compacted_at_utc = now() "
                        "WHERE event_id = :eid"
                    ),
                    {"eid": event_id},
                )
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE outbox_event SET storage_state = 'compacted', compacted_at_utc = now(), "
                    "payload_json = NULL, trace_id = NULL, schema_version = NULL "
                    "WHERE event_id = :eid"
                ),
                {"eid": event_id},
            )
        # 5b. After compaction the recipient/attempt rows may be deleted.
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM outbox_recipient WHERE event_id = :eid"), {"eid": event_id}
            )
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM outbox_delivery_attempt WHERE delivery_attempt_id = :aid"),
                {"aid": attempt_id},
            )
        # 5c. A compacted event is immutable: no reverse transition, no field change,
        #     and it may NEVER be deleted.
        with pytest.raises(ProgrammingError):
            with engine.begin() as connection:
                connection.execute(
                    text("UPDATE outbox_event SET storage_state = 'full' WHERE event_id = :eid"),
                    {"eid": event_id},
                )
        with pytest.raises(ProgrammingError):
            with engine.begin() as connection:
                connection.execute(
                    text("UPDATE outbox_event SET compacted_at_utc = now() WHERE event_id = :eid"),
                    {"eid": event_id},
                )
        with pytest.raises(ProgrammingError):
            with engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM outbox_event WHERE event_id = :eid"),
                    {"eid": event_id},
                )
        # 5d. A compacted event rejects ANY update (including a no-op): there is
        #     no json = json operator for whole-row comparisons, so compacted rows
        #     are simply frozen. A no-op update on a FULL event is still allowed.
        with pytest.raises(ProgrammingError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE outbox_event SET compacted_delivery_summary_json = compacted_delivery_summary_json "
                        "WHERE event_id = :eid"
                    ),
                    {"eid": event_id},
                )
    finally:
        context.close()


def test_pg_schema_context_cleans_up_created_schema_when_upgrade_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed migration must still drop the temporary schema it created."""
    import os

    if not os.environ.get("RAGQS_TEST_POSTGRES_URL"):
        pytest.skip("PostgreSQL integration environment is not configured")

    from alembic import command
    from tests._support import pg_schema_context, pg_test_schema_names

    before = pg_test_schema_names()

    def fail_upgrade(config, revision):
        del config, revision
        raise RuntimeError("migration exploded")

    monkeypatch.setattr(command, "upgrade", fail_upgrade)

    with pytest.raises(RuntimeError, match="migration exploded"):
        pg_schema_context()

    # The schema the failed context created must not be left behind.
    assert pg_test_schema_names() == before


def test_pg_schema_context_disposes_engine_before_dropping_schema() -> None:
    """close() must dispose the scoped engine BEFORE dropping the schema, so
    no pooled connection to the temporary schema outlives the test."""
    import os

    if not os.environ.get("RAGQS_TEST_POSTGRES_URL"):
        pytest.skip("PostgreSQL integration environment is not configured")

    from tests._support import pg_schema_context

    context = pg_schema_context()
    # Return a checked-out connection to the pool so dispose is observable.
    with context.engine.connect() as connection:
        connection.close()
    context.close()
    assert context.engine.pool.checkedin() == 0


def test_pg_test_schema_names_matches_only_literal_prefixes() -> None:
    """pg_test_schema_names() must match the LITERAL `outbox_it_`/`mig_`
    prefixes — a schema whose prefix differs in the underscore position (e.g.
    `outbox_itX...`) must never be reported as a temporary test schema."""
    import os
    import uuid

    if not os.environ.get("RAGQS_TEST_POSTGRES_URL"):
        pytest.skip("PostgreSQL integration environment is not configured")

    from sqlalchemy import create_engine, text

    from tests._support import pg_test_schema_names

    admin = create_engine(os.environ["RAGQS_TEST_POSTGRES_URL"])
    # The underscore position holds a hex character, so a `_` wildcard in a
    # LIKE pattern would (wrongly) match this decoy.
    decoy = f"outbox_it{uuid.uuid4().hex[:12]}"
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{decoy}"'))
        assert decoy not in pg_test_schema_names()
    finally:
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{decoy}" CASCADE'))
        admin.dispose()
