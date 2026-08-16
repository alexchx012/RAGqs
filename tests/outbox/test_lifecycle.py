"""Typed lifecycle ports: document redaction, account retirement, compaction."""

from __future__ import annotations

import pytest
from _helpers import (
    build_engine,
    build_identity_service,
    fixed_now,
    make_publisher,
    provision_user,
)
from sqlalchemy import select, update

from app.outbox.dispatcher import OutboxDispatcher
from app.outbox.lifecycle import (
    SqlAlchemyOutboxLifecycle,
    _command_input_fingerprint,
)
from app.outbox.notifications import NotificationMaterializer
from app.outbox.schema import (
    notification_delivery_receipt_table,
    notification_inbox_table,
    notification_table,
    outbox_account_retirement_tombstone_table,
    outbox_event_table,
    outbox_redaction_receipt_table,
)
from app.platform.errors import PlatformError


class _AcceptingArchiveVerifier:
    def verify_archive(self, *, archive_ref: str, checksum: str, **kwargs) -> bool:
        del archive_ref, checksum
        return True


def mark_account_deletable(engine, user_id: str) -> None:
    from app.identity.schema import identity_user_table

    with engine.begin() as connection:
        connection.execute(
            update(identity_user_table)
            .where(identity_user_table.c.id == user_id)
            .values(lifecycle_status="pending_delete")
        )


def make_lifecycle(engine):
    return SqlAlchemyOutboxLifecycle(
        engine,
        now=lambda: fixed_now(),
        archive_verifier=_AcceptingArchiveVerifier(),
    )


def deliver(engine, *, user_ids, event_id="evt_1", event_type="ingestion_completed"):
    publisher = make_publisher(engine, now=lambda: fixed_now())
    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    payload = (
        {
            "job_id": f"job_{event_id}",
            "document_id": f"doc_{event_id}",
            "document_version_id": f"docv_{event_id}",
            "publication_id": f"pub_{event_id}",
        }
        if event_type in {"ingestion_completed", "ocr_low_confidence"}
        else {"submission_id": f"sub_{event_id}"}
    )
    command = OutboxPublishCommand(
        event_id=event_id,
        caller_principal="ingestion" if event_type.startswith("ingestion") else "submissions",
        event_type=event_type,
        schema_version=1,
        aggregate_type=(
            "ingestion_job" if event_type.startswith("ingestion") else "knowledge_submission"
        ),
        aggregate_id=f"job_{event_id}",
        transition_version=1,
        occurred_at=fixed_now(),
        payload=payload,
        trace_id="trace_x",
        recipients=tuple(RecipientSelection(recipient_user_id=u) for u in user_ids),
    )
    with engine.begin() as connection:
        publisher.publish(command, connection=connection)
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


def redact_command(
    *,
    operation_id="op_1",
    deletion_id="del_1",
    document_id="doc_evt_1",
    document_version_ids=("docv_evt_1",),
):
    from app.outbox.ports import DocumentNotificationRedactionCommand

    return DocumentNotificationRedactionCommand(
        operation_id=operation_id,
        caller_principal="documents",
        deletion_id=deletion_id,
        document_id=document_id,
        document_version_ids=document_version_ids,
        reason="document_pending_delete",
        transaction_id="tx_1",
        mode="inline",
        canonical_input_fingerprint="fp_1",
    )


def retire_command(
    *,
    operation_id="op_ret_1",
    mode="durable",
    user_id="user_x",
    caller_principal="retention-ops",
):
    from app.outbox.ports import AccountNotificationRetirementCommand

    return AccountNotificationRetirementCommand(
        operation_id=operation_id,
        caller_principal=caller_principal,
        user_id=user_id,
        deletion_id="del_ret_1",
        verified_archive_ref="archive_ref_1",
        archive_checksum="checksum_1",
        transaction_id="tx_ret_1",
        mode=mode,
        canonical_input_fingerprint="fp_ret_1",
    )


