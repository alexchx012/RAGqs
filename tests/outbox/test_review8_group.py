"""Final review round: unforgeable signed capability tokens, reservation-first
lifecycle idempotency, retirement receipt verification with event-time joins,
and compaction retirement-fingerprint binding."""

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
    retention_redaction_token,
    retention_token,
)
from sqlalchemy import select, update

from app.identity.schema import identity_user_table
from app.outbox.capabilities import sign_token, verify_token
from app.outbox.dispatcher import OutboxDispatcher
from app.outbox.lifecycle import (
    SqlAlchemyOutboxLifecycle,
    _command_input_fingerprint,
)
from app.outbox.metrics import SqlAlchemyOutboxMetrics
from app.outbox.notifications import NotificationMaterializer
from app.outbox.publisher import SqlAlchemyOutboxPublisher
from app.outbox.schema import (
    notification_delivery_receipt_table,
    notification_table,
    outbox_account_retirement_tombstone_table,
    outbox_document_tombstone_table,
    outbox_event_table,
    outbox_redaction_receipt_table,
)
from app.platform.errors import PlatformError


class _AcceptingArchiveVerifier:
    def verify_archive(self, *, archive_ref: str, checksum: str, **kwargs) -> bool:
        del archive_ref, checksum
        return True


def make_lifecycle(engine):
    return SqlAlchemyOutboxLifecycle(
        engine,
        now=lambda: fixed_now(),
        archive_verifier=_AcceptingArchiveVerifier(),
        capability_secret=CAPABILITY_SECRET,
    )


def make_dispatcher(engine):
    return OutboxDispatcher(
        engine,
        consumers={"in_app_notification": NotificationMaterializer(engine)},
        now=lambda: fixed_now(),
        retention_days=30,
        notification_retention_days=90,
        metrics=SqlAlchemyOutboxMetrics(),
    )


def publish(engine, *, user_ids, event_id="evt_1"):
    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    publisher = make_publisher(engine, now=lambda: fixed_now())
    command = OutboxPublishCommand(
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
        trace_id="trace_x",
        recipients=tuple(RecipientSelection(recipient_user_id=u) for u in user_ids),
    )
    with engine.begin() as connection:
        publisher.publish(command, connection=connection)


def deliver(engine, *, user_ids=None, event_id="evt_1"):
    del user_ids, event_id
    dispatcher = make_dispatcher(engine)
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")


def mark_deletable(engine, user_id: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            update(identity_user_table)
            .where(identity_user_table.c.id == user_id)
            .values(lifecycle_status="pending_delete")
        )


def redact_command(
    *,
    operation_id="op_r8",
    deletion_id="del_1",
    document_id="doc_evt_1",
    document_version_ids=("docv_evt_1",),
    transaction_id="documents-delete:tx_1",
    caller_principal="documents",
    capability_token=None,
):
    from app.outbox.ports import DocumentNotificationRedactionCommand

    return DocumentNotificationRedactionCommand(
        operation_id=operation_id,
        caller_principal=caller_principal,
        deletion_id=deletion_id,
        document_id=document_id,
        document_version_ids=document_version_ids,
        reason="document_pending_delete",
        transaction_id=transaction_id,
        mode="inline",
        canonical_input_fingerprint="unused",
        capability_token=capability_token
        or docs_redaction_token(deletion_id=deletion_id, transaction_id=transaction_id),
    )


def retire_command(
    *,
    operation_id="op_ret_r8",
    user_id="user_x",
    deletion_id="del_ret_1",
    mode="inline",
):
    from app.outbox.ports import AccountNotificationRetirementCommand

    return AccountNotificationRetirementCommand(
        operation_id=operation_id,
        caller_principal="retention-ops",
        user_id=user_id,
        deletion_id=deletion_id,
        verified_archive_ref="archive_ref_1",
        archive_checksum="checksum_1",
        transaction_id="tx_ret_1",
        mode=mode,
        canonical_input_fingerprint="unused",
        capability_token=retention_token(),
    )


