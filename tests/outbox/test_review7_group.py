"""Sixth-round review items A/B/D/E: unforgeable capability tokens, operation
reservation before side effects, receipt verification in compaction, compaction
retirement-scope matching."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from _helpers import (
    CAPABILITY_SECRET,
    build_engine,
    build_identity_service,
    cap,
    docs_redaction_token,
    fixed_now,
    make_publisher,
    provision_user,
    retention_token,
)
from sqlalchemy import select, update

from app.identity.schema import identity_user_table
from app.outbox.dispatcher import OutboxDispatcher
from app.outbox.lifecycle import SqlAlchemyOutboxLifecycle, _command_input_fingerprint
from app.outbox.metrics import SqlAlchemyOutboxMetrics
from app.outbox.notifications import NotificationMaterializer
from app.outbox.schema import (
    notification_delivery_receipt_table,
    outbox_document_tombstone_table,
    outbox_event_table,
)
from app.platform.errors import PlatformError

INGESTION_CAP = cap("ingestion")


def make_command(**overrides):
    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    values = dict(
        event_id="evt_1",
        caller_principal="ingestion",
        capability=INGESTION_CAP,
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


def make_lifecycle(engine, **kwargs):
    kwargs.setdefault("archive_verifier", _AcceptingArchive())
    kwargs.setdefault("capability_secret", CAPABILITY_SECRET)
    return SqlAlchemyOutboxLifecycle(engine, now=lambda: fixed_now(), **kwargs)


class _AcceptingArchive:
    def verify_archive(self, *, archive_ref, checksum, **kwargs) -> bool:
        del archive_ref, checksum, kwargs
        return True


def make_dispatcher(engine):
    return OutboxDispatcher(
        engine,
        consumers={"in_app_notification": NotificationMaterializer(engine)},
        now=lambda: fixed_now(),
        retention_days=30,
        notification_retention_days=90,
        metrics=SqlAlchemyOutboxMetrics(),
    )


def publish(engine, *, user_ids, event_id="evt_1", capability=INGESTION_CAP):
    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    publisher = make_publisher(engine, now=lambda: fixed_now())
    command = OutboxPublishCommand(
        event_id=event_id,
        caller_principal="ingestion",
        capability=capability,
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


def mark_deletable(engine, user_id: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            update(identity_user_table)
            .where(identity_user_table.c.id == user_id)
            .values(lifecycle_status="pending_delete")
        )


def retire_command(*, user_id: str, operation_id="op_ret_1", **overrides):
    from app.outbox.ports import AccountNotificationRetirementCommand

    values = dict(
        operation_id=operation_id,
        caller_principal="retention-ops",
        user_id=user_id,
        deletion_id="del_1",
        verified_archive_ref="archive_ref_1",
        archive_checksum="checksum_1",
        transaction_id="tx_1",
        mode="inline",
        canonical_input_fingerprint="unused",
        capability_token=retention_token(),
    )
    values.update(overrides)
    return AccountNotificationRetirementCommand(**values)


# ---------------------------------------------------------------------------
# A. Capability authority: strings never authorize
# ---------------------------------------------------------------------------


def test_publish_without_a_capability_token_is_forbidden() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())

    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    forged = OutboxPublishCommand(
        event_id="evt_forged",
        caller_principal="ingestion",  # a valid-sounding string is NOT authority
        capability=None,
        event_type="ingestion_completed",
        schema_version=1,
        aggregate_type="ingestion_job",
        aggregate_id="job_forged",
        transition_version=1,
        occurred_at=fixed_now(),
        payload={
            "job_id": "job_forged",
            "document_id": "doc_forged",
            "document_version_id": "docv_forged",
            "publication_id": "pub_forged",
        },
        trace_id="t",
        recipients=(RecipientSelection(recipient_user_id=alice),),
    )
    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            publisher.publish(forged, connection=connection)
    assert raised.value.status_code == 403
    assert raised.value.code == "producer_not_authorized"


def test_publish_rejects_an_audit_label_mismatching_the_capability() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())

    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    forged = OutboxPublishCommand(
        event_id="evt_label",
        caller_principal="quota",  # audit label disagrees with the capability
        capability=INGESTION_CAP,
        event_type="ingestion_completed",
        schema_version=1,
        aggregate_type="ingestion_job",
        aggregate_id="job_label",
        transition_version=1,
        occurred_at=fixed_now(),
        payload={
            "job_id": "job_label",
            "document_id": "doc_label",
            "document_version_id": "docv_label",
            "publication_id": "pub_label",
        },
        trace_id="t",
        recipients=(RecipientSelection(recipient_user_id=alice),),
    )
    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            publisher.publish(forged, connection=connection)
    assert raised.value.status_code == 403


def test_capability_scopes_the_allowed_event_types() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())

    narrow = cap("ingestion", "ocr_low_confidence")
    from app.outbox.ports import RecipientSelection

    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            publisher.publish(
                make_command(
                    event_id="evt_narrow",
                    aggregate_id="job_narrow",
                    capability=narrow,
                    recipients=(RecipientSelection(recipient_user_id=alice),),
                ),
                connection=connection,
            )
    assert raised.value.code == "producer_not_authorized"


def test_runtime_exposes_scoped_producer_capabilities() -> None:
    from _helpers import make_settings

    from app.platform.runtime import build_runtime

    engine = build_engine()
    runtime = build_runtime(make_settings(), adapters={"database_engine": engine})
    # The raw producer registry is NOT a business interface: the master secret
    # and the issuer are assembly-time internals and never resolvable adapters.
    assert runtime.resolve("producer_capabilities", None) is None
    # The retention token is stored PRIVATELY: it is never resolvable as a
    # generic adapter and only the retirement gateway/worker access it through
    # The retention token is not stored on the runtime at all (not even under
    # a private key); the assembled worker holds it internally.
    assert runtime.resolve("retention_capability_token", None) is None
    assert "_retention_capability_token" not in runtime.adapters
    assert runtime.resolve("retirement_worker") is not None
    runtime.close()


# ---------------------------------------------------------------------------
# B. Operation reservation happens before any side effect
# ---------------------------------------------------------------------------


def test_redaction_reserves_before_touching_notifications() -> None:
    """A concurrent same-operation commit must leave NO redaction side effect:
    the conflicting caller's notification updates are rolled back with the
    savepoint and the original receipt is returned."""
    from app.outbox.lifecycle import _command_input_fingerprint
    from app.outbox.ports import DocumentNotificationRedactionCommand
    from app.outbox.schema import outbox_redaction_receipt_table

    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    lifecycle = make_lifecycle(engine)

    command = DocumentNotificationRedactionCommand(
        operation_id="op_b",
        caller_principal="documents",
        deletion_id="del_1",
        document_id="doc_evt_1",
        document_version_ids=("docv_evt_1",),
        reason="document_pending_delete",
        transaction_id="documents-delete:tx_1",
        mode="inline",
        canonical_input_fingerprint="unused",
        capability_token=docs_redaction_token(
            deletion_id="del_1", transaction_id="documents-delete:tx_1"
        ),
    )
    expected_fp = _command_input_fingerprint(command)

    # A concurrent caller already committed the same operation WITH side
    # effects (receipt + tombstone + redacted notification).
    with engine.begin() as connection:
        connection.execute(
            outbox_redaction_receipt_table.insert().values(
                operation_id="op_b",
                deletion_id="del_1",
                document_id="doc_evt_1",
                document_version_ids_json=["docv_evt_1"],
                input_fingerprint=expected_fp,
                state="completed",
                redacted_notification_count=1,
                already_redacted_count=0,
                created_at_utc=fixed_now(),
            )
        )
        connection.execute(
            outbox_document_tombstone_table.insert().values(
                document_id="doc_evt_1",
                document_version_id="docv_evt_1",
                deletion_id="del_1",
                created_at_utc=fixed_now(),
            )
        )

    # Our call returns the winner's receipt and applies NO new side effects.
    with engine.begin() as connection:
        receipt = lifecycle.redact_document_notifications(command, connection=connection)
    assert receipt.operation_id == "op_b"
    assert receipt.state == "completed"
    with engine.connect() as connection:
        tombstones = connection.execute(
            select(outbox_document_tombstone_table).where(
                outbox_document_tombstone_table.c.document_id == "doc_evt_1"
            )
        ).all()
        assert len(tombstones) == 1  # no duplicate tombstone
        receipts = connection.execute(select(outbox_redaction_receipt_table)).all()
        assert len(receipts) == 1  # no duplicate receipt


# ---------------------------------------------------------------------------
# D. Compaction verifies existing receipts (outcome/seq/fingerprint)
# ---------------------------------------------------------------------------


def test_compaction_verifies_existing_suppression_receipt_before_deleting() -> None:
    from app.outbox.schema import notification_suppression_table

    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    # Suppress alice before materialization.
    with engine.begin() as connection:
        connection.execute(
            update(identity_user_table)
            .where(identity_user_table.c.id == alice)
            .values(lifecycle_status="pending_delete")
        )
    dispatcher = make_dispatcher(engine)
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
    with engine.connect() as connection:
        assert (
            connection.execute(
                select(notification_suppression_table).where(
                    notification_suppression_table.c.event_id == "evt_1"
                )
            ).all()
            != []
        )

    # A tampered receipt exists for the suppressed recipient.
    with engine.begin() as connection:
        connection.execute(
            notification_delivery_receipt_table.insert().values(
                event_id="evt_1",
                recipient_user_id=alice,
                outcome="recipient_inactive",
                original_notification_seq=None,
                occurred_at_utc=fixed_now(),
                materialized_at_utc=None,
                retired_at_utc=fixed_now(),
                fingerprint="tampered-fingerprint",
            )
        )
    with engine.begin() as connection:
        connection.execute(
            update(outbox_event_table)
            .where(outbox_event_table.c.event_id == "evt_1")
            .values(compact_after_at_utc=datetime(2000, 1, 1, tzinfo=UTC))
        )

    with pytest.raises(PlatformError) as raised:
        dispatcher.compact_due_events(now=datetime(2026, 8, 5, tzinfo=UTC))
    assert raised.value.code == "receipt_fingerprint_mismatch"
    with engine.connect() as connection:
        # The suppression row is NOT deleted when verification fails.
        assert (
            connection.execute(
                select(notification_suppression_table).where(
                    notification_suppression_table.c.event_id == "evt_1"
                )
            ).all()
            != []
        )
        state = connection.execute(
            select(outbox_event_table.c.storage_state).where(
                outbox_event_table.c.event_id == "evt_1"
            )
        ).scalar_one()
        assert state == "full"


def test_compaction_suppression_receipt_occurred_at_joins_event_time() -> None:
    from app.outbox.compaction import canonical_receipt_fingerprint

    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    event_occurred = datetime(2026, 1, 1, tzinfo=UTC)
    publish(engine, user_ids=(alice,), event_id="evt_1")
    with engine.begin() as connection:
        connection.execute(
            update(outbox_event_table)
            .where(outbox_event_table.c.event_id == "evt_1")
            .values(occurred_at_utc=event_occurred)
        )
        connection.execute(
            update(identity_user_table)
            .where(identity_user_table.c.id == alice)
            .values(lifecycle_status="pending_delete")
        )
    dispatcher = make_dispatcher(engine)
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
    with engine.begin() as connection:
        connection.execute(
            update(outbox_event_table)
            .where(outbox_event_table.c.event_id == "evt_1")
            .values(compact_after_at_utc=datetime(2000, 1, 1, tzinfo=UTC))
        )
    dispatcher.compact_due_events(now=datetime(2026, 8, 5, tzinfo=UTC))
    with engine.connect() as connection:
        receipt = (
            connection.execute(
                select(notification_delivery_receipt_table).where(
                    notification_delivery_receipt_table.c.event_id == "evt_1"
                )
            )
            .mappings()
            .one()
        )
        expected = canonical_receipt_fingerprint("evt_1", alice, "recipient_inactive", None)
        assert receipt["fingerprint"] == expected
        assert as_utc(receipt["occurred_at_utc"]) == event_occurred


def as_utc(value):
    return value if value.tzinfo else value.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# E. Compaction requires the retirement receipt to match user+deletion
# ---------------------------------------------------------------------------


def test_compaction_with_a_different_deletion_id_is_409() -> None:
    from app.outbox.ports import EligibleAccountEventCompactionCommand

    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    dispatcher = make_dispatcher(engine)
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
    lifecycle = make_lifecycle(engine)
    mark_deletable(engine, alice)
    with engine.begin() as connection:
        lifecycle.retire_account_notification_state(
            retire_command(user_id=alice), connection=connection
        )

    base = dict(
        operation_id="op_comp_1",
        caller_principal="retention-ops",
        user_id=alice,
        deletion_id="del_OTHER",  # does not match the retirement's deletion
        retirement_receipt_id="op_ret_1",
        retirement_receipt_fingerprint=_command_input_fingerprint(retire_command(user_id=alice)),
        transaction_id="tx_comp_1",
        canonical_input_fingerprint="unused",
        capability_token=retention_token(),
    )

    def request():
        with engine.begin() as connection:
            return lifecycle.request_eligible_account_event_compaction(
                EligibleAccountEventCompactionCommand(**base), connection=connection
            )

    with pytest.raises(PlatformError) as raised:
        request()
    assert raised.value.code == "compaction_prerequisite_missing"
    assert raised.value.status_code == 409
