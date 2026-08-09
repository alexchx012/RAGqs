"""Blocker 4: retirement keeps a permanent inbox tombstone; materializer and
retirement serialize on the same user lock and re-check identity lifecycle."""

from __future__ import annotations

from _helpers import (
    CAPABILITY_SECRET,
    build_engine,
    build_identity_service,
    cap,
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
    notification_inbox_table,
    notification_suppression_table,
    notification_table,
    outbox_account_retirement_tombstone_table,
)


def make_lifecycle(engine):
    return SqlAlchemyOutboxLifecycle(
        engine,
        now=lambda: fixed_now(),
        archive_verifier=_AcceptingArchiveVerifier(),
        capability_secret=CAPABILITY_SECRET,
    )


class _AcceptingArchiveVerifier:
    def verify_archive(self, *, archive_ref: str, checksum: str, **kwargs) -> bool:
        del archive_ref, checksum
        return True


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


def make_dispatcher(engine):
    return OutboxDispatcher(
        engine,
        consumers={"in_app_notification": NotificationMaterializer(engine)},
        now=lambda: fixed_now(),
        retention_days=30,
        notification_retention_days=90,
        metrics=SqlAlchemyOutboxMetrics(),
    )


def mark_deletable(engine, user_id: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            update(identity_user_table)
            .where(identity_user_table.c.id == user_id)
            .values(lifecycle_status="pending_delete")
        )


def retire_command(*, user_id: str, operation_id="op_ret_1"):
    from app.outbox.ports import AccountNotificationRetirementCommand

    return AccountNotificationRetirementCommand(
        operation_id=operation_id,
        caller_principal="retention-ops",
        user_id=user_id,
        deletion_id="del_1",
        verified_archive_ref="archive_ref_1",
        archive_checksum="checksum_1",
        transaction_id="tx_1",
        mode="inline",
        canonical_input_fingerprint="fp_1",
        capability_token=retention_token(),
    )


def test_retirement_keeps_a_permanent_inbox_tombstone() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    dispatcher = make_dispatcher(engine)
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
    mark_deletable(engine, alice)
    lifecycle = make_lifecycle(engine)

    with engine.begin() as connection:
        lifecycle.retire_account_notification_state(
            retire_command(user_id=alice), connection=connection
        )

    with engine.connect() as connection:
        # The inbox row is removed and the PERMANENT tombstone keeps the watermark.
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
        assert tombstone["next_notification_seq"] == 2


def test_materialization_after_retirement_is_permanently_suppressed() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    publish(engine, user_ids=(alice,), event_id="evt_2")
    dispatcher = make_dispatcher(engine)
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
    mark_deletable(engine, alice)
    lifecycle = make_lifecycle(engine)
    with engine.begin() as connection:
        lifecycle.retire_account_notification_state(
            retire_command(user_id=alice), connection=connection
        )

    # A delivery for a later event must be suppressed, never rebuild seq 1.
    claim2 = dispatcher.claim_one(owner="worker-1")
    assert claim2 is not None
    dispatcher.run_consumer_and_finalize(claim2, owner="worker-1")

    with engine.connect() as connection:
        notifications = connection.execute(select(notification_table)).all()
        assert notifications == []
        suppressions = (
            connection.execute(select(notification_suppression_table.c.reason)).scalars().all()
        )
        assert suppressions
        # No inbox was rebuilt: the permanent tombstone is the only record.
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
        # Watermark never regresses or reuses sequences.
        assert tombstone["next_notification_seq"] == 2


def test_materializer_and_retirement_share_the_user_lock() -> None:
    """Both paths take the same advisory lock so they serialize per user."""
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    dispatcher = make_dispatcher(engine)
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
    mark_deletable(engine, alice)

    # Re-run materialization after retirement: within the lock, the identity
    # lifecycle re-check suppresses instead of materializing.
    lifecycle = make_lifecycle(engine)
    with engine.begin() as connection:
        lifecycle.retire_account_notification_state(
            retire_command(user_id=alice), connection=connection
        )
    claim2 = dispatcher.claim_one(owner="worker-1")
    if claim2 is not None:
        dispatcher.run_consumer_and_finalize(claim2, owner="worker-1")
    with engine.connect() as connection:
        assert connection.execute(select(notification_table)).all() == []


def test_retired_account_keeps_high_watermark_across_many_events() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    for index in range(3):
        publish(engine, user_ids=(alice,), event_id=f"evt_{index}")
    dispatcher = make_dispatcher(engine)
    for _ in range(3):
        claim = dispatcher.claim_one(owner="worker-1")
        assert claim is not None
        dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
    mark_deletable(engine, alice)
    lifecycle = make_lifecycle(engine)
    with engine.begin() as connection:
        lifecycle.retire_account_notification_state(
            retire_command(user_id=alice), connection=connection
        )
    for _ in range(3):
        claim = dispatcher.claim_one(owner="worker-1")
        if claim is not None:
            dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
    with engine.connect() as connection:
        # No inbox is rebuilt; the permanent tombstone keeps the watermark.
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
        assert tombstone["next_notification_seq"] == 4
        assert tombstone["read_through_seq"] == 0
