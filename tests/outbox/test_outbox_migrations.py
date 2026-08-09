"""Migration contract for outbox-owned tables."""

from __future__ import annotations

from pathlib import Path

import pytest
from _helpers import alembic_config
from sqlalchemy import create_engine, inspect, text

from alembic import command
from app.outbox.schema import OUTBOX_TABLE_NAMES


def test_alembic_config_round_trips_percent_encoded_scoped_postgres_url() -> None:
    """Config.set_main_option must accept a percent-encoded, schema-scoped
    PostgreSQL URL, and the option must read back as the original
    single-percent URL.

    The `options=-c%20search_path%3D...` query percent-encodes characters;
    ConfigParser treats a bare `%` as an interpolation marker and previously
    raised `ValueError: invalid interpolation syntax` when the option was
    read (the exact failure the real-PostgreSQL outbox tests hit).
    """
    url = (
        "postgresql+psycopg://ragqs:ragqs@localhost:5432/ragqs"
        "?options=-c%20search_path%3Doutbox_it_regression"
    )
    config = alembic_config(url)
    assert config.get_main_option("sqlalchemy.url") == url


def test_head_upgrade_creates_outbox_owned_tables(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'outbox.sqlite3'}"
    config = alembic_config(database_url)

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    tables = set(inspect(engine).get_table_names())
    engine.dispose()

    assert OUTBOX_TABLE_NAMES <= tables


def test_outbox_revision_can_downgrade_to_identity_base(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'outbox-downgrade.sqlite3'}"
    config = alembic_config(database_url)

    command.upgrade(config, "head")
    command.downgrade(config, "0002_identity_access")

    engine = create_engine(database_url)
    tables = set(inspect(engine).get_table_names())
    engine.dispose()

    assert not (OUTBOX_TABLE_NAMES & tables)


def test_sqlite_alembic_head_has_the_compacted_fields_check(tmp_path: Path) -> None:
    """The Alembic-migrated SQLite schema (not just metadata create_all) must
    carry `ck_outbox_event_compacted_fields_full_null` and reject illegal full
    events that carry compacted facts."""
    from sqlalchemy.exc import IntegrityError

    database_url = f"sqlite:///{tmp_path / 'outbox-check.sqlite3'}"
    config = alembic_config(database_url)

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    try:
        checks = inspect(engine).get_check_constraints("outbox_event")
        names = {c["name"] for c in checks}
        assert "ck_outbox_event_compacted_fields_full_null" in names

        # A legal full event (compacted facts NULL) inserts fine.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO outbox_event "
                    "(event_id, event_type, schema_version, aggregate_type, aggregate_id, "
                    "transition_version, occurred_at_utc, payload_json, payload_fingerprint, "
                    "trace_id, created_at_utc, storage_state, compact_after_at_utc, "
                    "compacted_at_utc, compacted_delivery_summary_json) "
                    "VALUES (:event_id, 'ingestion_completed', 1, 'ingestion_job', 'job_a', 1, "
                    "'2026-01-01 00:00:00', '{}', 'fp-a', 't', '2026-01-01 00:00:00', 'full', "
                    "NULL, NULL, NULL)"
                ),
                {"event_id": "evt_legal"},
            )
        # A full event carrying compacted_at_utc violates the CHECK.
        with pytest.raises(IntegrityError) as raised:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO outbox_event "
                        "(event_id, event_type, schema_version, aggregate_type, aggregate_id, "
                        "transition_version, occurred_at_utc, payload_json, payload_fingerprint, "
                        "trace_id, created_at_utc, storage_state, compact_after_at_utc, "
                        "compacted_at_utc, compacted_delivery_summary_json) "
                        "VALUES (:event_id, 'ingestion_completed', 1, 'ingestion_job', 'job_b', 1, "
                        "'2026-01-01 00:00:00', '{}', 'fp-b', 't', '2026-01-01 00:00:00', 'full', "
                        "NULL, '2026-02-01 00:00:00', NULL)"
                    ),
                    {"event_id": "evt_illegal"},
                )
        # The failure is the CHECK constraint, not something else.
        assert "ck_outbox_event_compacted_fields_full_null" in str(raised.value.orig)
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT COUNT(*) FROM outbox_event WHERE event_id = 'evt_illegal'")
                ).scalar_one()
                == 0
            )
    finally:
        engine.dispose()


