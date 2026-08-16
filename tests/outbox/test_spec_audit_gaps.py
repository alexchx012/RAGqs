"""Spec audit gaps: redaction scope conflicts, redaction/replay race, retired
ack 204, lifecycle transaction rollback and compaction replay."""

from __future__ import annotations

from datetime import UTC, datetime

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
from app.outbox.lifecycle import SqlAlchemyOutboxLifecycle
from app.outbox.notifications import NotificationMaterializer
from app.outbox.schema import (
    notification_table,
    outbox_redaction_receipt_table,
)
from app.outbox.service import NotificationService
from app.platform.errors import PlatformError


def make_lifecycle(engine):
    return SqlAlchemyOutboxLifecycle(
        engine,
        now=lambda: fixed_now(),
        archive_verifier=_AcceptingArchiveVerifier(),
    )


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


def deliver(engine, *, user_ids, event_id="evt_1", materialize=True):
    from app.outbox.metrics import SqlAlchemyOutboxMetrics
    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    publisher = make_publisher(engine, now=lambda: fixed_now())
    command = OutboxPublishCommand(
        event_id=event_id,
        event_type="ingestion_completed",
        caller_principal="ingestion",
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
    if not materialize:
        return
    dispatcher = OutboxDispatcher(
        engine,
        consumers={"in_app_notification": NotificationMaterializer(engine)},
        now=lambda: fixed_now(),
        retention_days=30,
        notification_retention_days=90,
        metrics=SqlAlchemyOutboxMetrics(),
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


def test_redaction_conflicting_document_scope_is_permanent_conflict() -> None:
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
                redact_command(document_id="doc_OTHER"), connection=connection
            )
    assert raised.value.status_code == 409


def test_redaction_conflicting_version_scope_is_permanent_conflict() -> None:
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
                redact_command(document_version_ids=("docv_OTHER",)), connection=connection
            )
    assert raised.value.status_code == 409


def test_redaction_rolls_back_with_the_deletion_transaction() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    lifecycle = make_lifecycle(engine)

    with pytest.raises(RuntimeError, match="delete failed"):
        with engine.begin() as connection:
            # The reservation row participates in the caller transaction (no
            # savepoint): an outer rollback must undo the reservation, the
            # redaction and the tombstone on every dialect.
            lifecycle.redact_document_notifications(redact_command(), connection=connection)
            raise RuntimeError("delete failed")

    # Nothing was committed: notifications keep their original rendering and
    # no redaction receipt exists.
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(notification_table).where(notification_table.c.event_id == "evt_1")
            )
            .mappings()
            .one()
        )
        assert row["title"] == "Document ingestion completed"
        assert row["redacted"] is False
        assert (
            connection.execute(
                select(outbox_redaction_receipt_table).where(
                    outbox_redaction_receipt_table.c.operation_id == "op_1"
                )
            ).all()
            == []
        )


def test_retirement_inline_rolls_back_with_the_caller_transaction() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    lifecycle = make_lifecycle(engine)
    from app.outbox.ports import AccountNotificationRetirementCommand

    command = AccountNotificationRetirementCommand(
        operation_id="op_ret_rollback",
        caller_principal="retention-ops",
        user_id=alice,
        deletion_id="del_1",
        verified_archive_ref="archive_ref_1",
        archive_checksum="checksum_1",
        transaction_id="tx_1",
        mode="inline",
        canonical_input_fingerprint="fp_1",
    )

    with pytest.raises(RuntimeError, match="retire failed"):
        with engine.begin() as connection:
            mark_account_deletable(engine, alice)
            lifecycle.retire_account_notification_state(command, connection=connection)
            raise RuntimeError("retire failed")

    with engine.connect() as connection:
        assert len(connection.execute(select(notification_table)).all()) == 1


def test_archive_proof_mismatch_is_a_permanent_422() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    lifecycle = make_lifecycle(engine)
    from app.outbox.ports import AccountNotificationRetirementCommand

    command = AccountNotificationRetirementCommand(
        operation_id="op_ret_proof",
        caller_principal="retention-ops",
        user_id=alice,
        deletion_id="del_1",
        verified_archive_ref="archive_ref_1",
        archive_checksum="checksum_1",
        transaction_id="tx_1",
        mode="durable",
        canonical_input_fingerprint="fp_1",
    )
    with engine.begin() as connection:
        mark_account_deletable(engine, alice)
        lifecycle.retire_account_notification_state(command, connection=connection)

    # The worker/retention retries with a substituted, unverified archive ref.
    forged = AccountNotificationRetirementCommand(
        operation_id="op_ret_proof",
        caller_principal="retention-ops",
        user_id=alice,
        deletion_id="del_1",
        verified_archive_ref="archive_ref_FORGED",
        archive_checksum="checksum_FORGED",
        transaction_id="tx_1",
        mode="durable",
        canonical_input_fingerprint="fp_1",
    )
    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            mark_account_deletable(engine, alice)
            lifecycle.apply_durable_retirement(forged, connection=connection)
    assert raised.value.code == "archive_proof_mismatch"
    assert raised.value.status_code == 422


