"""Fifth-round review items C: separate permanent account-retirement tombstone
with real inbox deletion, suppression receipts from event occurred_at,
compaction command count semantics, lifecycle savepoint reservation."""

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
from app.outbox.lifecycle import SqlAlchemyOutboxLifecycle
from app.outbox.metrics import SqlAlchemyOutboxMetrics
from app.outbox.notifications import NotificationMaterializer
from app.outbox.schema import (
    notification_delivery_receipt_table,
    notification_inbox_table,
    notification_suppression_table,
    notification_table,
    outbox_account_retirement_tombstone_table,
    outbox_compaction_command_table,
    outbox_event_table,
)
from app.platform.errors import PlatformError


def as_utc(value):
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def make_lifecycle(engine, **kwargs):
    kwargs.setdefault("archive_verifier", _Accepting())
    kwargs.setdefault("capability_secret", CAPABILITY_SECRET)
    return SqlAlchemyOutboxLifecycle(engine, now=lambda: fixed_now(), **kwargs)


class _Accepting:
    def verify_archive(self, *, archive_ref: str, checksum: str, **kwargs) -> bool:
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


def publish(engine, *, user_ids, event_id="evt_1", occurred_at=None):
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
        occurred_at=occurred_at or fixed_now(),
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


def retire(engine, lifecycle, alice: str, **overrides):
    mark_deletable(engine, alice)
    with engine.begin() as connection:
        return lifecycle.retire_account_notification_state(
            retire_command(user_id=alice, **overrides),
            connection=connection,
        )


def test_retirement_writes_tombstone_and_truly_deletes_the_inbox() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    dispatcher = make_dispatcher(engine)
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
    lifecycle = make_lifecycle(engine)

    receipt = retire(engine, lifecycle, alice)

    assert receipt.inbox_removed is True
    with engine.connect() as connection:
        # The inbox row is truly deleted...
        assert (
            connection.execute(
                select(notification_inbox_table).where(
                    notification_inbox_table.c.recipient_user_id == alice
                )
            ).all()
            == []
        )
        # ...and a permanent tombstone records the retirement.
        tombstone = (
            connection.execute(
                select(outbox_account_retirement_tombstone_table).where(
                    outbox_account_retirement_tombstone_table.c.recipient_user_id == alice
                )
            )
            .mappings()
            .one()
        )
        assert tombstone["next_notification_seq"] == 2
        assert tombstone["read_through_seq"] == 0


def test_materialization_after_retirement_is_blocked_by_the_tombstone() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    publish(engine, user_ids=(alice,), event_id="evt_2")
    dispatcher = make_dispatcher(engine)
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
    lifecycle = make_lifecycle(engine)
    retire(engine, lifecycle, alice)

    claim2 = dispatcher.claim_one(owner="worker-1")
    assert claim2 is not None
    dispatcher.run_consumer_and_finalize(claim2, owner="worker-1")

    with engine.connect() as connection:
        assert connection.execute(select(notification_table)).all() == []
        suppressions = (
            connection.execute(select(notification_suppression_table.c.reason)).scalars().all()
        )
        assert suppressions
        # No inbox was rebuilt: the tombstone is the only record.
        assert (
            connection.execute(
                select(notification_inbox_table).where(
                    notification_inbox_table.c.recipient_user_id == alice
                )
            ).all()
            == []
        )


def test_suppression_receipt_occurred_at_comes_from_the_event() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    event_occurred = datetime(2026, 1, 1, tzinfo=UTC)
    publish(engine, user_ids=(alice,), event_id="evt_1", occurred_at=event_occurred)
    # Suppress alice: deactivate before materialization.
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
        suppression = connection.execute(
            select(notification_suppression_table.c.suppressed_at_utc).where(
                notification_suppression_table.c.event_id == "evt_1"
            )
        ).scalar_one()

    # Compact and check the receipt's occurred_at equals the EVENT time.
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
        assert receipt["outcome"] == "recipient_inactive"
        assert as_utc(receipt["occurred_at_utc"]) == event_occurred
        assert receipt["occurred_at_utc"] != suppression