def compact_command(
    *,
    operation_id="op_comp_r8",
    user_id="user_x",
    retirement_receipt_id="op_ret_r8",
    retirement_receipt_fingerprint=None,
):
    from app.outbox.ports import EligibleAccountEventCompactionCommand

    return EligibleAccountEventCompactionCommand(
        operation_id=operation_id,
        caller_principal="retention-ops",
        user_id=user_id,
        deletion_id="del_ret_1",
        retirement_receipt_id=retirement_receipt_id,
        retirement_receipt_fingerprint=retirement_receipt_fingerprint
        or _command_input_fingerprint(retire_command(user_id=user_id)),
        transaction_id="tx_comp_1",
        canonical_input_fingerprint="unused",
        capability_token=retention_token(),
    )


# ---------------------------------------------------------------------------
# 1. Producer/lifecycle capability boundary
# ---------------------------------------------------------------------------


def test_producer_token_with_same_claims_but_forged_signature_is_403() -> None:
    """A token carrying the same claims signed with the WRONG secret is forged:
    the publisher must reject it even though every field matches."""
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())
    forged = sign_token(
        b"attacker-secret",
        kind="producer",
        principal="ingestion",
        scope={"event_types": ["ingestion_completed"]},
    )

    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    command = OutboxPublishCommand(
        capability=forged,
        event_id="evt_forged",
        caller_principal="ingestion",
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
            publisher.publish(command, connection=connection)
    assert raised.value.code == "producer_not_authorized"
    assert raised.value.status_code == 403


def test_retention_ops_with_documents_issued_inline_capability_may_redact() -> None:
    """Retention-ops holding a documents-issued inline transaction capability
    for the exact deletion/transaction is an authorized redaction caller."""
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    lifecycle = make_lifecycle(engine)

    command = redact_command(
        caller_principal="retention-ops",
        capability_token=retention_redaction_token(
            deletion_id="del_1", transaction_id="documents-delete:tx_1"
        ),
    )
    with engine.begin() as connection:
        receipt = lifecycle.redact_document_notifications(command, connection=connection)
    assert receipt.state == "completed"
    assert receipt.redacted_notification_count == 1
    with engine.connect() as connection:
        rows = connection.execute(
            select(notification_table).where(notification_table.c.document_id == "doc_evt_1")
        ).all()
        assert len(rows) == 1


def test_retention_ops_with_documents_token_is_still_403() -> None:
    """A documents token never authorizes retention-ops (principal mismatch)."""
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    lifecycle = make_lifecycle(engine)

    command = redact_command(
        caller_principal="retention-ops",
        capability_token=docs_redaction_token(
            deletion_id="del_1", transaction_id="documents-delete:tx_1"
        ),
    )
    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            lifecycle.redact_document_notifications(command, connection=connection)
    assert raised.value.status_code == 403


def test_documents_with_forged_lifecycle_token_is_403() -> None:
    """A lifecycle token signed with the wrong secret fails closed."""
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    lifecycle = make_lifecycle(engine)

    forged = sign_token(
        b"attacker-secret",
        kind="documents_redact",
        principal="documents",
        scope={"deletion_id": "del_1", "transaction_id": "documents-delete:tx_1", "mode": "inline"},
    )
    command = redact_command(capability_token=forged)
    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            lifecycle.redact_document_notifications(command, connection=connection)
    assert raised.value.status_code == 403


def test_lifecycle_without_configured_secret_fails_closed() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    lifecycle = SqlAlchemyOutboxLifecycle(
        engine,
        now=lambda: fixed_now(),
        archive_verifier=_AcceptingArchiveVerifier(),
        capability_secret=None,
    )
    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            lifecycle.redact_document_notifications(redact_command(), connection=connection)
    assert raised.value.status_code == 403


def test_publisher_without_configured_secret_fails_closed() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    # A publisher whose signing secret differs from the token's issuer treats
    # the token as forged: signature verification fails closed.
    publisher = SqlAlchemyOutboxPublisher(
        engine, now=lambda: fixed_now(), capability_secret=b"other-secret"
    )
    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    command = OutboxPublishCommand(
        capability=cap("ingestion"),
        event_id="evt_nosecret",
        caller_principal="ingestion",
        event_type="ingestion_completed",
        schema_version=1,
        aggregate_type="ingestion_job",
        aggregate_id="job_nosecret",
        transition_version=1,
        occurred_at=fixed_now(),
        payload={
            "job_id": "job_nosecret",
            "document_id": "doc_nosecret",
            "document_version_id": "docv_nosecret",
            "publication_id": "pub_nosecret",
        },
        trace_id="t",
        recipients=(RecipientSelection(recipient_user_id=alice),),
    )
    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            publisher.publish(command, connection=connection)
    assert raised.value.code == "producer_not_authorized"
    assert raised.value.status_code == 403


def test_runtime_does_not_expose_the_raw_producer_registry() -> None:
    from _helpers import make_settings

    from app.platform.runtime import build_runtime

    engine = build_engine()
    runtime = build_runtime(make_settings(), adapters={"database_engine": engine})
    # The raw registry is NOT a business interface; the master secret and the
    # issuer are assembly-time internals and never resolvable adapters. The
    # retention token is stored PRIVATELY and is never resolvable as a generic
    # adapter either.
    assert runtime.resolve("producer_capabilities", None) is None
    assert runtime.resolve("retention_capability_token", None) is None
    assert "_retention_capability_token" not in runtime.adapters
    assert runtime.resolve("retirement_worker") is not None
    runtime.close()


def test_verify_token_rejects_malformed_and_wrong_domain_tokens() -> None:
    assert verify_token(CAPABILITY_SECRET, "") is None
    assert verify_token(CAPABILITY_SECRET, "not-a-token") is None
    assert verify_token(CAPABILITY_SECRET, "v1.xx.yy") is None
    assert (
        verify_token(
            CAPABILITY_SECRET,
            sign_token(b"other", kind="producer", principal="ingestion"),
        )
        is None
    )


# ---------------------------------------------------------------------------
# 2. Reservation-first idempotency: losers return the winner's real receipt
# ---------------------------------------------------------------------------


def test_redaction_same_operation_replay_returns_original_counts() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    # A second event sharing the same document/version must also be redacted.
    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    publisher = make_publisher(engine, now=lambda: fixed_now())
    with engine.begin() as connection:
        publisher.publish(
            OutboxPublishCommand(
                capability=cap("ingestion"),
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
                    "document_id": "doc_evt_1",
                    "document_version_id": "docv_evt_1",
                    "publication_id": "pub_evt_2",
                },
                trace_id="trace_y",
                recipients=(RecipientSelection(recipient_user_id=alice),),
            ),
            connection=connection,
        )
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    deliver(engine, user_ids=(alice,), event_id="evt_2")
    lifecycle = make_lifecycle(engine)

    with engine.begin() as connection:
        first = lifecycle.redact_document_notifications(redact_command(), connection=connection)
    with engine.begin() as connection:
        second = lifecycle.redact_document_notifications(redact_command(), connection=connection)
    assert first.redacted_notification_count == 2
    # The replay returns the WINNER's real receipt, not a zeroed replay.
    assert second.redacted_notification_count == 2
    assert second.already_redacted_count == 0
    with engine.connect() as connection:
        tombstones = connection.execute(
            select(outbox_document_tombstone_table).where(
                outbox_document_tombstone_table.c.document_id == "doc_evt_1"
            )
        ).all()
        assert len(tombstones) == 1
        receipts = connection.execute(select(outbox_redaction_receipt_table)).all()
        assert len(receipts) == 1


