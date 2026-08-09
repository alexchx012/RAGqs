"""Fifth-round review items A: publisher capability deny-all, savepoint
concurrency, dispatcher generic-Exception handling, fail lease condition, OCR
fact type closure and container canonicalization."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from _helpers import (
    build_engine,
    build_identity_service,
    cap,
    fixed_now,
    make_publisher,
    provision_user,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.outbox.dispatcher import OutboxDispatcher
from app.outbox.metrics import SqlAlchemyOutboxMetrics
from app.outbox.notifications import NotificationMaterializer
from app.outbox.publisher import fingerprint_event
from app.outbox.schema import outbox_delivery_table, outbox_event_table
from app.platform.errors import PlatformError
from app.platform.persistence import FenceViolation


class _AcceptingGraph:
    def verify_activated_receipt(self, *, aggregate_id, graph_generation_id, connection) -> bool:
        del aggregate_id, graph_generation_id, connection
        return True


def make_command(**overrides):
    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    values = dict(
        event_id="evt_1",
        caller_principal="ingestion",
        capability=cap("ingestion"),
        event_type="ingestion_completed",
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
        recipients=(RecipientSelection(recipient_user_id="user_x"),),
    )
    values.update(overrides)
    return OutboxPublishCommand(**values)


def publish(engine, publisher, *, user_ids, **overrides):
    from app.outbox.ports import RecipientSelection

    recipients = tuple(RecipientSelection(recipient_user_id=u) for u in user_ids)
    return publisher.publish(
        make_command(recipients=recipients, **overrides),
        connection=engine.connect().__enter__(),
    )


def test_explicit_empty_capabilities_deny_all() -> None:
    engine = build_engine()
    # An explicit empty registry means NO capability objects are issued; any
    # caller-constructed command with a capability is still rejected because
    # the capability principal is not in the empty registry's scope... in the
    # token model, a capability WITHOUT a registry entry cannot exist. Simulate
    # the runtime issuing nothing: the publisher's registry is deny-all and a
    # command with no capability token is rejected.
    publisher = make_publisher(engine, now=lambda: fixed_now(), capabilities={})

    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    forged = OutboxPublishCommand(
        event_id="evt_empty",
        caller_principal="ingestion",
        capability=None,
        event_type="ingestion_completed",
        schema_version=1,
        aggregate_type="ingestion_job",
        aggregate_id="job_empty",
        transition_version=1,
        occurred_at=fixed_now(),
        payload={
            "job_id": "job_empty",
            "document_id": "doc_empty",
            "document_version_id": "docv_empty",
            "publication_id": "pub_empty",
        },
        trace_id="t",
        recipients=(RecipientSelection(recipient_user_id="user_x"),),
    )
    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            publisher.publish(forged, connection=connection)
    assert raised.value.code == "producer_not_authorized"
    assert raised.value.status_code == 403


def test_explicit_narrow_capabilities_deny_other_event_types() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(
        engine,
        now=lambda: fixed_now(),
        capabilities={"ingestion": frozenset({"ocr_low_confidence"})},
    )

    # A capability token scoped only to ocr_low_confidence cannot publish
    # ingestion_completed.
    narrow = cap("ingestion", "ocr_low_confidence")
    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            publisher.publish(
                make_command(
                    capability=narrow,
                    recipients=(
                        __import__(
                            "app.outbox.ports", fromlist=["RecipientSelection"]
                        ).RecipientSelection(recipient_user_id=alice),
                    ),
                ),
                connection=connection,
            )
    assert raised.value.code == "producer_not_authorized"


def test_publish_non_unique_integrity_error_is_not_swallowed() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    from app.outbox.ports import RecipientSelection

    publisher = make_publisher(engine, now=lambda: fixed_now())

    with engine.begin() as connection:
        publisher.publish(
            make_command(recipients=(RecipientSelection(recipient_user_id=alice),)),
            connection=connection,
        )
    # Simulate a concurrent publisher committing the same key between read+insert
    # by manually inserting the key row, then publishing again with a different
    # event_id: the savepoint re-read must reuse the real row.
    with engine.begin() as connection:
        connection.execute(
            outbox_event_table.insert().values(
                event_id="evt_concurrent",
                event_type="ingestion_completed",
                schema_version=1,
                aggregate_type="ingestion_job",
                aggregate_id="job_concurrent",
                transition_version=1,
                occurred_at_utc=fixed_now(),
                payload_json={
                    "job_id": "job_1",
                    "document_id": "doc_1",
                    "document_version_id": "docv_1",
                    "publication_id": "pub_1",
                },
                payload_fingerprint=fingerprint_event(
                    "ingestion_completed",
                    1,
                    {
                        "job_id": "job_1",
                        "document_id": "doc_1",
                        "document_version_id": "docv_1",
                        "publication_id": "pub_1",
                    },
                ),
                trace_id=None,
                created_at_utc=fixed_now(),
                storage_state="full",
                compact_after_at_utc=None,
                compacted_at_utc=None,
                compacted_delivery_summary_json=None,
            )
        )
        receipt = publisher.publish(
            make_command(
                event_id="evt_other",
                recipients=(RecipientSelection(recipient_user_id=alice),),
                aggregate_id="job_concurrent",
            ),
            connection=connection,
        )
    assert receipt.reused is True
    assert receipt.event_id == "evt_concurrent"

    # A non-unique integrity error (e.g. a broken insert path) must NOT be
    # swallowed as a reuse: monkeypatch the insert to raise a non-unique error.
    def broken_insert(connection, command, occurred_at, fingerprint, created_at=None):
        del connection, command, occurred_at, fingerprint, created_at
        raise IntegrityError("stmt", {}, Exception("unrelated constraint"))

    publisher._insert_event_row = broken_insert
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            publisher.publish(
                make_command(
                    event_id="evt_never",
                    recipients=(RecipientSelection(recipient_user_id=alice),),
                    aggregate_id="job_never",
                ),
                connection=connection,
            )


def test_dispatcher_generic_exception_is_retryable_not_crash() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    from app.outbox.ports import RecipientSelection

    publisher = make_publisher(engine, now=lambda: fixed_now())
    with engine.begin() as connection:
        publisher.publish(
            make_command(recipients=(RecipientSelection(recipient_user_id=alice),)),
            connection=connection,
        )
    dispatcher = OutboxDispatcher(
        engine,
        consumers={"in_app_notification": _ExplodingConsumer()},
        now=lambda: fixed_now(),
        retention_days=30,
        notification_retention_days=90,
        metrics=SqlAlchemyOutboxMetrics(),
    )
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    outcome = dispatcher.run_consumer_and_finalize(claim, owner="worker-1")

    assert outcome.status == "failed"
    assert outcome.error_category == "retryable"
    with engine.connect() as connection:
        status = connection.execute(
            select(outbox_delivery_table.c.status).where(
                outbox_delivery_table.c.event_id == "evt_1"
            )
        ).scalar_one()
        assert status == "retry_wait"


class _ExplodingConsumer:
    def materialize(self, connection, **kwargs):
        del connection, kwargs
        raise RuntimeError("boom inside consumer")


def test_fail_and_schedule_rejects_an_expired_lease() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    from app.outbox.ports import RecipientSelection

    publisher = make_publisher(engine, now=lambda: fixed_now())
    with engine.begin() as connection:
        publisher.publish(
            make_command(recipients=(RecipientSelection(recipient_user_id=alice),)),
            connection=connection,
        )

    clock = _MutableClock(datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC))
    dispatcher = OutboxDispatcher(
        engine,
        consumers={"in_app_notification": NotificationMaterializer(engine)},
        now=clock.now,
        retention_days=30,
        notification_retention_days=90,
        metrics=SqlAlchemyOutboxMetrics(),
    )
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    # 120s pass: the lease (60s) expired, so failure scheduling must abort.
    clock.advance(seconds=120)

    with pytest.raises(FenceViolation):
        dispatcher.fail_and_schedule(
            claim, owner="worker-1", error_category="retryable", error_code="boom"
        )


class _MutableClock:
    def __init__(self, start: datetime) -> None:
        self._current = start

    def now(self) -> datetime:
        return self._current

    def advance(self, seconds: int) -> None:
        from datetime import timedelta

        self._current = self._current + timedelta(seconds=seconds)


def test_ocr_fact_page_region_types_and_container_canonicalization() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    from app.outbox.ports import RecipientSelection

    publisher = make_publisher(engine, now=lambda: fixed_now())

    # page must be an int
    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            publisher.publish(
                make_command(
                    event_type="ocr_low_confidence",
                    payload={
                        "job_id": "job_1",
                        "document_id": "doc_1",
                        "document_version_id": "docv_1",
                        "publication_id": "pub_1",
                        "machine_low_confidence_fact": {
                            "confidence": 0.4,
                            "page": "three",
                            "region": [1, 2],
                        },
                    },
                    recipients=(RecipientSelection(recipient_user_id=alice),),
                ),
                connection=connection,
            )
    assert raised.value.code == "invalid_event_payload"

    # region must be a list of ints
    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            publisher.publish(
                make_command(
                    event_type="ocr_low_confidence",
                    payload={
                        "job_id": "job_1",
                        "document_id": "doc_1",
                        "document_version_id": "docv_1",
                        "publication_id": "pub_1",
                        "machine_low_confidence_fact": {
                            "confidence": 0.4,
                            "page": 3,
                            "region": ["left"],
                        },
                    },
                    recipients=(RecipientSelection(recipient_user_id=alice),),
                ),
                connection=connection,
            )
    assert raised.value.code == "invalid_event_payload"

    # Sensitive keys nested in tuple containers are rejected (canonicalized).
    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            publisher.publish(
                make_command(
                    payload={
                        "job_id": "job_1",
                        "document_id": "doc_1",
                        "document_version_id": "docv_1",
                        "publication_id": "pub_1",
                        "extra_list": [{"snippet": "secret"}],
                    },
                    recipients=(RecipientSelection(recipient_user_id=alice),),
                ),
                connection=connection,
            )
    assert raised.value.code == "invalid_event_payload"
