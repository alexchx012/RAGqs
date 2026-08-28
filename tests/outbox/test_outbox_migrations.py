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


def test_postgres_final_immutable_triggers_remain_installed_to_head_when_configured() -> None:
    """A fresh schema upgraded through 0006 -> head retains the final guards
    installed by the immutable-trigger migration."""
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
        # 0005 already installed the final trigger functions.
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


def _assert_full_event_identity_guarded(engine) -> None:
    """Publish one event and verify the installed guards reject a trace_id change."""
    import uuid

    from _helpers import build_identity_service, fixed_now, make_publisher, provision_user
    from sqlalchemy.exc import ProgrammingError

    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    identity = build_identity_service(engine)
    user_id = provision_user(identity, username=f"trigger_user_{uuid.uuid4().hex[:8]}")
    event_id = f"evt_refresh_{uuid.uuid4().hex[:8]}"
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
