"""Durable retirement: accepted -> completed worker processing and idempotent retry."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update

from app.outbox.lifecycle import SqlAlchemyOutboxLifecycle
from app.outbox.notifications import NotificationMaterializer
from app.outbox.retirement_worker import (
    RetirementWorker,
)
from app.outbox.schema import (
    notification_delivery_receipt_table,
    notification_inbox_table,
    notification_table,
    outbox_account_retirement_tombstone_table,
    outbox_retirement_command_table,
)
from app.platform.runtime import build_runtime
from app.platform.worker import create_worker_runtime
from tests._support import (
    build_engine,
    build_identity_service,
    fixed_now,
    make_publisher,
    make_settings,
    provision_user,
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


def make_lifecycle(engine):
    return SqlAlchemyOutboxLifecycle(
        engine,
        now=lambda: fixed_now(),
        archive_verifier=_AcceptingArchiveVerifier(),
    )


def deliver(engine, *, user_ids, event_id="evt_1"):
    from app.outbox.dispatcher import OutboxDispatcher
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


def retire_command(*, operation_id="op_ret_1", user_id="user_x"):
    from app.outbox.ports import AccountNotificationRetirementCommand

    return AccountNotificationRetirementCommand(
        operation_id=operation_id,
        caller_principal="retention-ops",
        user_id=user_id,
        deletion_id="del_ret_1",
        verified_archive_ref="archive_ref_1",
        archive_checksum="checksum_1",
        transaction_id="tx_ret_1",
        mode="durable",
        canonical_input_fingerprint="fp_ret_1",
    )


class _MutableClock:
    def __init__(self, start: datetime) -> None:
        self._current = start

    def now(self) -> datetime:
        return self._current

    def advance(self, seconds: int) -> None:
        from datetime import timedelta

        self._current = self._current + timedelta(seconds=seconds)


def stored_command(engine, operation_id: str) -> dict:
    with engine.connect() as connection:
        row = (
            connection.execute(
                select(outbox_retirement_command_table).where(
                    outbox_retirement_command_table.c.operation_id == operation_id
                )
            )
            .mappings()
            .one()
        )
        return dict(row)


def make_retirement_worker(engine, *, now=None, lifecycle=None, **kwargs):
    configured = make_settings()
    runtime = build_runtime(
        configured,
        adapters={
            "database_engine": engine,
        },
    )
    worker_runtime = create_worker_runtime(
        configured,
        runtime=runtime,
        now=now or (lambda: fixed_now()),
    )
    if lifecycle is None:
        lifecycle = SqlAlchemyOutboxLifecycle(
            engine,
            now=lambda: fixed_now(),
            archive_verifier=_AcceptingArchiveVerifier(),
        )
    if "processor" not in kwargs:
        # Build the narrow scoped processor around the lifecycle's internal
        # no-token entry; no token is needed or present anywhere.
        from app.outbox.retirement_worker import build_retirement_processor

        kwargs["processor"] = build_retirement_processor(lifecycle)
    return RetirementWorker(worker_runtime, **kwargs)


def test_durable_command_is_accepted_and_persisted_for_retention_retry() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    lifecycle = make_lifecycle(engine)

    receipt = None
    with engine.begin() as connection:
        mark_account_deletable(engine, alice)
        receipt = lifecycle.retire_account_notification_state(
            retire_command(user_id=alice), connection=connection
        )

    assert receipt.state == "accepted"
    assert receipt.retryable is True
    # The durable work is not applied yet: notifications remain.
    with engine.connect() as connection:
        assert len(connection.execute(select(notification_table)).all()) == 1
        stored = stored_command(engine, "op_ret_1")
        assert stored["state"] == "accepted"
        assert stored["mode"] == "durable"


def test_retirement_worker_completes_accepted_durable_commands() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    lifecycle = make_lifecycle(engine)
    with engine.begin() as connection:
        mark_account_deletable(engine, alice)
        lifecycle.retire_account_notification_state(
            retire_command(user_id=alice), connection=connection
        )

    worker = make_retirement_worker(engine)
    stats = worker.run_once(owner="worker-1")

    assert stats.completed == 1
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
        assert len(connection.execute(select(notification_delivery_receipt_table)).all()) == 1
        stored = stored_command(engine, "op_ret_1")
        assert stored["state"] == "completed"
        assert stored["completed_at_utc"] is not None


def test_retirement_worker_is_idempotent_after_a_lost_response() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    lifecycle = make_lifecycle(engine)
    with engine.begin() as connection:
        mark_account_deletable(engine, alice)
        first = lifecycle.retire_account_notification_state(
            retire_command(user_id=alice), connection=connection
        )
    assert first.state == "accepted"

    worker = make_retirement_worker(engine)
    assert worker.run_once(owner="worker-1").completed == 1
    # Retention retries the same operation after a lost response: the stored
    # receipt is now completed, and the work is not applied twice.
    with engine.begin() as connection:
        mark_account_deletable(engine, alice)
        second = lifecycle.retire_account_notification_state(
            retire_command(user_id=alice), connection=connection
        )
    assert second.state == "completed"
    assert second.retryable is False
    assert second.notification_retired_count == 1
    assert worker.run_once(owner="worker-1").completed == 0
    with engine.connect() as connection:
        assert len(connection.execute(select(notification_delivery_receipt_table)).all()) == 1


def test_durable_worker_skips_inline_and_unknown_operations() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    lifecycle = make_lifecycle(engine)
    from app.outbox.ports import AccountNotificationRetirementCommand

    inline = AccountNotificationRetirementCommand(
        operation_id="op_inline",
        caller_principal="retention-ops",
        user_id=alice,
        deletion_id="del_inline",
        verified_archive_ref="ref_inline",
        archive_checksum="checksum",
        transaction_id="tx_inline",
        mode="inline",
        canonical_input_fingerprint="fp_inline",
    )
    with engine.begin() as connection:
        mark_account_deletable(engine, alice)
        inline_receipt = lifecycle.retire_account_notification_state(inline, connection=connection)
    assert inline_receipt.state == "completed"

    worker = make_retirement_worker(engine)
    stats = worker.run_once(owner="worker-1")

    assert stats.completed == 0


def test_durable_retry_after_transient_failure_uses_the_same_operation() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    lifecycle = make_lifecycle(engine)
    with engine.begin() as connection:
        mark_account_deletable(engine, alice)
        lifecycle.retire_account_notification_state(
            retire_command(user_id=alice), connection=connection
        )

    class FailingOnce:
        def __init__(self, delegate) -> None:
            self._delegate = delegate
            self._failed = False

        def retire_account_notification_state(self, command, *, connection):
            mark_account_deletable(engine, alice)
            return self._delegate.retire_account_notification_state(command, connection=connection)

        def apply_accepted_durable_retirement(self, operation_id, *, connection):
            if not self._failed:
                self._failed = True
                raise RuntimeError("transient database contention")
            mark_account_deletable(engine, alice)
            return self._delegate.apply_accepted_durable_retirement(
                operation_id, connection=connection
            )

    failing = FailingOnce(lifecycle)
    worker = make_retirement_worker(engine, lifecycle=failing)

    stats = worker.run_once(owner="worker-1")
    assert stats.completed == 0
    assert stats.deferred == 1

    # The operation stays accepted; the same-ID retry (worker loop later, or
    # retention's own retry path) completes it exactly once.
    with engine.begin() as connection:
        mark_account_deletable(engine, alice)
        completed = lifecycle.apply_durable_retirement(
            retire_command(user_id=alice), connection=connection
        )
    assert completed.state == "completed"
    with engine.connect() as connection:
        assert stored_command(engine, "op_ret_1")["state"] == "completed"
        assert len(connection.execute(select(notification_delivery_receipt_table)).all()) == 1
    # A further retry with the same operation id replays the completed receipt.
    with engine.begin() as connection:
        mark_account_deletable(engine, alice)
        replay = lifecycle.retire_account_notification_state(
            retire_command(user_id=alice), connection=connection
        )
    assert replay.state == "completed"
    assert replay.notification_retired_count == 1