def test_retirement_same_operation_replay_returns_original_counts() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    publish(engine, user_ids=(alice,), event_id="evt_2")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    deliver(engine, user_ids=(alice,), event_id="evt_2")
    lifecycle = make_lifecycle(engine)
    command = retire_command(user_id=alice)

    with engine.begin() as connection:
        mark_deletable(engine, alice)
        first = lifecycle.retire_account_notification_state(command, connection=connection)
    with engine.begin() as connection:
        second = lifecycle.retire_account_notification_state(command, connection=connection)
    assert first.notification_retired_count == 2
    # The replay returns the winner's real receipt counts.
    assert second.notification_retired_count == 2
    assert second.receipt_count == 2
    with engine.connect() as connection:
        receipts = connection.execute(
            select(notification_delivery_receipt_table).where(
                notification_delivery_receipt_table.c.recipient_user_id == alice
            )
        ).all()
        assert len(receipts) == 2


def test_retirement_same_operation_different_input_is_409() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    lifecycle = make_lifecycle(engine)

    with engine.begin() as connection:
        mark_deletable(engine, alice)
        lifecycle.retire_account_notification_state(
            retire_command(user_id=alice), connection=connection
        )
    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            lifecycle.retire_account_notification_state(
                retire_command(user_id=alice, deletion_id="del_OTHER"),
                connection=connection,
            )
    assert raised.value.code == "idempotency_key_conflict"
    assert raised.value.status_code == 409


