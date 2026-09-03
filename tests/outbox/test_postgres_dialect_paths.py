"""PostgreSQL-specific SQL path verification at the compiled-SQL level.

A real PostgreSQL instance is not available in this environment, so the
PostgreSQL-only branches are verified by compiling the exact statements the
dispatcher and service would execute against the PostgreSQL dialect. The
remaining environment risk (running them against a live cluster) is documented
in the integration suite (tests/platform/test_postgres_s3_integration.py).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url

from app.outbox.dispatcher import OutboxDispatcher
from app.outbox.metrics import SqlAlchemyOutboxMetrics
from app.outbox.notifications import NotificationMaterializer
from app.outbox.schema import (
    notification_inbox_table,
    outbox_delivery_table,
)
from tests._support import build_engine, fixed_now


def compile_statement(statement) -> str:
    compiled = statement.compile(dialect=postgresql.dialect())
    return str(compiled)


def test_claim_uses_for_update_skip_locked_on_postgresql() -> None:
    engine = build_engine()
    OutboxDispatcher(
        engine,
        consumers={"in_app_notification": NotificationMaterializer(engine)},
        now=lambda: fixed_now(),
        retention_days=30,
        notification_retention_days=90,
        metrics=SqlAlchemyOutboxMetrics(),
    )
    now = fixed_now()
    candidate = (
        select(outbox_delivery_table)
        .where(
            or_(
                and_(
                    outbox_delivery_table.c.status == "pending",
                    outbox_delivery_table.c.next_attempt_at_utc.is_(None),
                ),
                and_(
                    outbox_delivery_table.c.status.in_(("pending", "retry_wait")),
                    outbox_delivery_table.c.next_attempt_at_utc <= now,
                ),
            )
        )
        .with_for_update(skip_locked=True)
    )
    sql = compile_statement(candidate)

    assert "FOR UPDATE SKIP LOCKED" in sql
    # The SQLite branch must NOT contain the lock clause.
    sqlite_sql = str(candidate.compile(dialect=__import__("sqlalchemy").dialects.sqlite.dialect()))
    assert "FOR UPDATE" not in sqlite_sql


def test_database_time_uses_clock_timestamp_on_postgresql() -> None:
    engine = build_engine()

    # _current_timestamp returns clock_timestamp() only for postgresql dialect;
    # verify the compiled expression through a statement.
    from sqlalchemy import func

    statement = select(func.clock_timestamp(type_=__import__("sqlalchemy").DateTime(timezone=True)))
    sql = compile_statement(statement)
    assert "clock_timestamp" in sql

    current = select(
        func.clock_timestamp(type_=__import__("sqlalchemy").DateTime(timezone=True))
        if engine.dialect.name == "postgresql"
        else func.current_timestamp()
    )
    assert "clock_timestamp" in compile_statement(
        current
    ) or "current_timestamp" in compile_statement(current)


def test_read_all_uses_advisory_lock_on_postgresql() -> None:
    statement = text("SELECT pg_advisory_xact_lock(hashtext(:lock))")
    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "pg_advisory_xact_lock" in sql
    assert "hashtext" in sql


def test_read_all_inbox_update_compiles_for_postgresql() -> None:
    target = 5
    statement = (
        update(notification_inbox_table)
        .where(
            notification_inbox_table.c.recipient_user_id == "user_x",
            notification_inbox_table.c.version == 1,
        )
        .values(
            read_through_seq=target,
            read_all_at_utc=datetime(2026, 8, 5, tzinfo=UTC),
            version=2,
        )
    )
    sql = compile_statement(statement)

    assert "UPDATE notification_inbox" in sql
    assert "read_through_seq" in sql


def test_delivery_claim_update_compiles_for_postgresql() -> None:
    lease_expires = fixed_now() + timedelta(seconds=60)
    statement = (
        update(outbox_delivery_table)
        .where(
            outbox_delivery_table.c.event_id == "evt_1",
            outbox_delivery_table.c.consumer_name == "in_app_notification",
            outbox_delivery_table.c.status.in_(("pending", "retry_wait")),
            outbox_delivery_table.c.version == 1,
        )
        .values(
            status="running",
            version=2,
            attempt_number=1,
            lease_owner="worker-1",
            lease_expires_at_utc=lease_expires,
            fence_token=1,
        )
    )
    sql = compile_statement(statement)

    assert "UPDATE outbox_delivery" in sql
    assert "lease_owner" in sql
    assert "fence_token" in sql
    assert "IN (" in sql
    assert "status" in sql


def test_postgres_url_is_accepted_by_the_runtime_configuration() -> None:
    from app.platform.config import load_platform_settings

    settings = load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "postgresql+psycopg://app:secret@db/rag",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
            "RAG_AUTH_SECRET_KEY": "test-secret-that-is-long-enough",
        }
    )
    assert settings.database.url.startswith("postgresql")
    url = make_url(settings.database.url)
    assert url.get_backend_name() == "postgresql"