def compact_command(
    *, operation_id="op_comp_1", retirement_receipt_id="op_ret_1", user_id="user_x"
):
    from app.outbox.ports import EligibleAccountEventCompactionCommand

    return EligibleAccountEventCompactionCommand(
        operation_id=operation_id,
        caller_principal="retention-ops",
        user_id=user_id,
        deletion_id="del_ret_1",
        retirement_receipt_id=retirement_receipt_id,
        retirement_receipt_fingerprint=_command_input_fingerprint(
            retire_command(user_id=user_id, mode="inline")
        ),
        transaction_id="tx_comp_1",
        canonical_input_fingerprint="fp_comp_1",
    )


def test_redaction_requires_the_documents_caller() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    lifecycle = make_lifecycle(engine)
    command = redact_command()
    command = command.__class__(
        operation_id=command.operation_id,
        caller_principal="retention-ops",
        deletion_id=command.deletion_id,
        document_id=command.document_id,
        document_version_ids=command.document_version_ids,
        reason=command.reason,
        transaction_id=command.transaction_id,
        mode=command.mode,
        canonical_input_fingerprint=command.canonical_input_fingerprint,
    )

    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            lifecycle.redact_document_notifications(command, connection=connection)
    assert raised.value.status_code == 403


def test_redaction_replaces_document_derived_fields_and_writes_a_receipt() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    deliver(engine, user_ids=(alice,), event_id="evt_2")
    lifecycle = make_lifecycle(engine)

    receipt = None
    with engine.begin() as connection:
        receipt = lifecycle.redact_document_notifications(
            redact_command(document_id="doc_evt_1"), connection=connection
        )

    assert receipt.state == "completed"
    assert receipt.redacted_notification_count == 1
    assert receipt.already_redacted_count == 0
    assert receipt.retryable is False
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(notification_table).where(notification_table.c.document_id == "doc_evt_1")
            )
            .mappings()
            .all()
        )
        assert len(rows) == 1
        assert rows[0]["title"] == "Deleted document"
        assert rows[0]["redacted"] is True
        # Opaque identifiers and facts survive.
        assert rows[0]["document_id"] == "doc_evt_1"
        assert rows[0]["document_version_id"] == "docv_evt_1"
        assert rows[0]["notification_seq"] == 1
        assert rows[0]["read_at_utc"] is None
        stored = (
            connection.execute(
                select(outbox_redaction_receipt_table).where(
                    outbox_redaction_receipt_table.c.operation_id == "op_1"
                )
            )
            .mappings()
            .one()
        )
        assert stored["state"] == "completed"
        assert stored["redacted_notification_count"] == 1


def test_redaction_receipt_is_idempotent_for_the_same_operation() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    lifecycle = make_lifecycle(engine)

    with engine.begin() as connection:
        first = lifecycle.redact_document_notifications(redact_command(), connection=connection)
    with engine.begin() as connection:
        second = lifecycle.redact_document_notifications(redact_command(), connection=connection)

    assert first.redacted_notification_count == 1
    # Same operation/input returns the original receipt unchanged.
    assert second.redacted_notification_count == 1
    assert second.already_redacted_count == 0
    assert second.operation_id == "op_1"


def test_redaction_conflicting_operation_is_permanent_conflict() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    lifecycle = make_lifecycle(engine)

    with engine.begin() as connection:
        lifecycle.redact_document_notifications(redact_command(), connection=connection)
    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            lifecycle.redact_document_notifications(
                redact_command(deletion_id="del_OTHER"), connection=connection
            )
    assert raised.value.status_code == 409


