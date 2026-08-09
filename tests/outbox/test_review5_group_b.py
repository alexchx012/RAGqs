"""Fifth-round review items B: capability verifier for lifecycle callers,
savepoint-based operation reservation, tombstone scope conflict normalization,
HMAC archive proofs bound to user/deletion, durable retirement lifecycle
re-validation."""

from __future__ import annotations

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
from app.outbox.lifecycle import SqlAlchemyOutboxLifecycle
from app.outbox.metrics import SqlAlchemyOutboxMetrics
from app.outbox.notifications import NotificationMaterializer
from app.outbox.schema import (
    outbox_retirement_command_table,
)
from app.platform.errors import PlatformError


def make_lifecycle(engine, **kwargs):
    kwargs.setdefault("archive_verifier", _AcceptingArchive())
    kwargs.setdefault("capability_secret", CAPABILITY_SECRET)
    return SqlAlchemyOutboxLifecycle(engine, now=lambda: fixed_now(), **kwargs)


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


def redact_command(
    *,
    document_id="doc_evt_1",
    document_version_ids=("docv_evt_1",),
    operation_id="op_1",
    deletion_id="del_1",
    transaction_id="tx_1",
):
    from app.outbox.ports import DocumentNotificationRedactionCommand

    return DocumentNotificationRedactionCommand(
        operation_id=operation_id,
        caller_principal="documents",
        deletion_id=deletion_id,
        document_id=document_id,
        document_version_ids=document_version_ids,
        reason="document_pending_delete",
        transaction_id=transaction_id,
        mode="inline",
        canonical_input_fingerprint="unused",
        capability_token=docs_redaction_token(
            deletion_id=deletion_id, transaction_id=transaction_id
        ),
    )


# ---------------------------------------------------------------------------
# 1. Lifecycle callers verified through an injected capability verifier
# ---------------------------------------------------------------------------


def test_redaction_requires_exact_deletion_capability() -> None:
    from app.outbox.ports import DocumentNotificationRedactionCommand

    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    lifecycle = make_lifecycle(engine)

    # Correct capability: the documents token is bound to the exact
    # deletion/transaction and authorizes the command.
    with engine.begin() as connection:
        receipt = lifecycle.redact_document_notifications(redact_command(), connection=connection)
    assert receipt.state == "completed"

    # A forged scope fails 403 even though the principal string says
    # "documents": the token is bound to del_FORGED while the command claims
    # del_1, so the authorizer rejects the scope mismatch.
    command = redact_command(operation_id="op_2")
    forged = DocumentNotificationRedactionCommand(
        operation_id=command.operation_id,
        caller_principal=command.caller_principal,
        deletion_id=command.deletion_id,
        document_id=command.document_id,
        document_version_ids=command.document_version_ids,
        reason=command.reason,
        transaction_id=command.transaction_id,
        mode=command.mode,
        canonical_input_fingerprint=command.canonical_input_fingerprint,
        capability_token=docs_redaction_token(
            deletion_id="del_FORGED", transaction_id=command.transaction_id
        ),
    )
    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            lifecycle.redact_document_notifications(forged, connection=connection)
    assert raised.value.status_code == 403


def test_retirement_requires_retention_capability() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    mark_deletable(engine, alice)
    lifecycle = make_lifecycle(engine)

    # The principal string is an audit label only; authority comes from the
    # signed capability token. A FORGED token (a documents-redaction token
    # presented as the retirement capability) is rejected with 403 even
    # though the principal claims retention-ops.
    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            lifecycle.retire_account_notification_state(
                retire_command(
                    user_id=alice,
                    capability_token=docs_redaction_token(
                        deletion_id="del_1", transaction_id="tx_1"
                    ),
                ),
                connection=connection,
            )
    assert raised.value.status_code == 403


class _AcceptingArchive:
    def verify_archive(self, *, archive_ref: str, checksum: str, **kwargs) -> bool:
        del archive_ref, checksum
        return True


# ---------------------------------------------------------------------------
# 2. HMAC archive proof bound to user/deletion/workflow
# ---------------------------------------------------------------------------