def _pg_url() -> str | None:
    import os

    return os.environ.get("RAGQS_TEST_POSTGRES_URL")


def _scoped_url(url: str, schema: str) -> str:
    from urllib.parse import quote

    sep = "&" if "?" in url else "?"
    return f"{url}{sep}options=-c%20search_path%3D{quote(schema)}"


def _pg_trigger_installed(engine) -> bool:
    """True when the immutable-event trigger exists for outbox_event in the
    CURRENT schema.

    pg_trigger is a database-wide catalog; earlier test schemas can carry the
    same trigger name, so the lookup must be scoped to the table resolved
    through the connection's search_path, never bare tgname.
    """
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT 1 FROM pg_trigger "
                "WHERE tgname = 'trg_outbox_event_immutable' "
                "AND tgrelid = 'outbox_event'::regclass"
            )
        ).scalar_one_or_none()
        return row is not None


def test_postgres_trigger_refresh_0006_to_head_when_configured() -> None:
    """A fresh schema upgraded through 0006 -> 0007 -> head gets the complete
    guards (0005 installs the old bodies, the refresh revisions re-run them)."""
    import uuid

    if not _pg_url():
        pytest.skip("PostgreSQL integration environment is not configured")
    from sqlalchemy import create_engine

    from alembic import command

    database_url = _pg_url()
    schema = f"mig_refresh_{uuid.uuid4().hex[:12]}"
    admin = create_engine(database_url)
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    finally:
        admin.dispose()
    scoped = _scoped_url(database_url, schema)
    try:
        config = alembic_config(scoped)
        command.upgrade(config, "0006_outbox_retirement_tombstone")
        engine = create_engine(scoped)
        # 0005 already installed the (old) trigger functions.
        assert _pg_trigger_installed(engine)
        engine.dispose()

        command.upgrade(config, "head")
        engine = create_engine(scoped)
        assert _pg_trigger_installed(engine)
        _assert_full_event_identity_guarded(engine)
        engine.dispose()
    finally:
        admin = create_engine(database_url)
        try:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            admin.dispose()


def test_postgres_trigger_refresh_replaces_broken_0007_bodies_when_configured() -> None:
    """A schema that carries the OLD (weaker) trigger bodies must be healed by
    the head refresh revision: trace_id becomes immutable after re-running."""
    import uuid

    if not _pg_url():
        pytest.skip("PostgreSQL integration environment is not configured")
    from sqlalchemy import create_engine

    from alembic import command

    database_url = _pg_url()
    schema = f"mig_heal_{uuid.uuid4().hex[:12]}"
    admin = create_engine(database_url)
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    finally:
        admin.dispose()
    scoped = _scoped_url(database_url, schema)
    try:
        config = alembic_config(scoped)
        command.upgrade(config, "0007_refresh_immutable_triggers")
        engine = create_engine(scoped)
        # Simulate a database that only ever saw the original 0005 bodies:
        # replace the event guard with the old weak function (identity-only,
        # trace mutable).
        with engine.begin() as connection:
            connection.execute(text("""
                    CREATE OR REPLACE FUNCTION trg_fn_outbox_event_immutable() RETURNS trigger AS $$
                    BEGIN
                        IF TG_OP = 'UPDATE' THEN
                            IF OLD.storage_state = 'full' AND NEW.storage_state = 'full' THEN
                                IF NEW.event_id IS DISTINCT FROM OLD.event_id THEN
                                    RAISE EXCEPTION 'immutable column % on outbox_event', 'event_id';
                                END IF;
                            END IF;
                        END IF;
                        IF TG_OP = 'UPDATE' THEN
                            RETURN NEW;
                        END IF;
                        RETURN OLD;
                    END;
                    $$ LANGUAGE plpgsql;
                    """))
        engine.dispose()

        command.upgrade(config, "head")
        engine = create_engine(scoped)
        assert _pg_trigger_installed(engine)
        _assert_full_event_identity_guarded(engine)
        engine.dispose()
    finally:
        admin = create_engine(database_url)
        try:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            admin.dispose()