def test_ack_on_a_retired_materialized_notification_is_204() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    service = NotificationService(engine, now=lambda: fixed_now())
    # Retire the notification through the regular maintenance path.
    from app.outbox.maintenance import NotificationRetentionMaintenance

    with engine.begin() as connection:
        connection.execute(
            update(notification_table)
            .where(notification_table.c.event_id == "evt_1")
            .values(retire_after_at_utc=datetime(2000, 1, 1, tzinfo=UTC))
        )
    NotificationRetentionMaintenance(engine, now=lambda: fixed_now()).run_once()

    # Evidence now comes from the permanent receipt: ack succeeds with 204 and
    # never rebuilds an ack row (spec: retired materialized receipt -> no-op).
    service.ack_event(alice, "evt_1")
    with engine.connect() as connection:
        from app.outbox.schema import notification_context_ack_table

        ack = connection.execute(
            select(notification_context_ack_table).where(
                notification_context_ack_table.c.event_id == "evt_1"
            )
        ).all()
        assert ack == []


def test_ack_for_a_suppressed_recipient_is_404() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    # Publish for alice but suppress her before materialization.
    from sqlalchemy import update as sa_update

    from app.outbox.dispatcher import OutboxDispatcher
    from app.outbox.metrics import SqlAlchemyOutboxMetrics
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
        from app.identity.schema import identity_user_table

        connection.execute(
            sa_update(identity_user_table)
            .where(identity_user_table.c.id == alice)
            .values(lifecycle_status="pending_delete")
        )
    dispatcher = OutboxDispatcher(
        engine,
        consumers={"in_app_notification": NotificationMaterializer(engine)},
        now=lambda: fixed_now(),
        retention_days=30,
        notification_retention_days=90,
        metrics=SqlAlchemyOutboxMetrics(),
    )
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")

    service = NotificationService(engine, now=lambda: fixed_now())
    with pytest.raises(PlatformError) as raised:
        service.ack_event(alice, "evt_1")
    assert raised.value.status_code == 404


def test_redaction_then_materialization_never_restores_original_text() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    # Event published but not yet materialized.
    deliver(engine, user_ids=(alice,), event_id="evt_1", materialize=False)
    lifecycle = make_lifecycle(engine)
    with engine.begin() as connection:
        lifecycle.redact_document_notifications(redact_command(), connection=connection)

    # A later claim/materialization (or replay) must render the fixed
    # deleted-document text and never restore filename/title/snippet.
    from app.outbox.metrics import SqlAlchemyOutboxMetrics

    dispatcher = OutboxDispatcher(
        engine,
        consumers={"in_app_notification": NotificationMaterializer(engine)},
        now=lambda: fixed_now(),
        retention_days=30,
        notification_retention_days=90,
        metrics=SqlAlchemyOutboxMetrics(),
    )
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
        # Only opaque identifiers remain in the payload.
        assert row["payload_json"] == {}
        assert row["document_id"] == "doc_evt_1"
        assert row["document_version_id"] == "docv_evt_1"


def test_compaction_same_operation_replay_returns_the_original_receipt() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    lifecycle = make_lifecycle(engine)
    from app.outbox.lifecycle import _command_input_fingerprint
    from app.outbox.ports import AccountNotificationRetirementCommand

    retire = AccountNotificationRetirementCommand(
        operation_id="op_ret_replay",
        caller_principal="retention-ops",
        user_id=alice,
        deletion_id="del_1",
        verified_archive_ref="archive_ref_1",
        archive_checksum="checksum_1",
        transaction_id="tx_1",
        mode="inline",
        canonical_input_fingerprint="fp_1",
    )
    with engine.begin() as connection:
        mark_account_deletable(engine, alice)
        lifecycle.retire_account_notification_state(retire, connection=connection)

    from app.outbox.ports import EligibleAccountEventCompactionCommand

    compact = EligibleAccountEventCompactionCommand(
        operation_id="op_comp_replay",
        caller_principal="retention-ops",
        user_id=alice,
        deletion_id="del_1",
        retirement_receipt_id="op_ret_replay",
        retirement_receipt_fingerprint=_command_input_fingerprint(retire),
        transaction_id="tx_2",
        canonical_input_fingerprint="fp_2",
    )
    with engine.begin() as connection:
        first = lifecycle.request_eligible_account_event_compaction(compact, connection=connection)
    with engine.begin() as connection:
        second = lifecycle.request_eligible_account_event_compaction(compact, connection=connection)

    assert first.compacted_count == 1
    assert second.compacted_count == 1
    assert second.state == "completed"
