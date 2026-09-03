"""Outbox dispatcher worker contract."""

from __future__ import annotations

from sqlalchemy import select

from app.outbox.schema import notification_table
from app.outbox.worker import OutboxWorker
from app.platform.config import load_platform_settings
from app.platform.runtime import build_runtime
from app.platform.worker import create_worker_runtime
from tests._support import (
    build_engine,
    build_identity_service,
    fixed_now,
    make_publisher,
    make_settings,
    provision_user,
)


def test_outbox_worker_delivers_pending_events() -> None:
    configured = make_settings()
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    publisher = make_publisher(engine, now=lambda: fixed_now())

    with engine.begin() as connection:
        publisher.publish(
            OutboxPublishCommand(
                event_id="evt_1",
                event_type="ingestion_completed",
                caller_principal="ingestion",
                schema_version=1,
                aggregate_type="ingestion_job",
                aggregate_id="job_1",
                transition_version=1,
                occurred_at=fixed_now(),
                payload={
                    "job_id": "job_1",
                    "document_id": "doc_1",
                    "document_version_id": "docv_1",
                    "publication_id": "pub_1",
                },
                trace_id="trace_x",
                recipients=(RecipientSelection(recipient_user_id=alice),),
            ),
            connection=connection,
        )
    runtime = build_runtime(
        configured,
        adapters={"database_engine": engine, "identity_access": identity},
    )
    worker_runtime = create_worker_runtime(
        configured,
        runtime=runtime,
        now=lambda: fixed_now(),
    )

    stats = OutboxWorker(worker_runtime).run_once(owner="worker-1")

    assert stats.claimed == 1
    assert stats.delivered == 1
    assert stats.failed == 0
    with engine.connect() as connection:
        notifications = connection.execute(select(notification_table)).all()
        assert len(notifications) == 1
    runtime.close()


def test_outbox_worker_is_noop_when_nothing_is_due() -> None:
    configured = make_settings()
    engine = build_engine()
    identity = build_identity_service(engine)
    runtime = build_runtime(
        configured,
        adapters={"database_engine": engine, "identity_access": identity},
    )
    worker_runtime = create_worker_runtime(
        configured,
        runtime=runtime,
        now=lambda: fixed_now(),
    )

    stats = OutboxWorker(worker_runtime).run_once(owner="worker-1")

    assert stats.claimed == 0
    assert stats.delivered == 0
    runtime.close()


def test_outbox_worker_run_once_entrypoint_returns_stats() -> None:
    configured = make_settings()
    engine = build_engine()
    identity = build_identity_service(engine)
    runtime = build_runtime(
        configured,
        adapters={"database_engine": engine, "identity_access": identity},
    )

    from app.outbox.worker import run_outbox_worker_once

    stats = run_outbox_worker_once(
        load_platform_settings(
            {
                "RAG_PLATFORM_PROFILE": "development",
                "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
                "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
                "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
                "RAG_PROVIDER_NAME": "fake",
            }
        ),
        runtime=runtime,
        owner="worker-1",
    )

    assert stats.claimed == 0
    runtime.close()