# ---------------------------------------------------------------------------
# 4. Retirement receipt verification and event-time joins
# ---------------------------------------------------------------------------


def test_retirement_tampered_materialized_receipt_rolls_back_deletion() -> None:
    """An existing materialized receipt whose facts disagree with the
    notification is an invariant violation: retirement raises and deletes
    nothing."""
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    # Insert a receipt with the WRONG occurred_at fact.
    with engine.begin() as connection:
        connection.execute(
            notification_delivery_receipt_table.insert().values(
                event_id="evt_1",
                recipient_user_id=alice,
                outcome="materialized",
                original_notification_seq=1,
                occurred_at_utc=datetime(2020, 1, 1, tzinfo=UTC),
                materialized_at_utc=fixed_now(),
                retired_at_utc=fixed_now(),
                fingerprint="tampered",
            )
        )
    lifecycle = make_lifecycle(engine)
    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            mark_deletable(engine, alice)
            lifecycle.retire_account_notification_state(
                retire_command(user_id=alice), connection=connection
            )
    assert raised.value.code == "receipt_fingerprint_mismatch"
    with engine.connect() as connection:
        # The notification and inbox survive the failed retirement.
        assert (
            connection.execute(
                select(notification_table).where(notification_table.c.recipient_user_id == alice)
            ).all()
            != []
        )
        assert (
            connection.execute(
                select(outbox_account_retirement_tombstone_table).where(
                    outbox_account_retirement_tombstone_table.c.recipient_user_id == alice
                )
            ).all()
            == []
        )


def test_retirement_suppression_receipt_occurred_at_joins_event_time() -> None:
    """T1 (event occurred_at) differs from T2 (suppressed_at): the permanent
    receipt must carry the EVENT time, never the suppression time."""
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
        # Suppress: alice is no longer active, so materialization writes a
        # suppression at fixed_now() (2026-08-05) — different from T1.
        connection.execute(
            update(identity_user_table)
            .where(identity_user_table.c.id == alice)
            .values(lifecycle_status="pending_delete")
        )
    dispatcher = make_dispatcher(engine)
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
    lifecycle = make_lifecycle(engine)
    with engine.begin() as connection:
        mark_deletable(engine, alice)
        lifecycle.retire_account_notification_state(
            retire_command(user_id=alice), connection=connection
        )
    with engine.connect() as connection:
        receipt = (
            connection.execute(
                select(
                    notification_delivery_receipt_table.c.occurred_at_utc,
                    notification_delivery_receipt_table.c.fingerprint,
                    notification_delivery_receipt_table.c.outcome,
                ).where(
                    notification_delivery_receipt_table.c.event_id == "evt_1",
                    notification_delivery_receipt_table.c.recipient_user_id == alice,
                )
            )
            .mappings()
            .one()
        )
        received = receipt["occurred_at_utc"]
        received = received.replace(tzinfo=UTC) if received.tzinfo is None else received
        assert received == event_occurred
        assert receipt["outcome"] == "recipient_inactive"
        expected = canonical_receipt_fingerprint("evt_1", alice, "recipient_inactive", None)
        assert receipt["fingerprint"] == expected