def _assert_full_event_identity_guarded(engine) -> None:
    """Publish one event and verify the installed guards reject a trace_id change."""
    import uuid

    from _helpers import build_identity_service, cap, fixed_now, make_publisher, provision_user
    from sqlalchemy.exc import ProgrammingError

    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    identity = build_identity_service(engine)
    user_id = provision_user(identity, username=f"trigger_user_{uuid.uuid4().hex[:8]}")
    event_id = f"evt_refresh_{uuid.uuid4().hex[:8]}"
    publisher = make_publisher(engine)
    with engine.begin() as connection:
        publisher.publish(
            OutboxPublishCommand(
                capability=cap("ingestion"),
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
    with pytest.raises(ProgrammingError):
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE outbox_event SET trace_id = 'other' WHERE event_id = :eid"),
                {"eid": event_id},
            )
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE outbox_event SET occurred_at_utc = occurred_at_utc " "WHERE event_id = :eid"
            ),
            {"eid": event_id},
        )


_BROKEN_0009_EVENT_BODY = """
CREATE OR REPLACE FUNCTION trg_fn_outbox_event_immutable() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF OLD.storage_state = 'full' AND NEW.storage_state = 'full' THEN
            -- Historical broken body: raw JSON IS DISTINCT FROM (no ::text)
            -- fails with 'operator does not exist: json = json' on any
            -- UPDATE of a row carrying a JSON column.
            IF NEW.payload_json IS DISTINCT FROM OLD.payload_json THEN
                RAISE EXCEPTION 'immutable column payload_json on outbox_event';
            END IF;
            IF NEW.trace_id IS DISTINCT FROM OLD.trace_id THEN
                RAISE EXCEPTION 'immutable column trace_id on outbox_event';
            END IF;
        ELSIF OLD.storage_state = 'full' AND NEW.storage_state = 'compacted' THEN
            IF NEW.compacted_at_utc IS NULL THEN
                RAISE EXCEPTION 'compacted events must record compacted_at_utc';
            END IF;
        ELSE
            -- Historical broken body: whole-row no-op comparison on a row
            -- with a JSON column also fails with json = json.
            IF NEW IS NOT DISTINCT FROM OLD THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'outbox_event storage may only transition full -> compacted once';
        END IF;
    ELSIF TG_OP = 'DELETE' THEN
        IF OLD.storage_state = 'full' THEN
            RAISE EXCEPTION 'full outbox_event rows may only be removed by controlled compaction';
        END IF;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        RETURN NEW;
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;
"""


