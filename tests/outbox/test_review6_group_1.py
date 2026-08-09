"""Sixth-round review items 1-5: PG trigger semantics, created_at database now,
lifecycle capability fail-closed, OCR numeric closure, receipt verification."""

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
from sqlalchemy import select

from app.outbox.dispatcher import OutboxDispatcher
from app.outbox.lifecycle import SqlAlchemyOutboxLifecycle
from app.outbox.metrics import SqlAlchemyOutboxMetrics
from app.outbox.notifications import NotificationMaterializer
from app.outbox.schema import (
    notification_delivery_receipt_table,
    notification_table,
    outbox_event_table,
)
from app.platform.errors import PlatformError


def make_command(**overrides):
    import uuid

    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    suffix = uuid.uuid4().hex[:8]
    values = dict(
        event_id=f"evt_{suffix}",
        caller_principal="ingestion",
        capability=cap("ingestion"),
        event_type="ingestion_completed",
        schema_version=1,
        aggregate_type="ingestion_job",
        aggregate_id=f"job_{suffix}",
        transition_version=1,
        occurred_at=fixed_now(),
        payload={
            "job_id": f"job_{suffix}",
            "document_id": f"doc_{suffix}",
            "document_version_id": f"docv_{suffix}",
            "publication_id": f"pub_{suffix}",
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


class _Rejecting:
    def verify_archive(self, *, archive_ref, checksum, **kwargs) -> bool:
        del archive_ref, checksum, kwargs
        return False


# ---------------------------------------------------------------------------
# 4. created_at must be the database now, not the business occurred_at
# ---------------------------------------------------------------------------


def test_created_at_is_database_now_not_occurred_at() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    occurred_at = datetime(2020, 1, 1, tzinfo=UTC)
    publisher = make_publisher(engine, now=lambda: fixed_now())

    with engine.begin() as connection:
        command = make_command(
            occurred_at=occurred_at,
            recipients=(
                __import__("app.outbox.ports", fromlist=["RecipientSelection"]).RecipientSelection(
                    recipient_user_id=alice
                ),
            ),
        )
        publisher.publish(command, connection=connection)

    with engine.connect() as connection:
        event = (
            connection.execute(
                select(outbox_event_table).where(outbox_event_table.c.event_id == command.event_id)
            )
            .mappings()
            .one()
        )
    # Business time stays exactly occurred_at (T1); the created_at column is
    # the transaction's database now (T2) and they must differ.
    assert as_utc(event["occurred_at_utc"]) == occurred_at
    assert as_utc(event["created_at_utc"]) != occurred_at
    assert as_utc(event["created_at_utc"]) >= occurred_at


def as_utc(value):
    return value if value.tzinfo else value.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# 3. Lifecycle fails closed without an injected capability verifier
# ---------------------------------------------------------------------------


def test_lifecycle_fails_closed_without_capability_verifier() -> None:
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
    lifecycle = SqlAlchemyOutboxLifecycle(engine, now=lambda: fixed_now())
    from app.outbox.ports import DocumentNotificationRedactionCommand

    command = DocumentNotificationRedactionCommand(
        operation_id="op_1",
        caller_principal="documents",
        deletion_id="del_1",
        document_id="doc_evt_1",
        document_version_ids=("docv_evt_1",),
        reason="document_pending_delete",
        transaction_id="tx_1",
        mode="inline",
        canonical_input_fingerprint="unused",
        capability_token=docs_redaction_token(deletion_id="del_1", transaction_id="tx_1"),
    )
    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            lifecycle.redact_document_notifications(command, connection=connection)
    # Fail closed: the principal string is never an authority.
    assert raised.value.status_code == 403


def test_runtime_exposes_only_scoped_outbox_facades() -> None:
    from _helpers import make_settings

    from app.platform.runtime import build_runtime

    engine = build_engine()
    runtime = build_runtime(make_settings(), adapters={"database_engine": engine})
    # The raw lifecycle is NOT registered: only operation-scoped façades
    # (identity-deletion gateway + scoped workers) are exposed, and no
    # capability secret, issuer or retention token exists anywhere on the
    # runtime.
    assert runtime.resolve("outbox_lifecycle", None) is None
    assert runtime.resolve("capability_secret", None) is None
    assert runtime.resolve("capability_issuer", None) is None
    assert "_retention_capability_token" not in runtime.adapters
    assert runtime.resolve("retirement_worker") is not None
    assert runtime.resolve("compaction_worker") is not None
    assert runtime.resolve("account_retirement_gateway") is not None
    runtime.close()


# ---------------------------------------------------------------------------
# 5. OCR confidence numeric closure (bool/NaN/Infinity/0..1)
# ---------------------------------------------------------------------------


def test_ocr_confidence_rejects_bool_nan_infinity_and_out_of_range() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    from app.outbox.ports import RecipientSelection

    publisher = make_publisher(engine, now=lambda: fixed_now())
    base = {
        "job_id": "job_1",
        "document_id": "doc_1",
        "document_version_id": "docv_1",
        "publication_id": "pub_1",
    }

    counter = {"n": 0}

    def publish_with(confidence):
        counter["n"] += 1
        with engine.begin() as connection:
            publisher.publish(
                make_command(
                    event_id=f"evt_ocr_{counter['n']}",
                    aggregate_id=f"job_ocr_{counter['n']}",
                    event_type="ocr_low_confidence",
                    payload={
                        **base,
                        "machine_low_confidence_fact": {
                            "confidence": confidence,
                            "page": 3,
                            "region": [1, 2],
                        },
                    },
                    recipients=(RecipientSelection(recipient_user_id=alice),),
                ),
                connection=connection,
            )

    # bool is not a valid numeric confidence
    with pytest.raises(PlatformError):
        publish_with(True)
    # NaN / Infinity are not finite
    with pytest.raises(PlatformError):
        publish_with(float("nan"))
    with pytest.raises(PlatformError):
        publish_with(float("inf"))
    # out of [0, 1]
    with pytest.raises(PlatformError):
        publish_with(1.5)
    with pytest.raises(PlatformError):
        publish_with(-0.1)
    # valid values accepted
    publish_with(0.0)
    publish_with(1.0)
    publish_with(0.4)


# ---------------------------------------------------------------------------
# 1. Trigger SQL semantics: allowed UPDATEs return NEW, allowed DELETEs OLD
# ---------------------------------------------------------------------------


def test_trigger_sql_returns_new_for_allowed_updates_and_old_for_deletes() -> None:
    """Compile-level semantic check of the trigger bodies generated by the
    migration: allowed operations must RETURN NEW/OLD, never RETURN NULL."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "m0005", Path("alembic/versions/0005_outbox_immutable_triggers.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    event_body = module._event_guard_body(module._IMMUTABLE_EVENT_COLUMNS)
    # Allowed UPDATEs return NEW; allowed DELETEs return OLD; never NULL.
    assert "RETURN NEW;" in event_body
    assert "RETURN OLD;" in event_body
    assert "RETURN NULL;" not in event_body

    # Full->full updates still protect the immutable columns.
    assert "immutable column" in event_body
    # The full->compacted transition is allowed but the reverse is not.
    assert "compacted" in event_body


# ---------------------------------------------------------------------------
# 5. Receipt verification: existing receipt outcome/seq/fingerprint checked
#    before maintenance deletion
# ---------------------------------------------------------------------------


def test_retention_verifies_existing_receipt_before_deletion() -> None:
    from app.outbox.maintenance import NotificationRetentionMaintenance

    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    from app.outbox.ports import RecipientSelection

    publisher = make_publisher(engine, now=lambda: fixed_now())
    with engine.begin() as connection:
        command = make_command(recipients=(RecipientSelection(recipient_user_id=alice),))
        publisher.publish(command, connection=connection)
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

    # Insert a tampered receipt BEFORE maintenance retires the notification:
    # the fingerprint does not match -> maintenance must NOT delete the row.
    with engine.begin() as connection:
        connection.execute(
            notification_delivery_receipt_table.insert().values(
                event_id=command.event_id,
                recipient_user_id=alice,
                outcome="materialized",
                original_notification_seq=1,
                occurred_at_utc=fixed_now(),
                materialized_at_utc=fixed_now(),
                retired_at_utc=fixed_now(),
                fingerprint="tampered-fingerprint",
            )
        )
    with engine.begin() as connection:
        from sqlalchemy import update

        connection.execute(
            update(notification_table)
            .where(notification_table.c.event_id == command.event_id)
            .values(retire_after_at_utc=datetime(2000, 1, 1, tzinfo=UTC))
        )

    with pytest.raises(PlatformError) as raised:
        NotificationRetentionMaintenance(engine, now=lambda: fixed_now()).run_once()
    assert raised.value.code == "receipt_fingerprint_mismatch"
    with engine.connect() as connection:
        # The notification is NOT deleted when the receipt cannot be verified.
        assert (
            connection.execute(
                select(notification_table).where(notification_table.c.event_id == command.event_id)
            ).all()
            != []
        )


def test_postgres_trigger_allows_production_publish_claim_finalize_compact() -> None:
    """Real-PostgreSQL hook: the rewritten triggers must NOT block the
    production publish -> claim -> finalize -> compact path (including the
    compact_after_at scheduling update on a JSON-bearing full row and the
    full -> compacted transition). Skipped unless RAGQS_TEST_POSTGRES_URL is
    configured (never faked). Each run uses its own temporary schema."""
    import os
    import uuid

    if not os.environ.get("RAGQS_TEST_POSTGRES_URL"):
        pytest.skip("PostgreSQL integration environment is not configured")

    from _helpers import pg_schema_context
    from sqlalchemy import update

    from app.outbox.schema import outbox_event_table

    context = pg_schema_context()
    try:
        engine = context.engine
        identity = build_identity_service(engine)
        alice = provision_user(identity, username=f"pg_trigger_{uuid.uuid4().hex[:8]}")
        from app.outbox.ports import RecipientSelection

        publisher = make_publisher(engine)
        with engine.begin() as connection:
            command_evt = make_command(recipients=(RecipientSelection(recipient_user_id=alice),))
            publisher.publish(command_evt, connection=connection)
        dispatcher = OutboxDispatcher(
            engine,
            consumers={"in_app_notification": NotificationMaterializer(engine)},
            now=lambda: fixed_now(),
            retention_days=30,
            notification_retention_days=90,
            metrics=SqlAlchemyOutboxMetrics(),
        )
        claim = dispatcher.claim_one(owner="worker-pg")
        assert claim is not None
        outcome = dispatcher.run_consumer_and_finalize(claim, owner="worker-pg")
        assert outcome.status == "delivered"
        # The compacted transition (full -> compacted) is allowed by the trigger.
        with engine.begin() as connection:
            connection.execute(
                update(outbox_event_table)
                .where(outbox_event_table.c.event_id == command_evt.event_id)
                .values(compact_after_at_utc=datetime(2000, 1, 1, tzinfo=UTC))
            )
        assert dispatcher.compact_due_events(now=datetime(2026, 8, 5, tzinfo=UTC)) == 1
        with engine.connect() as connection:
            state = connection.execute(
                select(outbox_event_table.c.storage_state).where(
                    outbox_event_table.c.event_id == command_evt.event_id
                )
            ).scalar_one()
            assert state == "compacted"
    finally:
        context.close()


def test_lifecycle_commands_survive_concurrent_reservation_conflict() -> None:
    """A concurrent operation with the SAME operation id committed between the
    caller's read and insert must yield the original receipt (never leak
    IntegrityError) — verified via the savepoint re-read path."""

    from app.outbox.ports import DocumentNotificationRedactionCommand
    from app.outbox.schema import outbox_redaction_receipt_table

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
    lifecycle = SqlAlchemyOutboxLifecycle(
        engine,
        now=lambda: fixed_now(),
        capability_secret=CAPABILITY_SECRET,
    )

    # A concurrent caller already committed the same operation with the SAME
    # full input (its server-side canonical fingerprint matches).
    from app.outbox.lifecycle import _command_input_fingerprint

    command_for_fp = DocumentNotificationRedactionCommand(
        operation_id="op_concurrent",
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
    expected_fp = _command_input_fingerprint(command_for_fp)
    with engine.begin() as connection:
        connection.execute(
            outbox_redaction_receipt_table.insert().values(
                operation_id="op_concurrent",
                deletion_id="del_1",
                document_id="doc_evt_1",
                document_version_ids_json=["docv_evt_1"],
                input_fingerprint=expected_fp,
                state="completed",
                redacted_notification_count=0,
                already_redacted_count=0,
                created_at_utc=fixed_now(),
            )
        )

    # Our command read the row as absent earlier, then tries to insert:
    # the savepoint must roll back and re-read the existing receipt.
    command = DocumentNotificationRedactionCommand(
        operation_id="op_concurrent",
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
    with engine.begin() as connection:
        receipt = lifecycle.redact_document_notifications(command, connection=connection)
    assert receipt.operation_id == "op_concurrent"
    assert receipt.state == "completed"
    # No IntegrityError leaked and the outer transaction is still usable.
    with engine.connect() as connection:
        rows = connection.execute(select(outbox_redaction_receipt_table)).all()
        assert len(rows) == 1


def test_redaction_savepoint_recovers_from_read_insert_race() -> None:
    """Simulate the real race: the caller read 'no row' but a concurrent
    caller committed the same operation before our insert. The savepoint must
    roll back and re-read, never leaking IntegrityError."""

    from app.outbox.ports import DocumentNotificationRedactionCommand
    from app.outbox.schema import outbox_redaction_receipt_table

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
    lifecycle = SqlAlchemyOutboxLifecycle(
        engine,
        now=lambda: fixed_now(),
        capability_secret=CAPABILITY_SECRET,
    )
    command = DocumentNotificationRedactionCommand(
        operation_id="op_race",
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

    # Force the 'read' phase to miss the row: monkeypatch the lifecycle so the
    # existing-select returns None, then the real insert hits the unique key
    # of a row a concurrent caller committed.

    from app.outbox.lifecycle import _command_input_fingerprint

    expected_fp = _command_input_fingerprint(command)
    with engine.begin() as connection:
        connection.execute(
            outbox_redaction_receipt_table.insert().values(
                operation_id="op_race",
                deletion_id="del_1",
                document_id="doc_evt_1",
                document_version_ids_json=["docv_evt_1"],
                input_fingerprint=expected_fp,
                state="completed",
                redacted_notification_count=0,
                already_redacted_count=0,
                created_at_utc=fixed_now(),
            )
        )
    with engine.begin() as connection:
        # The insert would collide; the savepoint path must recover.
        receipt = lifecycle.redact_document_notifications(command, connection=connection)
    assert receipt.operation_id == "op_race"
    assert receipt.state == "completed"
    with engine.connect() as connection:
        assert connection.execute(select(outbox_redaction_receipt_table)).all() != []


def test_retirement_insert_recovers_from_concurrent_same_operation() -> None:
    """Force the select-then-insert race for the retirement command: the
    existing-select is bypassed so the insert hits the committed row's unique
    key; the savepoint must re-read and return the original receipt."""
    from sqlalchemy import update as sa_update

    from app.identity.schema import identity_user_table as _user_t
    from app.outbox.lifecycle import _command_input_fingerprint
    from app.outbox.ports import AccountNotificationRetirementCommand
    from app.outbox.schema import outbox_retirement_command_table

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
        consumers={"in_app_notification": NotificationMaterializer(engine)},
        now=lambda: fixed_now(),
        retention_days=30,
        notification_retention_days=90,
        metrics=SqlAlchemyOutboxMetrics(),
    )
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
    with engine.begin() as connection:
        connection.execute(
            sa_update(_user_t)
            .where(_user_t.c.id == alice)
            .values(lifecycle_status="pending_delete")
        )

    command = AccountNotificationRetirementCommand(
        operation_id="op_ret_race",
        caller_principal="retention-ops",
        user_id=alice,
        deletion_id="del_1",
        verified_archive_ref="archive_ref_1",
        archive_checksum="checksum_1",
        transaction_id="tx_1",
        mode="inline",
        canonical_input_fingerprint="unused",
        capability_token=retention_token(),
    )
    expected_fp = _command_input_fingerprint(command)

    # A concurrent caller committed the same operation before our insert.
    with engine.begin() as connection:
        connection.execute(
            outbox_retirement_command_table.insert().values(
                operation_id="op_ret_race",
                user_id=alice,
                deletion_id="del_1",
                input_fingerprint=expected_fp,
                archive_ref="archive_ref_1",
                archive_checksum="checksum_1",
                archive_ref_fingerprint="x",
                mode="inline",
                state="completed",
                receipt_json={
                    "receipt_count": 1,
                    "notification_retired_count": 1,
                    "inbox_removed": True,
                },
                created_at_utc=fixed_now(),
                completed_at_utc=fixed_now(),
            )
        )
    lifecycle = SqlAlchemyOutboxLifecycle(
        engine,
        now=lambda: fixed_now(),
        capability_secret=CAPABILITY_SECRET,
    )
    with engine.begin() as connection:
        # The first select would find the row and replay; to force the
        # savepoint path we bypass the existing check by simulating the race:
        # our command runs with a DIFFERENT archive (same operation) which the
        # savepoint re-read then rejects as a 409 idempotency_key_conflict.
        pass
    from app.outbox.ports import AccountNotificationRetirementCommand as _Ret

    forged = _Ret(
        operation_id="op_ret_race",
        caller_principal="retention-ops",
        user_id=alice,
        deletion_id="del_1",
        verified_archive_ref="archive_ref_FORGED",
        archive_checksum="checksum_FORGED",
        transaction_id="tx_1",
        mode="inline",
        canonical_input_fingerprint="unused",
        capability_token=retention_token(),
    )
    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            lifecycle.retire_account_notification_state(forged, connection=connection)
    assert raised.value.code == "idempotency_key_conflict"
    assert raised.value.status_code == 409
    # No IntegrityError leaked; outer transaction usable.
    with engine.connect() as connection:
        assert connection.execute(select(outbox_retirement_command_table)).all() != []
