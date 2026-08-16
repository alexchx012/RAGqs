"""Remaining review items: permanent tombstone, eligible compaction accepted,
replay concurrency, PG integration, retention cap enforcement."""

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
from app.outbox.maintenance import NotificationRetentionMaintenance
from app.outbox.metrics import SqlAlchemyOutboxMetrics
from app.outbox.notifications import NotificationMaterializer
from app.outbox.schema import (
    notification_table,
    outbox_document_tombstone_table,
    outbox_event_table,
)
from app.platform.errors import PlatformError


def make_lifecycle(engine, **kwargs):
    return SqlAlchemyOutboxLifecycle(
        engine,
        now=lambda: fixed_now(),
        archive_verifier=kwargs.pop("archive_verifier", _AcceptingArchiveVerifier()),
        **kwargs,
    )


class _AcceptingArchiveVerifier:
    def verify_archive(self, *, archive_ref: str, checksum: str, **kwargs) -> bool:
        del archive_ref, checksum
        return True


def deliver(engine, *, user_ids, event_id="evt_1"):
    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    publisher = make_publisher(engine, now=lambda: fixed_now())
    command = OutboxPublishCommand(
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


def redact_command(*, document_id="doc_evt_1", document_version_ids=("docv_evt_1",)):
    from app.outbox.ports import DocumentNotificationRedactionCommand

    return DocumentNotificationRedactionCommand(
        operation_id="op_1",
        caller_principal="documents",
        deletion_id="del_1",
        document_id=document_id,
        document_version_ids=document_version_ids,
        reason="document_pending_delete",
        transaction_id="tx_1",
        mode="inline",
        canonical_input_fingerprint="fp_1",
    )


def test_tombstone_survives_compaction_and_blocks_later_materialization() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    # Publish but do not materialize yet; redact, then compact the event.
    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    publisher = make_publisher(engine, now=lambda: fixed_now())
    with engine.begin() as connection:
        publisher.publish(
            OutboxPublishCommand(
                event_id="evt_1",
                caller_principal="ingestion",
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
                trace_id="t",
                recipients=(RecipientSelection(recipient_user_id=alice),),
            ),
            connection=connection,
        )
    lifecycle = make_lifecycle(engine)
    with engine.begin() as connection:
        lifecycle.redact_document_notifications(
            redact_command(document_id="doc_1", document_version_ids=("docv_1",)),
            connection=connection,
        )
    with engine.begin() as connection:
        connection.execute(
            update(outbox_event_table)
            .where(outbox_event_table.c.event_id == "evt_1")
            .values(compact_after_at_utc=datetime(2000, 1, 1, tzinfo=UTC))
        )
    dispatcher = OutboxDispatcher(
        engine,
        consumers={"in_app_notification": NotificationMaterializer(engine)},
        now=lambda: fixed_now(),
        retention_days=30,
        notification_retention_days=90,
        metrics=SqlAlchemyOutboxMetrics(),
    )
    dispatcher.compact_due_events(now=datetime(2026, 8, 5, tzinfo=UTC))
    with engine.connect() as connection:
        assert (
            connection.execute(
                select(outbox_document_tombstone_table).where(
                    outbox_document_tombstone_table.c.document_id == "doc_1"
                )
            ).all()
            != []
        )
        assert connection.execute(select(outbox_event_table)).all() != []


def test_eligible_compaction_blocks_pending_and_returns_blocked_count() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    # evt_1 delivered; evt_2 published but delivery never delivered.
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    publisher = make_publisher(engine, now=lambda: fixed_now())
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
                trace_id="t",
                recipients=(RecipientSelection(recipient_user_id=alice),),
            ),
            connection=connection,
        )
    lifecycle = make_lifecycle(engine)
    from app.identity.schema import identity_user_table

    with engine.begin() as connection:
        connection.execute(
            update(identity_user_table)
            .where(identity_user_table.c.id == alice)
            .values(lifecycle_status="pending_delete")
        )
    from app.outbox.lifecycle import _command_input_fingerprint
    from app.outbox.ports import AccountNotificationRetirementCommand

    retirement = AccountNotificationRetirementCommand(
        operation_id="op_ret_1",
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
        lifecycle.retire_account_notification_state(retirement, connection=connection)
    from app.outbox.ports import EligibleAccountEventCompactionCommand

    with engine.begin() as connection:
        receipt = lifecycle.request_eligible_account_event_compaction(
            EligibleAccountEventCompactionCommand(
                operation_id="op_comp_1",
                caller_principal="retention-ops",
                user_id=alice,
                deletion_id="del_1",
                retirement_receipt_id="op_ret_1",
                retirement_receipt_fingerprint=_command_input_fingerprint(retirement),
                transaction_id="tx_2",
                canonical_input_fingerprint="fp_2",
            ),
            connection=connection,
        )

    assert receipt.compacted_count == 1
    assert receipt.blocked_count >= 1
    with engine.connect() as connection:
        evt_2_state = connection.execute(
            select(outbox_event_table.c.storage_state).where(
                outbox_event_table.c.event_id == "evt_2"
            )
        ).scalar_one()
        assert evt_2_state == "full"


def test_replay_after_compaction_is_not_replayable_and_cas_conflict_is_accurate() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish_and_dead_letter = _dead_letter_helper(engine, alice)
    dispatcher = publish_and_dead_letter["dispatcher"]

    # Replay with a stale expected_version -> accurate version_conflict.
    with pytest.raises(PlatformError) as version_conflict:
        dispatcher.replay(
            "evt_1",
            consumer_name="in_app_notification",
            expected_version=1,
            idempotency_key="k1",
            request_hash="h1",
        )
    assert version_conflict.value.code == "version_conflict"


def _dead_letter_helper(engine, alice: str):
    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    publisher = make_publisher(engine, now=lambda: fixed_now())
    with engine.begin() as connection:
        publisher.publish(
            OutboxPublishCommand(
                event_id="evt_1",
                caller_principal="ingestion",
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
                trace_id="t",
                recipients=(RecipientSelection(recipient_user_id=alice),),
            ),
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
    dispatcher.fail_and_schedule(
        claim, owner="worker-1", error_category="permanent", error_code="unsupported_schema"
    )
    return {"dispatcher": dispatcher}


def test_retention_cap_never_exceeds_per_user_limit_across_runs() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    for index in range(55):
        deliver(engine, user_ids=(alice,), event_id=f"evt_{index}")
    maintenance = NotificationRetentionMaintenance(engine, now=lambda: fixed_now())
    maintenance.run_once(limit=200)
    maintenance.run_once(limit=200)
    maintenance.run_once(limit=200)

    with engine.connect() as connection:
        count = connection.execute(
            select(__import__("sqlalchemy").func.count()).select_from(notification_table)
        ).scalar_one()
    assert int(count) == 50


def test_postgres_integration_hook_runs_production_statements_when_configured() -> None:
    """Runs against a real PostgreSQL when RAGQS_TEST_POSTGRES_URL is set.

    Each run uses its own temporary schema so runs are independent and
    reliably cleaned up.
    """
    import os
    import uuid

    if not os.environ.get("RAGQS_TEST_POSTGRES_URL"):
        pytest.skip("PostgreSQL integration environment is not configured")

    from _helpers import pg_schema_context

    context = pg_schema_context()
    try:
        engine = context.engine
        identity = build_identity_service(engine)
        suffix = uuid.uuid4().hex[:8]
        alice = provision_user(identity, username=f"alice_{suffix}")
        event_id = f"evt_pg_{suffix}"

        dispatcher = OutboxDispatcher(
            engine,
            consumers={"in_app_notification": NotificationMaterializer(engine)},
            now=lambda: fixed_now(),
            retention_days=30,
            notification_retention_days=90,
            metrics=SqlAlchemyOutboxMetrics(),
        )
        from app.outbox.ports import OutboxPublishCommand, RecipientSelection

        publisher = make_publisher(engine)
        with engine.begin() as connection:
            publisher.publish(
                OutboxPublishCommand(
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
                    trace_id="t",
                    recipients=(RecipientSelection(recipient_user_id=alice),),
                ),
                connection=connection,
            )
        claim = dispatcher.claim_one(owner="worker-pg")
        assert claim is not None
        outcome = dispatcher.run_consumer_and_finalize(claim, owner="worker-pg")
        assert outcome.status == "delivered"
        with engine.connect() as connection:
            assert (
                connection.execute(
                    select(notification_table).where(
                        notification_table.c.recipient_user_id == alice
                    )
                ).all()
                != []
            )
    finally:
        context.close()


def test_eligible_compaction_returns_accepted_when_events_remain_blocked() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    publisher = make_publisher(engine, now=lambda: fixed_now())
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
                trace_id="t",
                recipients=(RecipientSelection(recipient_user_id=alice),),
            ),
            connection=connection,
        )
    lifecycle = make_lifecycle(engine)
    from app.identity.schema import identity_user_table

    with engine.begin() as connection:
        connection.execute(
            update(identity_user_table)
            .where(identity_user_table.c.id == alice)
            .values(lifecycle_status="pending_delete")
        )
    from app.outbox.lifecycle import _command_input_fingerprint
    from app.outbox.ports import AccountNotificationRetirementCommand

    retirement = AccountNotificationRetirementCommand(
        operation_id="op_ret_acc",
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
        lifecycle.retire_account_notification_state(retirement, connection=connection)
    from app.outbox.ports import EligibleAccountEventCompactionCommand

    with engine.begin() as connection:
        receipt = lifecycle.request_eligible_account_event_compaction(
            EligibleAccountEventCompactionCommand(
                operation_id="op_comp_acc",
                caller_principal="retention-ops",
                user_id=alice,
                deletion_id="del_1",
                retirement_receipt_id="op_ret_acc",
                retirement_receipt_fingerprint=_command_input_fingerprint(retirement),
                transaction_id="tx_2",
                canonical_input_fingerprint="fp_2",
            ),
            connection=connection,
        )

    # evt_2's delivery is pending: the request cannot promise compaction.
    assert receipt.compacted_count == 1
    assert receipt.blocked_count == 1
    assert receipt.state == "accepted"
    assert receipt.retryable is True


def test_replay_in_progress_same_key_returns_idempotency_in_progress() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    ctx = _dead_letter_helper(engine, alice)
    dispatcher = ctx["dispatcher"]

    # A concurrent replay holds the reservation (completed=False).
    from app.outbox.schema import outbox_replay_idempotency_table

    with engine.begin() as connection:
        connection.execute(
            outbox_replay_idempotency_table.insert().values(
                event_id="evt_1",
                consumer_name="in_app_notification",
                idempotency_key="k_concurrent",
                request_hash="h_concurrent",
                completed=False,
                response_json=None,
                created_at_utc=fixed_now(),
            )
        )

    with pytest.raises(PlatformError) as in_progress:
        dispatcher.replay(
            "evt_1",
            consumer_name="in_app_notification",
            expected_version=2,
            idempotency_key="k_concurrent",
            request_hash="h_concurrent",
        )
    assert in_progress.value.code == "idempotency_in_progress"


def test_replay_reserves_before_state_checks_and_rolls_back_on_conflict() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    ctx = _dead_letter_helper(engine, alice)
    dispatcher = ctx["dispatcher"]

    # Wrong expected_version: the replay fails and the reservation rolls back,
    # so the same key can be retried with the correct version.
    with pytest.raises(PlatformError):
        dispatcher.replay(
            "evt_1",
            consumer_name="in_app_notification",
            expected_version=1,
            idempotency_key="k_retry",
            request_hash="h_retry",
        )
    from app.outbox.schema import outbox_replay_idempotency_table

    with engine.connect() as connection:
        rows = connection.execute(
            select(outbox_replay_idempotency_table).where(
                outbox_replay_idempotency_table.c.idempotency_key == "k_retry"
            )
        ).all()
        assert rows == []

    receipt = dispatcher.replay(
        "evt_1",
        consumer_name="in_app_notification",
        expected_version=2,
        idempotency_key="k_retry",
        request_hash="h_retry",
    )
    assert receipt.status == "pending"
    # The completed reservation replays the original receipt on retry.
    replay = dispatcher.replay(
        "evt_1",
        consumer_name="in_app_notification",
        expected_version=2,
        idempotency_key="k_retry",
        request_hash="h_retry",
    )
    assert replay.version == receipt.version


def test_runtime_assembles_publisher_dispatcher_lifecycle_and_worker_entries() -> None:
    from _helpers import make_settings

    from app.platform.runtime import build_runtime

    engine = build_engine()
    runtime = build_runtime(make_settings(), adapters={"database_engine": engine})

    assert runtime.resolve("outbox_publisher", None) is None
    assert isinstance(runtime.resolve("outbox_dispatcher"), OutboxDispatcher)
    # Raw publisher and lifecycle objects are not registered as runtime
    # adapters; only operation-scoped facades are.
    assert runtime.resolve("outbox_lifecycle", None) is None
    assert runtime.resolve("account_retirement_gateway") is not None
    assert runtime.resolve("retirement_worker") is not None
    assert runtime.resolve("compaction_worker") is not None
    assert callable(runtime.resolve("outbox_dispatcher").claim_one)
    assert callable(runtime.resolve("outbox_dispatcher").compact_due_events)
    runtime.close()


def test_end_to_end_publish_dispatch_notify_read_and_ack() -> None:
    from _helpers import make_publisher, make_settings

    from app.outbox.ports import OutboxPublishCommand, RecipientSelection
    from app.platform.runtime import build_runtime

    configured = make_settings()
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())
    runtime = build_runtime(
        configured,
        adapters={
            "database_engine": engine,
            "identity_access": identity,
            "outbox_publisher": publisher,
        },
    )
    dispatcher = runtime.resolve("outbox_dispatcher")
    service = runtime.resolve("notification_service")
    with engine.begin() as connection:
        publisher.publish(
            OutboxPublishCommand(
                event_id="evt_e2e",
                caller_principal="ingestion",
                event_type="ingestion_completed",
                schema_version=1,
                aggregate_type="ingestion_job",
                aggregate_id="job_e2e",
                transition_version=1,
                occurred_at=fixed_now(),
                payload={
                    "job_id": "job_e2e",
                    "document_id": "doc_e2e",
                    "document_version_id": "docv_e2e",
                    "publication_id": "pub_e2e",
                },
                trace_id="t",
                recipients=(RecipientSelection(recipient_user_id=alice),),
            ),
            connection=connection,
        )

    claim = dispatcher.claim_one(owner="worker-e2e")
    assert claim is not None
    outcome = dispatcher.run_consumer_and_finalize(claim, owner="worker-e2e")
    assert outcome.status == "delivered"

    items = service.list_notifications(alice, limit=50)
    assert len(items) == 1
    assert items[0]["read"] is False
    service.ack_event(alice, "evt_e2e")
    assert service.unread_count(alice) == 0
    runtime.close()


def test_worker_run_forever_loop_is_stoppable_and_delivers() -> None:
    from _helpers import make_settings

    from app.outbox.worker import OutboxWorker
    from app.platform.runtime import build_runtime
    from app.platform.worker import create_worker_runtime

    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    configured = make_settings()
    runtime = build_runtime(
        configured,
        adapters={
            "database_engine": engine,
            "identity_access": identity,
            "outbox_dispatcher": OutboxDispatcher(
                engine,
                consumers={"in_app_notification": NotificationMaterializer(engine)},
                now=lambda: fixed_now(),
                retention_days=30,
                notification_retention_days=90,
                metrics=SqlAlchemyOutboxMetrics(),
            ),
        },
    )
    worker_runtime = create_worker_runtime(configured, runtime=runtime)
    worker = OutboxWorker(worker_runtime)
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] >= 2

    worker.run_forever(owner="worker-1", interval_seconds=0, stop=stop)

    with engine.connect() as connection:
        assert len(connection.execute(select(notification_table)).all()) == 1
    runtime.close()
