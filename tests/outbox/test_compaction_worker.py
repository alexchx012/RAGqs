"""Blocker 6: outbox-owned compaction worker re-evaluates accepted compaction
commands, compacts only fully-delivered events, keeps blocked accepted, and
advances to completed with cumulative counts."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update

from app.identity.schema import identity_user_table
from app.outbox.dispatcher import OutboxDispatcher
from app.outbox.lifecycle import SqlAlchemyOutboxLifecycle, _command_input_fingerprint
from app.outbox.metrics import SqlAlchemyOutboxMetrics
from app.outbox.notifications import NotificationMaterializer
from app.outbox.schema import (
    outbox_compaction_command_table,
    outbox_event_table,
)
from app.platform.runtime import build_runtime
from tests._support import (
    build_engine,
    build_identity_service,
    fixed_now,
    make_publisher,
    make_settings,
    provision_user,
)


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


def publish(engine, *, user_ids, event_id="evt_1"):
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


def retire_account(engine, lifecycle, alice: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            update(identity_user_table)
            .where(identity_user_table.c.id == alice)
            .values(lifecycle_status="pending_delete")
        )
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


def _retirement_command(alice: str):
    from app.outbox.ports import AccountNotificationRetirementCommand

    return AccountNotificationRetirementCommand(
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


def request_compaction(engine, lifecycle, alice: str, *, operation_id="op_comp_1"):
    from app.outbox.ports import EligibleAccountEventCompactionCommand

    with engine.begin() as connection:
        return lifecycle.request_eligible_account_event_compaction(
            EligibleAccountEventCompactionCommand(
                operation_id=operation_id,
                caller_principal="retention-ops",
                user_id=alice,
                deletion_id="del_1",
                retirement_receipt_id="op_ret_1",
                retirement_receipt_fingerprint=_command_input_fingerprint(
                    _retirement_command(alice)
                ),
                transaction_id="tx_2",
                canonical_input_fingerprint="fp_2",
            ),
            connection=connection,
        )


def make_compaction_worker(engine, *, now=None):
    configured = make_settings()
    runtime = build_runtime(
        configured,
        adapters={
            "database_engine": engine,
            "outbox_lifecycle": SqlAlchemyOutboxLifecycle(
                engine,
                now=lambda: fixed_now(),
            ),
        },
    )
    return runtime.resolve("compaction_worker")


def test_accepted_compaction_worker_advances_to_completed_after_delivery() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    publish(engine, user_ids=(alice,), event_id="evt_2")
    dispatcher = make_dispatcher(engine)
    # evt_1 delivered; evt_2 stays pending.
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
    lifecycle = make_lifecycle(engine)
    retire_account(engine, lifecycle, alice)
    receipt = request_compaction(engine, lifecycle, alice)
    assert receipt.state == "accepted"
    assert receipt.compacted_count == 1

    # Worker run: evt_2 still pending -> stays accepted with counts carried.
    worker = make_compaction_worker(engine)
    stats = worker.run_once(owner="worker-1")
    assert stats.processed == 1
    assert stats.completed == 0
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
        assert stored["state"] == "accepted"

    # Now deliver evt_2; the worker completes the command and compacts both.
    claim2 = dispatcher.claim_one(owner="worker-1")
    assert claim2 is not None
    dispatcher.run_consumer_and_finalize(claim2, owner="worker-1")
    from app.platform.database import platform_lease_table

    with engine.begin() as connection:
        connection.execute(
            update(platform_lease_table)
            .where(platform_lease_table.c.resource == "outbox-compaction:op_comp_1")
            .values(expires_at_utc=datetime(2000, 1, 1, tzinfo=UTC))
        )
    stats2 = worker.run_once(owner="worker-1")
    assert stats2.completed == 1
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
        assert stored["completed_at_utc"] is not None
        receipt_json = stored["receipt_json"]
        assert receipt_json["compacted_count"] >= 2
        for event_id in ("evt_1", "evt_2"):
            state = connection.execute(
                select(outbox_event_table.c.storage_state).where(
                    outbox_event_table.c.event_id == event_id
                )
            ).scalar_one()
            assert state == "compacted"


def test_compaction_worker_is_idempotent_for_completed_commands() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publish(engine, user_ids=(alice,), event_id="evt_1")
    dispatcher = make_dispatcher(engine)
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")
    lifecycle = make_lifecycle(engine)
    retire_account(engine, lifecycle, alice)
    receipt = request_compaction(engine, lifecycle, alice)
    assert receipt.state == "completed"

    worker = make_compaction_worker(engine)
    stats = worker.run_once(owner="worker-1")
    assert stats.processed == 0
    assert stats.completed == 0


def _build_runtime_with_worker(engine):
    configured = make_settings()
    runtime = build_runtime(
        configured,
        adapters={
            "database_engine": engine,
            "outbox_lifecycle": SqlAlchemyOutboxLifecycle(
                engine,
                now=lambda: fixed_now(),
            ),
        },
    )
    return configured, runtime


def test_run_compaction_worker_once_resolves_the_assembled_worker() -> None:
    """One-shot entry resolves the build_runtime-assembled worker (injected
    lifecycle), never a rebuilt `CompactionWorker(worker_runtime)`."""
    from app.outbox.compaction_worker import run_compaction_worker_once

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
    retire_account(engine, lifecycle, alice)
    request_compaction(engine, lifecycle, alice)

    configured, runtime = _build_runtime_with_worker(engine)
    assert runtime.resolve("compaction_worker") is not None
    stats = run_compaction_worker_once(configured, runtime=runtime, owner="worker-1")
    assert stats.processed == 1
    assert stats.completed == 0
    runtime.close()


def test_create_compaction_worker_returns_the_assembled_worker() -> None:
    """create_compaction_worker() resolves the already-assembled worker from
    build_runtime (same object), so the console path never rebuilds it."""
    from app.outbox.compaction_worker import create_compaction_worker

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
    retire_account(engine, lifecycle, alice)
    request_compaction(engine, lifecycle, alice)

    configured, runtime = _build_runtime_with_worker(engine)
    worker, stats = create_compaction_worker(configured, runtime=runtime, owner="worker-1")
    assert worker is runtime.resolve("compaction_worker")
    assert stats.processed == 1
    assert stats.completed == 0
    runtime.close()


def test_compaction_worker_console_main_enters_run_forever(monkeypatch) -> None:
    """The console main() path resolves the assembled worker and enters
    run_forever (not RuntimeError), using the same resolution as
    create_compaction_worker()."""
    import app.outbox.compaction_worker as module

    engine = build_engine()
    configured, runtime = _build_runtime_with_worker(engine)

    entered = {"n": 0}
    original_forever = module.CompactionWorker.run_forever

    def fake_forever(self, *, owner, interval_seconds=60.0, limit=100, stop=None):
        del self, owner, interval_seconds, limit, stop
        entered["n"] += 1

    monkeypatch.setattr(module, "build_runtime", lambda settings: runtime)
    monkeypatch.setattr(module, "load_platform_settings", lambda: configured)
    monkeypatch.setattr(module.CompactionWorker, "run_forever", fake_forever)
    # The assembled worker is what the console loop drives.
    assembled = runtime.resolve("compaction_worker")
    assert assembled is not None
    try:
        module.main()
    finally:
        monkeypatch.setattr(module.CompactionWorker, "run_forever", original_forever)
    assert entered["n"] == 1
    runtime.close()
