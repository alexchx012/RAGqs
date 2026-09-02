"""Materialized titles read readable domain facts at materialization time.

Design 13.1: the outbox payload keeps only opaque machine facts, so the
dispatcher substitutes the current document name into the title when the
event carries a document identifier. Documents inside the deletion lifecycle
keep the fixed redacted text (tombstone first, lifecycle as the floor), and
events without a document fact keep their typed default title.
"""

from __future__ import annotations

from _helpers import (
    build_engine,
    build_identity_service,
    fixed_now,
    make_publisher,
    provision_user,
)
from sqlalchemy import select

from app.documents.schema import documents_table
from app.outbox.dispatcher import OutboxDispatcher
from app.outbox.notifications import NotificationMaterializer
from app.outbox.ports import (
    DocumentNotificationRedactionCommand,
    OutboxPublishCommand,
    RecipientSelection,
)
from app.outbox.schema import notification_table

_PRODUCTION_EVENTS: dict[str, dict[str, object]] = {
    "ingestion_completed": {
        "caller_principal": "ingestion",
        "aggregate_type": "ingestion_job",
        "payload": {
            "job_id": "job_evt_1",
            "document_id": "doc_evt_1",
            "document_version_id": "docv_evt_1",
            "publication_id": "pub_evt_1",
        },
    },
    "ocr_low_confidence": {
        "caller_principal": "ingestion",
        "aggregate_type": "ingestion_job",
        "payload": {
            "job_id": "job_evt_1",
            "document_id": "doc_evt_1",
            "document_version_id": "docv_evt_1",
            "publication_id": "pub_evt_1",
            "reason": "low_confidence",
            "status": "published",
            "machine_low_confidence_fact": {"confidence": 0.42, "page": 1, "region": [0, 0, 6, 6]},
        },
    },
    "quota_approved": {
        "caller_principal": "quota",
        "aggregate_type": "quota_request",
        "payload": {"request_id": "request_evt_1"},
    },
    "graph_build_completed": {
        "caller_principal": "knowledge_graph",
        "aggregate_type": "graph_build_run",
        "payload": {
            "graph_build_id": "gb_evt_1",
            "status": "succeeded",
            "source_revision": 1,
            "graph_generation_id": "gen_evt_1",
            "index_generation_id": "index_gen_evt_1",
            "failure_class": None,
        },
    },
}


class _Accepting:
    def verify_archive(self, *, archive_ref: str, checksum: str, **kwargs) -> bool:
        del archive_ref, checksum
        return True


class _AcceptingGraphReceipt:
    def verify_activated_receipt(
        self, *, aggregate_id: str, graph_generation_id: str, connection
    ) -> bool:
        del aggregate_id, graph_generation_id, connection
        return True


def insert_document(engine, *, document_id: str, name: str, lifecycle_status: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            documents_table.insert().values(
                id=document_id,
                space_id="public",
                lifecycle_status=lifecycle_status,
                version=1,
                name=name,
                uploaded_at_utc=fixed_now(),
                created_at_utc=fixed_now(),
                updated_at_utc=fixed_now(),
            )
        )


def publish(engine, *, user_id: str, event_type: str) -> None:
    spec = _PRODUCTION_EVENTS[event_type]
    publisher = make_publisher(
        engine,
        now=lambda: fixed_now(),
        graph_activated_receipt_port=_AcceptingGraphReceipt(),
    )
    command = OutboxPublishCommand(
        event_id=f"evt_{event_type}",
        caller_principal=str(spec["caller_principal"]),
        event_type=event_type,
        schema_version=1,
        aggregate_type=str(spec["aggregate_type"]),
        aggregate_id="job_evt_1",
        transition_version=1,
        occurred_at=fixed_now(),
        payload=dict(spec["payload"]),  # type: ignore[arg-type]
        trace_id="trace_x",
        recipients=(RecipientSelection(recipient_user_id=user_id),),
    )
    with engine.begin() as connection:
        publisher.publish(command, connection=connection)


def deliver(engine) -> None:
    dispatcher = OutboxDispatcher(
        engine,
        consumers={"in_app_notification": NotificationMaterializer(engine)},
        now=lambda: fixed_now(),
        retention_days=30,
        notification_retention_days=90,
    )
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")


