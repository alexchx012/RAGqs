"""Review gaps: read-all locking, ack atomicity, compaction completeness,
retirement lifecycle, archive verifier, ops strictness, schema constraints."""

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
from sqlalchemy import select, text, update

from app.outbox.dispatcher import OutboxDispatcher
from app.outbox.lifecycle import SqlAlchemyOutboxLifecycle
from app.outbox.metrics import SqlAlchemyOutboxMetrics
from app.outbox.notifications import NotificationMaterializer
from app.outbox.schema import (
    notification_context_ack_table,
    notification_delivery_receipt_table,
    outbox_event_table,
)
from app.outbox.service import NotificationService
from app.platform.errors import PlatformError


def make_lifecycle(engine, **kwargs):
    return SqlAlchemyOutboxLifecycle(engine, now=lambda: fixed_now(), **kwargs)


def deliver(engine, *, user_ids, event_id="evt_1", event_type="ingestion_completed"):
    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    publisher = make_publisher(engine, now=lambda: fixed_now())
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
    caller = "ingestion" if event_type.startswith("ingestion") else "submissions"
    aggregate = "ingestion_job" if event_type.startswith("ingestion") else "knowledge_submission"
    command = OutboxPublishCommand(
        event_id=event_id,
        caller_principal=caller,
        event_type=event_type,
        schema_version=1,
        aggregate_type=aggregate,
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
        metrics=SqlAlchemyOutboxMetrics(),
    )
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")


def retire_command(
    *,
    operation_id="op_ret_1",
    user_id="user_x",
    mode="durable",
    verified_archive_ref="archive_ref_1",
    archive_checksum="checksum_1",
):
    from app.outbox.ports import AccountNotificationRetirementCommand

    return AccountNotificationRetirementCommand(
        operation_id=operation_id,
        caller_principal="retention-ops",
        user_id=user_id,
        deletion_id="del_1",
        verified_archive_ref=verified_archive_ref,
        archive_checksum=archive_checksum,
        transaction_id="tx_1",
        mode=mode,
        canonical_input_fingerprint="fp_1",
    )


def test_read_all_advances_to_the_highest_materialized_seq_at_commit() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    deliver(engine, user_ids=(alice,), event_id="evt_2")
    service = NotificationService(engine, now=lambda: fixed_now())

    service.read_all(alice)

    from app.outbox.schema import notification_inbox_table

    with engine.connect() as connection:
        inbox = (
            connection.execute(
                select(notification_inbox_table).where(
                    notification_inbox_table.c.recipient_user_id == alice
                )
            )
            .mappings()
            .one()
        )
    assert inbox["read_through_seq"] == 2


def test_read_all_does_not_skip_notifications_materialized_after_the_call() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    service = NotificationService(engine, now=lambda: fixed_now())
    # Nothing materialized yet: read-all advances the watermark to 0.
    service.read_all(alice)

    deliver(engine, user_ids=(alice,), event_id="evt_1")
    items = service.list_notifications(alice, limit=50)
    assert items[0]["read"] is False


def test_ack_is_atomic_under_repeat_calls() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    service = NotificationService(engine, now=lambda: fixed_now())

    service.ack_event(alice, "evt_1")
    service.ack_event(alice, "evt_1")

    with engine.connect() as connection:
        rows = connection.execute(
            select(notification_context_ack_table).where(
                notification_context_ack_table.c.event_id == "evt_1"
            )
        ).all()
        assert len(rows) == 1


def test_ack_only_accepts_materialized_receipt_evidence() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    # Manually insert a recipient_inactive receipt for a different event.
    with engine.begin() as connection:
        connection.execute(
            notification_delivery_receipt_table.insert().values(
                event_id="evt_suppressed",
                recipient_user_id=alice,
                outcome="recipient_inactive",
                original_notification_seq=None,
                occurred_at_utc=fixed_now(),
                materialized_at_utc=None,
                retired_at_utc=fixed_now(),
                fingerprint="fp_suppressed",
            )
        )
    service = NotificationService(engine, now=lambda: fixed_now())

    with pytest.raises(PlatformError) as raised:
        service.ack_event(alice, "evt_suppressed")
    assert raised.value.status_code == 404


