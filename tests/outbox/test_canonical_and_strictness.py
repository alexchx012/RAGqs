"""Important items I7-I11 and minors M1/M2: canonical fingerprints, retirement
lifecycle strictness, tombstone version scope, receipt unification, ack
no-op, retention inner limit and per-pass dead-letter counting."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from _helpers import (
    build_engine,
    build_identity_service,
    fixed_now,
    make_publisher,
    provision_user,
)
from sqlalchemy import select, update

from app.identity.schema import identity_user_table
from app.outbox.dispatcher import OutboxDispatcher
from app.outbox.lifecycle import SqlAlchemyOutboxLifecycle
from app.outbox.maintenance import NotificationRetentionMaintenance
from app.outbox.metrics import SqlAlchemyOutboxMetrics
from app.outbox.notifications import NotificationMaterializer
from app.outbox.schema import (
    notification_context_ack_table,
    notification_delivery_receipt_table,
    notification_table,
    outbox_document_tombstone_table,
    outbox_event_table,
)
from app.outbox.service import NotificationService
from app.platform.errors import PlatformError


class _Accepting:
    def verify_archive(self, *, archive_ref: str, checksum: str, **kwargs) -> bool:
        del archive_ref, checksum
        return True


def make_lifecycle(engine):
    return SqlAlchemyOutboxLifecycle(
        engine,
        now=lambda: fixed_now(),
        archive_verifier=_Accepting(),
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


def publish(engine, *, user_ids, event_id="evt_1", doc_id=None, docv_id=None):
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
            "document_id": doc_id or f"doc_{event_id}",
            "document_version_id": docv_id or f"docv_{event_id}",
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
        canonical_input_fingerprint="unused-client-value",
    )
    values.update(overrides)
    return AccountNotificationRetirementCommand(**values)


# ---------------------------------------------------------------------------
# I7: server-side canonical input fingerprint; same ID different input -> 409
# ---------------------------------------------------------------------------


def retire(engine, lifecycle, alice: str, **overrides):
    mark_deletable(engine, alice)
    with engine.begin() as connection:
        return lifecycle.retire_account_notification_state(
            retire_command(user_id=alice, **overrides),
            connection=connection,
        )


def test_retirement_same_operation_different_archive_checksum_is_409() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,))
    lifecycle = make_lifecycle(engine)
    retire(engine, lifecycle, alice)

    with pytest.raises(PlatformError) as raised:
        retire(engine, lifecycle, alice, archive_checksum="checksum_CHANGED")
    assert raised.value.status_code == 409
    assert raised.value.code == "idempotency_key_conflict"


def test_retirement_ignores_a_tampered_client_fingerprint() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,))
    lifecycle = make_lifecycle(engine)
    # The client-supplied canonical_input_fingerprint is never trusted: the
    # server recomputes it from the full input.
    retire(engine, lifecycle, alice, canonical_input_fingerprint="forged-client-fp")
    retire(engine, lifecycle, alice, canonical_input_fingerprint="forged-client-fp")

    with engine.connect() as connection:
        from app.outbox.schema import outbox_retirement_command_table

        row = (
            connection.execute(
                select(outbox_retirement_command_table).where(
                    outbox_retirement_command_table.c.operation_id == "op_ret_1"
                )
            )
            .mappings()
            .one()
        )
        assert row["input_fingerprint"] != "forged-client-fp"


def test_compaction_same_operation_different_transaction_is_409() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,))
    dispatcher = make_dispatcher(engine)
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
    lifecycle = make_lifecycle(engine)
    retire(engine, lifecycle, alice)
    from app.outbox.lifecycle import _command_input_fingerprint
    from app.outbox.ports import EligibleAccountEventCompactionCommand

    base = dict(
        operation_id="op_comp_1",
        caller_principal="retention-ops",
        user_id=alice,
        deletion_id="del_1",
        retirement_receipt_id="op_ret_1",
        retirement_receipt_fingerprint=_command_input_fingerprint(retire_command(user_id=alice)),
        transaction_id="tx_comp_1",
        canonical_input_fingerprint="unused",
    )

    def request(**overrides):
        with engine.begin() as connection:
            return lifecycle.request_eligible_account_event_compaction(
                EligibleAccountEventCompactionCommand(**{**base, **overrides}),
                connection=connection,
            )

    request()
    with pytest.raises(PlatformError) as raised:
        request(transaction_id="tx_comp_CHANGED")
    assert raised.value.status_code == 409


# ---------------------------------------------------------------------------
# I8: retirement only accepts a correct pending-delete lifecycle
# ---------------------------------------------------------------------------


def test_retirement_on_an_already_deleted_account_is_permanent_409() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,))
    with engine.begin() as connection:
        connection.execute(
            update(identity_user_table)
            .where(identity_user_table.c.id == alice)
            .values(lifecycle_status="deleted")
        )
    lifecycle = make_lifecycle(engine)

    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            lifecycle.retire_account_notification_state(
                retire_command(user_id=alice),
                connection=connection,
            )
    assert raised.value.status_code == 409
    assert raised.value.code == "account_already_deleted"


# ---------------------------------------------------------------------------
# I9: tombstone is per (document, version); exact version matching
# ---------------------------------------------------------------------------


def redact_command(
    *,
    document_id="doc_evt_1",
    document_version_ids=("docv_evt_1",),
    operation_id="op_1",
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
    )


def test_tombstone_tracks_document_and_version_independently() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    # Two events for the same document, different versions.
    publish(engine, user_ids=(alice,), event_id="evt_v1", doc_id="doc_1", docv_id="docv_1")
    publish(engine, user_ids=(alice,), event_id="evt_v2", doc_id="doc_1", docv_id="docv_2")
    lifecycle = make_lifecycle(engine)
    dispatcher = make_dispatcher(engine)

    # Redact only version docv_1.
    with engine.begin() as connection:
        lifecycle.redact_document_notifications(
            redact_command(document_id="doc_1", document_version_ids=("docv_1",)),
            connection=connection,
        )
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(
                    outbox_document_tombstone_table.c.document_id,
                    outbox_document_tombstone_table.c.document_version_id,
                )
            )
            .mappings()
            .all()
        )
        assert len(rows) == 1
        assert rows[0]["document_id"] == "doc_1"
        assert rows[0]["document_version_id"] == "docv_1"

    # Materialize both: only the docv_1 projection is redacted.
    for _ in range(2):
        claim = dispatcher.claim_one(owner="worker-1")
        assert claim is not None
        dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
    with engine.connect() as connection:
        v1 = (
            connection.execute(
                select(notification_table).where(notification_table.c.event_id == "evt_v1")
            )
            .mappings()
            .one()
        )
        v2 = (
            connection.execute(
                select(notification_table).where(notification_table.c.event_id == "evt_v2")
            )
            .mappings()
            .one()
        )
        assert v1["redacted"] is True
        assert v1["title"] == "Deleted document"
        assert v2["redacted"] is False
        assert v2["title"] == "Document ingestion completed"


def test_multiple_redaction_scopes_coexist() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_a", doc_id="doc_a", docv_id="docv_a")
    publish(engine, user_ids=(alice,), event_id="evt_b", doc_id="doc_b", docv_id="docv_b")
    lifecycle = make_lifecycle(engine)

    with engine.begin() as connection:
        lifecycle.redact_document_notifications(
            redact_command(document_id="doc_a", document_version_ids=("docv_a",)),
            connection=connection,
        )
    with engine.begin() as connection:
        lifecycle.redact_document_notifications(
            redact_command(
                operation_id="op_2",
                document_id="doc_b",
                document_version_ids=("docv_b",),
            ),
            connection=connection,
        )
    with engine.connect() as connection:
        rows = connection.execute(select(outbox_document_tombstone_table)).all()
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# I10: unified canonical receipt fingerprint; suppression occurred_at = event
# ---------------------------------------------------------------------------


def test_maintenance_and_compaction_share_the_canonical_receipt_fingerprint() -> None:
    from app.outbox.compaction import canonical_receipt_fingerprint

    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,))
    dispatcher = make_dispatcher(engine)
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
    # Retire evt_1 via the regular maintenance path.
    with engine.begin() as connection:
        connection.execute(
            update(notification_table)
            .where(notification_table.c.event_id == "evt_1")
            .values(retire_after_at_utc=datetime(2000, 1, 1, tzinfo=UTC))
        )
    NotificationRetentionMaintenance(engine, now=lambda: fixed_now()).run_once()

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
        expected = canonical_receipt_fingerprint(
            "evt_1", alice, "materialized", int(receipt["original_notification_seq"])
        )
        assert receipt["fingerprint"] == expected


def test_maintenance_delegates_materialized_receipt_fingerprints_to_the_canonical_helper(
    monkeypatch,
) -> None:
    import app.outbox.maintenance as maintenance_module

    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,))
    dispatcher = make_dispatcher(engine)
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
    with engine.begin() as connection:
        connection.execute(
            update(notification_table)
            .where(notification_table.c.event_id == "evt_1")
            .values(retire_after_at_utc=datetime(2000, 1, 1, tzinfo=UTC))
        )
    calls = []

    def canonical(event_id, user_id, outcome, seq):
        calls.append((event_id, user_id, outcome, seq))
        return "canonical-fingerprint"

    monkeypatch.setattr(
        maintenance_module,
        "canonical_receipt_fingerprint",
        canonical,
        raising=False,
    )

    NotificationRetentionMaintenance(engine, now=lambda: fixed_now()).run_once()

    assert calls == [("evt_1", alice, "materialized", 1)]
    with engine.connect() as connection:
        fingerprint = connection.execute(
            select(notification_delivery_receipt_table.c.fingerprint).where(
                notification_delivery_receipt_table.c.event_id == "evt_1"
            )
        ).scalar_one()
    assert fingerprint == "canonical-fingerprint"


def test_suppression_receipt_occurred_at_comes_from_the_event() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    # Suppress alice by deactivating her before materialization.
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
    with engine.connect() as connection:
        event = connection.execute(
            select(outbox_event_table.c.occurred_at_utc).where(
                outbox_event_table.c.event_id == "evt_1"
            )
        ).scalar_one()
    # Compact it and check the receipt's occurred_at equals the event time.

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
        assert receipt["occurred_at_utc"] == event


# ---------------------------------------------------------------------------
# I11: receipt-only ack is a 204 no-op and never rebuilds an ack row
# ---------------------------------------------------------------------------


def test_ack_on_retired_materialized_receipt_is_noop_without_ack_row() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,))
    dispatcher = make_dispatcher(engine)
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
    with engine.begin() as connection:
        connection.execute(
            update(notification_table)
            .where(notification_table.c.event_id == "evt_1")
            .values(retire_after_at_utc=datetime(2000, 1, 1, tzinfo=UTC))
        )
    NotificationRetentionMaintenance(engine, now=lambda: fixed_now()).run_once()

    service = NotificationService(engine, now=lambda: fixed_now())
    service.ack_event(alice, "evt_1")  # 204 semantics

    with engine.connect() as connection:
        ack = connection.execute(
            select(notification_context_ack_table).where(
                notification_context_ack_table.c.event_id == "evt_1"
            )
        ).all()
        # The retired materialized receipt is evidence; NO ack row is rebuilt.
        assert ack == []


# ---------------------------------------------------------------------------
# M1: retention inner loop honors the global limit
# ---------------------------------------------------------------------------


def test_retention_inner_loop_honors_the_global_limit() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    bob = provision_user(identity, username="bob")
    for user_id, prefix in ((alice, "a"), (bob, "b")):
        for index in range(55):
            publish(
                engine,
                user_ids=(user_id,),
                event_id=f"evt_{prefix}_{index}",
                doc_id=f"doc_{prefix}_{index}",
                docv_id=f"docv_{prefix}_{index}",
            )
    dispatcher = make_dispatcher(engine)
    for _ in range(110):
        claim = dispatcher.claim_one(owner="worker-1")
        if claim is None:
            break
        dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
    # 物化事务内裁剪已把每用户压到 50 条在线；再各补 5 条存量行（旧版本遗留
    # 数据语义）使其重新超限，后台 retire 任务才有存量可扫。
    for user_id, prefix in ((alice, "a"), (bob, "b")):
        with engine.begin() as connection:
            for index in range(5):
                connection.execute(
                    notification_table.insert().values(
                        id=f"n_legacy_{prefix}_{index}",
                        event_id=f"evt_legacy_{prefix}_{index}",
                        recipient_user_id=user_id,
                        notification_type="ingestion_completed",
                        title="Legacy stock",
                        payload_json={},
                        document_id=None,
                        document_version_id=None,
                        event_occurred_at_utc=fixed_now(),
                        materialized_at_utc=fixed_now(),
                        notification_seq=1000 + index,
                        read_at_utc=None,
                        retire_after_at_utc=fixed_now() + timedelta(days=90),
                        redacted=False,
                    )
                )

    retired = NotificationRetentionMaintenance(engine, now=lambda: fixed_now()).run_once(limit=5)

    assert retired == 5


# ---------------------------------------------------------------------------
# M2: worker dead_lettered counts only this pass
# ---------------------------------------------------------------------------


def test_worker_dead_lettered_counts_only_this_passes_transitions() -> None:
    from _helpers import make_settings

    from app.outbox.schema import outbox_delivery_table
    from app.outbox.worker import OutboxWorker
    from app.platform.runtime import build_runtime
    from app.platform.worker import create_worker_runtime

    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    # A stale dead-letter row from a previous pass.
    with engine.begin() as connection:
        connection.execute(
            outbox_delivery_table.insert().values(
                event_id="evt_stale",
                consumer_name="in_app_notification",
                status="dead_letter",
                version=5,
                replay_generation=1,
                attempt_number=8,
                cycle_attempt_number=8,
                error_category="permanent",
                error_code="unsupported_schema",
                next_attempt_at_utc=None,
                lease_owner=None,
                lease_expires_at_utc=None,
                fence_token=None,
                delivered_at_utc=None,
            )
        )
    bare = OutboxDispatcher(
        engine,
        consumers={},
        now=lambda: fixed_now(),
        retention_days=30,
        notification_retention_days=90,
        metrics=SqlAlchemyOutboxMetrics(),
    )
    configured = make_settings()
    runtime = build_runtime(
        configured,
        adapters={
            "database_engine": engine,
            "identity_access": identity,
            "outbox_dispatcher": bare,
        },
    )
    worker_runtime = create_worker_runtime(configured, runtime=runtime)
    worker = OutboxWorker(worker_runtime)

    stats = worker.run_once(owner="worker-1")

    # Only the evt_1 transition in this pass is counted; the stale row is not.
    assert stats.dead_lettered == 1
    runtime.close()
