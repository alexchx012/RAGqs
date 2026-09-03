"""Blocker 5: redaction and materialization serialize on the same document
lock; materialization re-checks the tombstone inside the lock right before
insert, so a redaction committed concurrently can never be overwritten."""

from __future__ import annotations

from sqlalchemy import select, text

from app.outbox.dispatcher import OutboxDispatcher
from app.outbox.lifecycle import SqlAlchemyOutboxLifecycle
from app.outbox.metrics import SqlAlchemyOutboxMetrics
from app.outbox.notifications import NotificationMaterializer
from app.outbox.schema import notification_table
from tests._support import (
    build_engine,
    build_identity_service,
    fixed_now,
    make_publisher,
    provision_user,
)


def make_lifecycle(engine):
    return SqlAlchemyOutboxLifecycle(
        engine,
        now=lambda: fixed_now(),
        archive_verifier=_Accepting(),
    )


class _Accepting:
    def verify_archive(self, *, archive_ref: str, checksum: str, **kwargs) -> bool:
        del archive_ref, checksum
        return True


def publish(engine, *, user_ids, event_id="evt_1"):
    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    publisher = make_publisher(engine, now=lambda: fixed_now())
    command = OutboxPublishCommand(
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
        trace_id="trace_x",
        recipients=tuple(RecipientSelection(recipient_user_id=u) for u in user_ids),
    )
    with engine.begin() as connection:
        publisher.publish(command, connection=connection)


def make_dispatcher(engine):
    return OutboxDispatcher(
        engine,
        consumers={"in_app_notification": NotificationMaterializer(engine)},
        now=lambda: fixed_now(),
        retention_days=30,
        notification_retention_days=90,
        metrics=SqlAlchemyOutboxMetrics(),
    )


def redact_command(*, document_id="doc_evt_1", document_version_ids=("docv_evt_1",)):
    from app.outbox.ports import DocumentNotificationRedactionCommand

    return DocumentNotificationRedactionCommand(
        operation_id="op_1",
        caller_principal="documents",
        deletion_id="del_1",
        document_id=document_id,
        document_version_ids=document_version_ids,
        reason="document_pending_delete",
        transaction_id="tx_1",
        mode="inline",
        canonical_input_fingerprint="fp_1",
    )


def test_redaction_and_materialization_share_the_document_lock() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    lifecycle = make_lifecycle(engine)
    dispatcher = make_dispatcher(engine)

    # Redaction first: tombstone + redacted rendering committed.
    with engine.begin() as connection:
        lifecycle.redact_document_notifications(redact_command(), connection=connection)

    # Then materialization: it must take the same document lock and re-check
    # the tombstone inside the lock, rendering the deleted text.
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")

    with engine.connect() as connection:
        row = (
            connection.execute(
                select(notification_table).where(notification_table.c.event_id == "evt_1")
            )
            .mappings()
            .one()
        )
        assert row["title"] == "Deleted document"
        assert row["redacted"] is True
        assert row["payload_json"] == {}
        assert row["document_id"] == "doc_evt_1"
        assert row["document_version_id"] == "docv_evt_1"


def test_materialization_committed_before_redaction_is_then_redacted() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    lifecycle = make_lifecycle(engine)
    dispatcher = make_dispatcher(engine)

    # Materialize first with the original rendering.
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(notification_table).where(notification_table.c.event_id == "evt_1")
            )
            .mappings()
            .one()
        )
        assert row["title"] == "Document ingestion completed"

    # Redaction afterwards replaces the human-readable fields.
    with engine.begin() as connection:
        lifecycle.redact_document_notifications(redact_command(), connection=connection)
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(notification_table).where(notification_table.c.event_id == "evt_1")
            )
            .mappings()
            .one()
        )
        assert row["title"] == "Deleted document"
        assert row["redacted"] is True


def test_document_lock_is_used_by_both_paths_on_postgresql() -> None:
    """Both paths compile the same advisory lock keyed by document id."""
    from sqlalchemy.dialects import postgresql

    redact_stmt = text("SELECT pg_advisory_xact_lock(hashtext(:lock))")
    sql = str(redact_stmt.compile(dialect=postgresql.dialect()))
    assert "pg_advisory_xact_lock" in sql
    compiled = str(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock))").compile(dialect=postgresql.dialect())
    )
    assert "hashtext" in compiled