def test_compaction_command_counts_are_consistent_across_passes() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    publish(engine, user_ids=(alice,), event_id="evt_2")
    dispatcher = make_dispatcher(engine)
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
    lifecycle = make_lifecycle(engine)
    retire(engine, lifecycle, alice)

    from app.outbox.lifecycle import _command_input_fingerprint as _ret_fp
    from app.outbox.ports import EligibleAccountEventCompactionCommand

    base = dict(
        operation_id="op_comp_1",
        caller_principal="retention-ops",
        user_id=alice,
        deletion_id="del_1",
        retirement_receipt_id="op_ret_1",
        retirement_receipt_fingerprint=_ret_fp(retire_command(user_id=alice)),
        transaction_id="tx_comp_1",
        canonical_input_fingerprint="unused",
        capability_token=retention_token(),
    )

    def request():
        with engine.begin() as connection:
            return lifecycle.request_eligible_account_event_compaction(
                EligibleAccountEventCompactionCommand(**base), connection=connection
            )

    first = request()
    assert first.state == "accepted"
    assert first.compacted_count == 1
    assert first.blocked_count == 1
    with engine.connect() as connection:
        stored = (
            connection.execute(
                select(outbox_compaction_command_table).where(
                    outbox_compaction_command_table.c.operation_id == "op_comp_1"
                )
            )
            .mappings()
            .one()
        )
        assert stored["receipt_json"]["compacted_count"] == 1
        assert stored["receipt_json"]["blocked_count"] == 1

    # Deliver evt_2 then run the compaction worker: the receipt must be
    # cumulative and consistent.
    claim2 = dispatcher.claim_one(owner="worker-1")
    assert claim2 is not None
    dispatcher.run_consumer_and_finalize(claim2, owner="worker-1")
    from _helpers import make_settings

    from app.outbox.compaction_worker import CompactionWorker
    from app.platform.runtime import build_runtime
    from app.platform.worker import create_worker_runtime

    configured = make_settings()
    runtime = build_runtime(configured, adapters={"database_engine": engine})
    worker_runtime = create_worker_runtime(configured, runtime=runtime)
    worker = CompactionWorker(worker_runtime, lifecycle=lifecycle)
    stats = worker.run_once(owner="worker-1")
    assert stats.completed == 1
    with engine.connect() as connection:
        stored = (
            connection.execute(
                select(outbox_compaction_command_table).where(
                    outbox_compaction_command_table.c.operation_id == "op_comp_1"
                )
            )
            .mappings()
            .one()
        )
        assert stored["state"] == "completed"
        assert stored["receipt_json"]["compacted_count"] == 2
        assert stored["receipt_json"]["blocked_count"] == 0
        assert stored["receipt_json"]["eligible_count"] == 2
    runtime.close()


def test_lifecycle_reservation_survives_a_savepoint_rollback() -> None:
    """A conflicting tombstone scope inside a savepoint must not leak
    IntegrityError and must leave the outer transaction usable."""
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    lifecycle = make_lifecycle(engine)
    with engine.begin() as connection:
        lifecycle.redact_document_notifications(_redact_command(), connection=connection)
        # A second operation with the SAME operation id but a conflicting
        # document scope raises a permanent 409; the outer transaction stays
        # usable.
        with pytest.raises(PlatformError) as raised:
            lifecycle.redact_document_notifications(
                _redact_command(document_id="doc_OTHER"),
                connection=connection,
            )
        assert raised.value.status_code == 409
        # The outer transaction can still read/write afterwards.
        count = connection.execute(
            select(notification_table.c.id).where(notification_table.c.event_id == "evt_1")
        ).all()
        assert count == []


def _redact_command(
    *,
    operation_id="op_1",
    document_id="doc_evt_1",
    document_version_ids=("docv_evt_1",),
):
    from app.outbox.ports import DocumentNotificationRedactionCommand

    return DocumentNotificationRedactionCommand(
        operation_id=operation_id,
        caller_principal="documents",
        deletion_id="del_1",
        document_id=document_id,
        document_version_ids=document_version_ids,
        reason="document_pending_delete",
        transaction_id="tx_1",
        mode="inline",
        canonical_input_fingerprint="unused",
        capability_token=docs_redaction_token(deletion_id="del_1", transaction_id="tx_1"),
    )


def test_retirement_worker_run_forever_and_console_entry() -> None:
    import importlib
    import inspect

    from app.outbox.retirement_worker import RetirementWorker

    assert callable(getattr(RetirementWorker, "run_forever", None))
    module = importlib.import_module("app.outbox.retirement_worker")
    source = inspect.getsource(module.main)
    assert "run_forever" in source


def test_runtime_publisher_contract_documents_producer_boundary() -> None:
    """The typed publisher is the formal boundary for later producer domains;
    the runtime exposes it and the docstring documents the boundary."""
    import inspect

    from _helpers import make_settings

    from app.outbox.publisher import SqlAlchemyOutboxPublisher
    from app.platform.runtime import build_runtime

    engine = build_engine()
    runtime = build_runtime(make_settings(), adapters={"database_engine": engine})
    assert isinstance(runtime.resolve("outbox_publisher"), SqlAlchemyOutboxPublisher)
    doc = inspect.getdoc(SqlAlchemyOutboxPublisher)
    assert "ingestion/submission/quota/calibration/graph" in doc
    assert "OutboxPublishPort" in doc
    runtime.close()