def test_postgres_upgrade_from_stamped_0009_with_broken_bodies_to_head() -> None:
    """A database REALLY upgraded to 0009 (full schema) whose trigger bodies
    were then replaced with the HISTORICAL broken text (raw JSON comparison +
    whole-row no-op allowance) must be healed by upgrading to head: only 0010
    runs (0008/0009 are already applied), so the fix must come from 0010's
    own refresh + CHECK. Exceptions are asserted precisely (SQLSTATE-driven),
    never as a blanket `pytest.raises(Exception)`."""
    import uuid

    if not _pg_url():
        pytest.skip("PostgreSQL integration environment is not configured")
    from sqlalchemy import create_engine

    from alembic import command

    database_url = _pg_url()
    schema = f"mig_broken_{uuid.uuid4().hex[:12]}"
    admin = create_engine(database_url)
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    finally:
        admin.dispose()
    scoped = _scoped_url(database_url, schema)
    try:
        config = alembic_config(scoped)
        # Real production upgrade: build the FULL schema through 0009 (not a
        # bare stamp on an empty schema).
        command.upgrade(config, "0009_refresh_immutable_triggers")
        engine = create_engine(scoped)
        # Then the deployed code at that time carried the broken bodies:
        # replace the event guard with the historical broken function text.
        with engine.begin() as connection:
            connection.execute(text(_BROKEN_0009_EVENT_BODY))
        engine.dispose()

        # Upgrade to head: 0008/0009 are already applied, so only 0010 runs.
        command.upgrade(config, "head")
        engine = create_engine(scoped)
        _assert_broken_body_healed(engine)
        engine.dispose()
    finally:
        admin = create_engine(database_url)
        try:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            admin.dispose()


def _assert_broken_body_healed(engine) -> None:
    """The healed guards must allow the production paths and reject the
    invalid ones."""
    import uuid

    from _helpers import build_identity_service, cap, fixed_now, make_publisher, provision_user
    from sqlalchemy.exc import ProgrammingError

    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    identity = build_identity_service(engine)
    user_id = provision_user(identity, username=f"heal_user_{uuid.uuid4().hex[:8]}")
    event_id = f"evt_heal_{uuid.uuid4().hex[:8]}"
    publisher = make_publisher(engine)
    with engine.begin() as connection:
        publisher.publish(
            OutboxPublishCommand(
                capability=cap("ingestion"),
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
    # 1. JSON scheduling update on a full event works (no json = json error).
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE outbox_event SET compact_after_at_utc = now() WHERE event_id = :eid"),
            {"eid": event_id},
        )
    # 2. A full event cannot set compacted facts. The trigger raises its own
    #    exception; the DB CHECK would raise 23514. Assert exactly the
    #    expected DBAPI class and verify the constraint/trigger message.
    with pytest.raises(ProgrammingError) as raised:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE outbox_event SET compacted_at_utc = now() WHERE event_id = :eid"),
                {"eid": event_id},
            )
    message = str(raised.value.orig)
    assert any(
        marker in message
        for marker in (
            "compacted_at_utc",
            "ck_outbox_event_compacted_fields_full_null",
            "23514",
        )
    )
    # 3. The legal full -> compacted transition works and then any further
    #    UPDATE on the compacted row is rejected (no whole-row json compare).
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE outbox_event SET storage_state = 'compacted', compacted_at_utc = now(), "
                "payload_json = NULL, trace_id = NULL, schema_version = NULL, "
                "compacted_delivery_summary_json = '[]' WHERE event_id = :eid"
            ),
            {"eid": event_id},
        )
    with pytest.raises(ProgrammingError):
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE outbox_event SET compacted_at_utc = now() WHERE event_id = :eid"),
                {"eid": event_id},
            )
    # 4. Attempt terminal summaries are enforced (delivered without ended_at
    #    is rejected; failed without an error summary is rejected).
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO outbox_delivery_attempt "
                "(delivery_attempt_id, event_id, consumer_name, replay_generation, "
                "attempt_number, cycle_attempt_number, fence_token, started_at_utc, status) "
                "VALUES (:aid, :eid, 'in_app_notification', 1, 1, 1, 1, now(), 'running')"
            ),
            {"aid": f"attempt_heal_{uuid.uuid4().hex[:8]}", "eid": event_id},
        )
    with pytest.raises(ProgrammingError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE outbox_delivery_attempt SET status = 'failed', ended_at_utc = now() "
                    "WHERE event_id = :eid"
                ),
                {"eid": event_id},
            )
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE outbox_delivery_attempt SET status = 'failed', ended_at_utc = now(), "
                "error_category = 'retryable', error_code = 'boom' WHERE event_id = :eid"
            ),
            {"eid": event_id},
        )