def test_retirement_writes_receipts_removes_notifications_and_inbox() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    deliver(engine, user_ids=(alice,), event_id="evt_2")
    lifecycle = make_lifecycle(engine)
    command = retire_command(user_id=alice, mode="inline")

    receipt = None
    with engine.begin() as connection:
        mark_account_deletable(engine, alice)
        receipt = lifecycle.retire_account_notification_state(command, connection=connection)

    assert receipt.state in {"accepted", "completed"}
    assert receipt.notification_retired_count == 2
    assert receipt.inbox_removed is True
    with engine.connect() as connection:
        assert connection.execute(select(notification_table)).all() == []
        # The inbox row is removed; the permanent tombstone keeps the watermark.
        assert (
            connection.execute(
                select(notification_inbox_table).where(
                    notification_inbox_table.c.recipient_user_id == alice
                )
            ).all()
            == []
        )
        tombstone = (
            connection.execute(
                select(outbox_account_retirement_tombstone_table).where(
                    outbox_account_retirement_tombstone_table.c.recipient_user_id == alice
                )
            )
            .mappings()
            .one()
        )
        assert tombstone["next_notification_seq"] >= 1
        receipts = (
            connection.execute(
                select(notification_delivery_receipt_table).where(
                    notification_delivery_receipt_table.c.recipient_user_id == alice
                )
            )
            .mappings()
            .all()
        )
        assert len(receipts) == 2
        assert {r["outcome"] for r in receipts} == {"materialized"}


def test_retirement_is_idempotent_and_requires_retention_ops() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    lifecycle = make_lifecycle(engine)

    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            mark_account_deletable(engine, alice)
            lifecycle.retire_account_notification_state(
                retire_command(
                    user_id=alice,
                    operation_id="op_ret_2",
                    caller_principal="documents",
                ),
                connection=connection,
            )
    assert raised.value.status_code == 403

    with engine.begin() as connection:
        mark_account_deletable(engine, alice)
        first = lifecycle.retire_account_notification_state(
            retire_command(user_id=alice, operation_id="op_ret_3", mode="inline"),
            connection=connection,
        )
    with engine.begin() as connection:
        mark_account_deletable(engine, alice)
        second = lifecycle.retire_account_notification_state(
            retire_command(user_id=alice, operation_id="op_ret_3", mode="inline"),
            connection=connection,
        )
    assert first.notification_retired_count == second.notification_retired_count
    assert second.inbox_removed is True


def test_compaction_requires_a_completed_retirement_receipt() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    lifecycle = make_lifecycle(engine)

    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            lifecycle.request_eligible_account_event_compaction(
                compact_command(), connection=connection
            )
    assert raised.value.status_code == 409


def test_compaction_compacts_only_fully_delivered_events() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    # evt_2 is published but its delivery never reaches delivered.
    publisher = make_publisher(engine, now=lambda: fixed_now())
    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    with engine.begin() as connection:
        publisher.publish(
            OutboxPublishCommand(
                event_id="evt_2",
                caller_principal="ingestion",
                event_type="ingestion_completed",
                schema_version=1,
                aggregate_type="ingestion_job",
                aggregate_id="job_evt_2",
                transition_version=1,
                occurred_at=fixed_now(),
                payload={
                    "job_id": "job_evt_2",
                    "document_id": "doc_evt_2",
                    "document_version_id": "docv_evt_2",
                    "publication_id": "pub_evt_2",
                },
                trace_id="trace_y",
                recipients=(RecipientSelection(recipient_user_id=alice),),
            ),
            connection=connection,
        )
    lifecycle = make_lifecycle(engine)
    with engine.begin() as connection:
        mark_account_deletable(engine, alice)
        lifecycle.retire_account_notification_state(
            retire_command(user_id=alice, mode="inline"), connection=connection
        )

    receipt = None
    with engine.begin() as connection:
        receipt = lifecycle.request_eligible_account_event_compaction(
            compact_command(user_id=alice), connection=connection
        )

    assert receipt.state in {"accepted", "completed"}
    assert receipt.compacted_count >= 1
    with engine.connect() as connection:
        evt_1 = connection.execute(
            select(outbox_event_table.c.storage_state).where(
                outbox_event_table.c.event_id == "evt_1"
            )
        ).scalar_one()
        evt_2 = connection.execute(
            select(outbox_event_table.c.storage_state).where(
                outbox_event_table.c.event_id == "evt_2"
            )
        ).scalar_one()
        assert evt_1 == "compacted"
        # Non-terminal deliveries stay full and under normal dispatcher policy.
        assert evt_2 == "full"