def test_archive_proof_is_hmac_bound_to_user_and_deletion() -> None:
    from app.identity.archive import IdentityArchiveProofIssuer, IdentityArchiveProofVerifier

    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    bob = provision_user(identity, username="bob")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    mark_deletable(engine, alice)
    mark_deletable(engine, bob)

    issuer = IdentityArchiveProofIssuer(secret=b"test-secret-archive-key")
    verifier = IdentityArchiveProofVerifier(secret=b"test-secret-archive-key")
    ref, checksum = issuer.issue(
        user_id=alice, deletion_id="del_1", cleanup_operation_id="cleanup_1", requested_at="t1"
    )
    assert verifier.verify_archive(
        archive_ref=ref,
        checksum=checksum,
        user_id=alice,
        deletion_id="del_1",
        cleanup_operation_id="cleanup_1",
    )
    # Cross-user forgery fails.
    assert not verifier.verify_archive(
        archive_ref=ref,
        checksum=checksum,
        user_id=bob,
        deletion_id="del_1",
        cleanup_operation_id="cleanup_1",
    )
    # Cross-deletion forgery fails.
    assert not verifier.verify_archive(
        archive_ref=ref,
        checksum=checksum,
        user_id=alice,
        deletion_id="del_FORGED",
        cleanup_operation_id="cleanup_1",
    )
    # Cross-workflow forgery fails.
    assert not verifier.verify_archive(
        archive_ref=ref,
        checksum=checksum,
        user_id=alice,
        deletion_id="del_1",
        cleanup_operation_id="cleanup_FORGED",
    )

    lifecycle = make_lifecycle(engine, archive_verifier=verifier)
    with engine.begin() as connection:
        receipt = lifecycle.retire_account_notification_state(
            retire_command(
                user_id=alice,
                verified_archive_ref=ref,
                archive_checksum=checksum,
            ),
            connection=connection,
        )
    assert receipt.state == "completed"


def test_durable_retirement_revalidates_lifecycle_and_workflow() -> None:
    from app.identity.archive import IdentityArchiveProofIssuer, IdentityArchiveProofVerifier

    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    mark_deletable(engine, alice)
    issuer = IdentityArchiveProofIssuer(secret=b"secret")
    ref, checksum = issuer.issue(
        user_id=alice, deletion_id="del_1", cleanup_operation_id="cleanup_1", requested_at="t1"
    )
    lifecycle = make_lifecycle(
        engine, archive_verifier=IdentityArchiveProofVerifier(secret=b"secret")
    )
    with engine.begin() as connection:
        receipt = lifecycle.retire_account_notification_state(
            retire_command(
                user_id=alice, mode="durable", verified_archive_ref=ref, archive_checksum=checksum
            ),
            connection=connection,
        )
    assert receipt.state == "accepted"

    # The account becomes deleted BEFORE the worker applies the durable work.
    with engine.begin() as connection:
        connection.execute(
            update(identity_user_table)
            .where(identity_user_table.c.id == alice)
            .values(lifecycle_status="deleted")
        )
    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            lifecycle.apply_durable_retirement(
                retire_command(
                    user_id=alice,
                    mode="durable",
                    verified_archive_ref=ref,
                    archive_checksum=checksum,
                ),
                connection=connection,
            )
    assert raised.value.status_code == 409
    assert raised.value.code == "account_already_deleted"
    # The accepted command stays accepted for a permanent conflict path.
    with engine.connect() as connection:
        stored = (
            connection.execute(
                select(outbox_retirement_command_table).where(
                    outbox_retirement_command_table.c.operation_id == "op_ret_1"
                )
            )
            .mappings()
            .one()
        )
        assert stored["state"] == "accepted"


# ---------------------------------------------------------------------------
# 3. Tombstone scope conflict normalized to 409/422 (no IntegrityError)
# ---------------------------------------------------------------------------


def test_tombstone_scope_conflict_is_normalized() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    lifecycle = make_lifecycle(engine)

    with engine.begin() as connection:
        lifecycle.redact_document_notifications(redact_command(), connection=connection)
    # A second redaction for the same (document, version) is a no-op receipt.
    with engine.begin() as connection:
        second = lifecycle.redact_document_notifications(redact_command(), connection=connection)
    assert second.state == "completed"
    # A conflicting scope (different version list for the same operation id)
    # is a permanent 409, never an IntegrityError leak.
    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            lifecycle.redact_document_notifications(
                redact_command(document_version_ids=("docv_OTHER",)),
                connection=connection,
            )
    assert raised.value.status_code == 409