def test_retirement_tampered_suppression_receipt_rolls_back_deletion() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
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
    # A receipt claiming materialized for a suppressed recipient is a mismatch.
    with engine.begin() as connection:
        connection.execute(
            notification_delivery_receipt_table.insert().values(
                event_id="evt_1",
                recipient_user_id=alice,
                outcome="materialized",
                original_notification_seq=7,
                occurred_at_utc=fixed_now(),
                materialized_at_utc=fixed_now(),
                retired_at_utc=fixed_now(),
                fingerprint="wrong",
            )
        )
    lifecycle = make_lifecycle(engine)
    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            mark_deletable(engine, alice)
            lifecycle.retire_account_notification_state(
                retire_command(user_id=alice), connection=connection
            )
    assert raised.value.code == "receipt_fingerprint_mismatch"
    with engine.connect() as connection:
        assert (
            connection.execute(
                select(outbox_account_retirement_tombstone_table).where(
                    outbox_account_retirement_tombstone_table.c.recipient_user_id == alice
                )
            ).all()
            == []
        )


# ---------------------------------------------------------------------------
# 5. Compaction deletion binding: the retirement canonical fingerprint
# ---------------------------------------------------------------------------


def test_compaction_requires_the_exact_retirement_fingerprint() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    lifecycle = make_lifecycle(engine)
    retirement = retire_command(user_id=alice)
    with engine.begin() as connection:
        mark_deletable(engine, alice)
        lifecycle.retire_account_notification_state(retirement, connection=connection)

    real_fingerprint = _command_input_fingerprint(retirement)
    assert real_fingerprint
    # A compaction command claiming a DIFFERENT retirement fingerprint (e.g. an
    # old deletion's receipt) is rejected before any compaction side effect.
    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            lifecycle.request_eligible_account_event_compaction(
                compact_command(
                    user_id=alice,
                    retirement_receipt_fingerprint="0" * 64,
                ),
                connection=connection,
            )
    assert raised.value.code == "compaction_prerequisite_missing"
    assert raised.value.status_code == 409
    with engine.connect() as connection:
        assert (
            connection.execute(
                select(outbox_event_table.c.storage_state).where(
                    outbox_event_table.c.event_id == "evt_1"
                )
            ).scalar_one()
            == "full"
        )

    # With the exact fingerprint the compaction is authorized.
    with engine.begin() as connection:
        receipt = lifecycle.request_eligible_account_event_compaction(
            compact_command(user_id=alice, retirement_receipt_fingerprint=real_fingerprint),
            connection=connection,
        )
    assert receipt.compacted_count == 1
    with engine.connect() as connection:
        assert (
            connection.execute(
                select(outbox_event_table.c.storage_state).where(
                    outbox_event_table.c.event_id == "evt_1"
                )
            ).scalar_one()
            == "compacted"
        )


def test_compaction_same_operation_replay_returns_original_receipt() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    lifecycle = make_lifecycle(engine)
    retirement = retire_command(user_id=alice)
    with engine.begin() as connection:
        mark_deletable(engine, alice)
        lifecycle.retire_account_notification_state(retirement, connection=connection)
    command = compact_command(
        user_id=alice, retirement_receipt_fingerprint=_command_input_fingerprint(retirement)
    )
    with engine.begin() as connection:
        first = lifecycle.request_eligible_account_event_compaction(command, connection=connection)
    with engine.begin() as connection:
        second = lifecycle.request_eligible_account_event_compaction(command, connection=connection)
    assert first.compacted_count == 1
    # The replay returns the winner's real receipt counts.
    assert second.compacted_count == 1
    assert second.state == "completed"