def materialized(engine, *, event_id: str) -> tuple[str, bool]:
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(
                    notification_table.c.title,
                    notification_table.c.redacted,
                ).where(notification_table.c.event_id == event_id)
            )
            .mappings()
            .one()
        )
    return str(row["title"]), bool(row["redacted"])


def redact_document(engine, *, document_id: str, version_ids: tuple[str, ...]) -> None:
    from app.outbox.lifecycle import SqlAlchemyOutboxLifecycle

    lifecycle = SqlAlchemyOutboxLifecycle(
        engine, now=lambda: fixed_now(), archive_verifier=_Accepting()
    )
    with engine.begin() as connection:
        lifecycle.redact_document_notifications(
            DocumentNotificationRedactionCommand(
                operation_id=f"op_{document_id}",
                caller_principal="documents",
                deletion_id=f"del_{document_id}",
                document_id=document_id,
                document_version_ids=version_ids,
                reason="document_pending_delete",
                transaction_id="tx_1",
                mode="inline",
                canonical_input_fingerprint="fp_1",
            ),
            connection=connection,
        )


def test_ingestion_completed_title_substitutes_document_name() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    insert_document(engine, document_id="doc_evt_1", name="季度报告.pdf", lifecycle_status="active")
    publish(engine, user_id=alice, event_type="ingestion_completed")
    deliver(engine)

    title, redacted = materialized(engine, event_id="evt_ingestion_completed")
    assert title == 'Document "季度报告.pdf" ingestion completed'
    assert redacted is False
    with engine.connect() as connection:
        stored = connection.execute(
            select(notification_table.c.payload_json).where(
                notification_table.c.event_id == "evt_ingestion_completed"
            )
        ).scalar_one()
    # The payload keeps only opaque machine facts; the name lives in the title.
    assert stored["document_id"] == "doc_evt_1"
    assert not any("季度报告" in str(value) for value in stored.values())


def test_ocr_low_confidence_title_substitutes_document_name() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    insert_document(
        engine, document_id="doc_evt_1", name="Scanned contract.pdf", lifecycle_status="active"
    )
    publish(engine, user_id=alice, event_type="ocr_low_confidence")
    deliver(engine)

    title, redacted = materialized(engine, event_id="evt_ocr_low_confidence")
    assert title == 'Low-confidence OCR result for document "Scanned contract.pdf"'
    assert redacted is False


def test_deleted_document_with_tombstone_keeps_redacted_title() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    insert_document(
        engine, document_id="doc_evt_1", name="季度报告.pdf", lifecycle_status="pending_delete"
    )
    publish(engine, user_id=alice, event_type="ingestion_completed")
    # The deletion transaction writes the permanent (document, version)
    # tombstone before the dispatcher materializes the notification.
    redact_document(engine, document_id="doc_evt_1", version_ids=("docv_evt_1",))
    deliver(engine)

    title, redacted = materialized(engine, event_id="evt_ingestion_completed")
    assert title == "Deleted document"
    assert redacted is True


def test_document_in_deletion_lifecycle_without_tombstone_keeps_redacted_title() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    # The documents row already left the readable lifecycle and no
    # exact-version tombstone covers this event's version: the name must
    # still never surface.
    insert_document(
        engine, document_id="doc_evt_1", name="季度报告.pdf", lifecycle_status="deleted"
    )
    publish(engine, user_id=alice, event_type="ingestion_completed")
    deliver(engine)

    title, redacted = materialized(engine, event_id="evt_ingestion_completed")
    assert title == "Deleted document"


def test_missing_document_fact_keeps_typed_title() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    # No documents row at all: nothing readable to substitute.
    publish(engine, user_id=alice, event_type="ingestion_completed")
    deliver(engine)

    title, _ = materialized(engine, event_id="evt_ingestion_completed")
    assert title == "Document ingestion completed"


def test_quota_and_graph_events_keep_typed_titles() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    # Quota and graph events carry no document identifier: typed titles stay.
    publish(engine, user_id=alice, event_type="quota_approved")
    deliver(engine)
    publish(engine, user_id=alice, event_type="graph_build_completed")
    deliver(engine)

    quota_title, quota_redacted = materialized(engine, event_id="evt_quota_approved")
    graph_title, graph_redacted = materialized(engine, event_id="evt_graph_build_completed")
    assert quota_title == "Quota request approved"
    assert quota_redacted is False
    assert graph_title == "Knowledge graph build completed"
    assert graph_redacted is False