def test_ops_api_restricts_consumer_and_rejects_oversized_keys() -> None:
    from _helpers import make_settings
    from fastapi.testclient import TestClient

    from app.platform.app_factory import create_platform_app
    from app.platform.runtime import build_runtime

    configured = make_settings()
    engine = build_engine()
    identity = build_identity_service(engine)
    provision_user(identity, username="caller", role="ops")
    dispatcher = OutboxDispatcher(
        engine,
        consumers={"in_app_notification": NotificationMaterializer(engine)},
        now=lambda: fixed_now(),
        retention_days=30,
        notification_retention_days=90,
        metrics=SqlAlchemyOutboxMetrics(),
    )
    runtime = build_runtime(
        configured,
        adapters={"database_engine": engine, "identity_access": identity},
    )
    runtime.adapters["outbox_dispatcher"] = dispatcher
    app = create_platform_app(configured, runtime=runtime)
    token = identity.login(username="caller", password="Password1").access_token

    with TestClient(app) as client:
        bad_consumer = client.get(
            "/v1/ops/outbox-deliveries/evt_1?consumer_name=other",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert bad_consumer.status_code == 422
        oversized = client.post(
            "/v1/ops/outbox-deliveries/evt_1/replay",
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": "k" * 300,
            },
            json={"consumer_name": "in_app_notification", "expected_version": 1},
        )
        assert oversized.status_code == 422
    runtime.close()


def test_compaction_writes_receipts_for_suppressions_and_removes_all_full_only_rows() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    # Suppress alice for evt_1.
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
        from app.identity.schema import identity_user_table

        connection.execute(
            update(identity_user_table)
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
    from app.outbox.schema import notification_suppression_table

    with engine.connect() as connection:
        assert len(connection.execute(select(notification_suppression_table)).all()) == 1

    # Force the compaction to be due.
    with engine.begin() as connection:
        connection.execute(
            update(outbox_event_table)
            .where(outbox_event_table.c.event_id == "evt_1")
            .values(compact_after_at_utc=datetime(2000, 1, 1, tzinfo=UTC))
        )
    dispatcher.compact_due_events(now=datetime(2026, 8, 5, tzinfo=UTC))

    with engine.connect() as connection:
        event = (
            connection.execute(
                select(outbox_event_table).where(outbox_event_table.c.event_id == "evt_1")
            )
            .mappings()
            .one()
        )
        assert event["storage_state"] == "compacted"
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
        from app.outbox.schema import (
            outbox_delivery_attempt_table,
            outbox_delivery_table,
            outbox_recipient_table,
        )

        assert connection.execute(select(outbox_recipient_table)).all() == []
        assert connection.execute(select(outbox_delivery_table)).all() == []
        assert connection.execute(select(outbox_delivery_attempt_table)).all() == []
        assert connection.execute(select(notification_suppression_table)).all() == []


def test_retirement_rejects_an_account_not_in_a_deletable_lifecycle() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    lifecycle = make_lifecycle(engine)

    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            lifecycle.retire_account_notification_state(
                retire_command(user_id=alice, mode="inline"),
                connection=connection,
            )
    assert raised.value.status_code == 422


def test_retirement_requires_a_verifier_and_rejects_forged_archive_proof() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    from app.identity.schema import identity_user_table

    with engine.begin() as connection:
        connection.execute(
            update(identity_user_table)
            .where(identity_user_table.c.id == alice)
            .values(lifecycle_status="pending_delete")
        )

    class RejectingVerifier:
        def verify_archive(self, *, archive_ref: str, checksum: str, **kwargs) -> bool:
            del archive_ref, checksum
            return False

    lifecycle = make_lifecycle(engine, archive_verifier=RejectingVerifier())

    # A forged archive proof is rejected even though the command claims it.
    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            lifecycle.retire_account_notification_state(
                retire_command(
                    user_id=alice,
                    mode="inline",
                    verified_archive_ref="archive_ref_FORGED",
                    archive_checksum="checksum_FORGED",
                ),
                connection=connection,
            )
    assert raised.value.status_code == 422


def test_schema_has_check_constraints_for_state_domains() -> None:
    engine = build_engine()
    with engine.connect() as connection:
        sql = (
            connection.execute(text("SELECT sql FROM sqlite_master WHERE type='table'"))
            .scalars()
            .all()
        )
    joined = "\n".join(sql)
    # State domains are CHECK-constrained in the migration.
    assert "CHECK" in joined
