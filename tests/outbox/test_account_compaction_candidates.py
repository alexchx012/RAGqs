"""Account event compaction resolves candidates with one aggregate query.

The account retirement/deletion compaction path used to walk every event the
user ever received with two roundtrips each (storage_state + non-delivered
count). The candidate sweep now classifies all of them in one join/aggregate
statement and only hands all-delivered candidates to compact_event, so
blocked (still-pending) and already-compacted events cost no per-event
roundtrips while the classification stays identical to the per-event path.
"""

from __future__ import annotations

from _helpers import (
    build_engine,
    build_identity_service,
    fixed_now,
    make_publisher,
    provision_user,
)
from sqlalchemy import event, func, select, update

from app.identity.schema import identity_user_table
from app.outbox.compaction import compact_event
from app.outbox.lifecycle import SqlAlchemyOutboxLifecycle, _command_input_fingerprint
from app.outbox.ports import (
    AccountNotificationRetirementCommand,
    EligibleAccountEventCompactionCommand,
    OutboxPublishCommand,
    RecipientSelection,
)
from app.outbox.schema import (
    outbox_delivery_table,
    outbox_event_table,
    outbox_recipient_table,
)


class _AcceptingArchiveVerifier:
    def verify_archive(self, *, archive_ref: str, checksum: str, **kwargs) -> bool:
        del archive_ref, checksum
        return True


def make_lifecycle(engine) -> SqlAlchemyOutboxLifecycle:
    return SqlAlchemyOutboxLifecycle(
        engine,
        now=lambda: fixed_now(),
        archive_verifier=_AcceptingArchiveVerifier(),
    )


def _retirement_command(user_id: str) -> AccountNotificationRetirementCommand:
    return AccountNotificationRetirementCommand(
        operation_id="op_ret_1",
        caller_principal="retention-ops",
        user_id=user_id,
        deletion_id="del_ret_1",
        verified_archive_ref="archive_ref_1",
        archive_checksum="checksum_1",
        transaction_id="tx_ret_1",
        mode="inline",
        canonical_input_fingerprint="fp_ret_1",
    )


def publish(engine, *, user_id: str, event_id: str, deliver_event: bool) -> None:
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
        recipients=(RecipientSelection(recipient_user_id=user_id),),
    )
    with engine.begin() as connection:
        publisher.publish(command, connection=connection)
    if not deliver_event:
        return
    from app.outbox.dispatcher import OutboxDispatcher
    from app.outbox.notifications import NotificationMaterializer

    dispatcher = OutboxDispatcher(
        engine,
        consumers={"in_app_notification": NotificationMaterializer(engine)},
        now=lambda: fixed_now(),
        retention_days=30,
        notification_retention_days=90,
    )
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")


def retire_account(engine, lifecycle: SqlAlchemyOutboxLifecycle, user_id: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            update(identity_user_table)
            .where(identity_user_table.c.id == user_id)
            .values(lifecycle_status="pending_delete")
        )
        lifecycle.retire_account_notification_state(
            _retirement_command(user_id), connection=connection
        )


def request_compaction(engine, lifecycle: SqlAlchemyOutboxLifecycle, user_id: str):
    with engine.begin() as connection:
        return lifecycle.request_eligible_account_event_compaction(
            EligibleAccountEventCompactionCommand(
                operation_id="op_comp_1",
                caller_principal="retention-ops",
                user_id=user_id,
                deletion_id="del_ret_1",
                retirement_receipt_id="op_ret_1",
                retirement_receipt_fingerprint=_command_input_fingerprint(
                    _retirement_command(user_id)
                ),
                transaction_id="tx_comp_1",
                canonical_input_fingerprint="fp_comp_1",
            ),
            connection=connection,
        )


def classify_per_event_path(engine, user_id: str) -> tuple[set[str], set[str]]:
    """The classification the former per-event loop computed, query by query."""
    eligible: set[str] = set()
    blocked: set[str] = set()
    with engine.connect() as connection:
        event_ids = (
            connection.execute(
                select(outbox_recipient_table.c.event_id).where(
                    outbox_recipient_table.c.recipient_user_id == user_id
                )
            )
            .scalars()
            .all()
        )
        for event_id in event_ids:
            row = (
                connection.execute(
                    select(
                        outbox_event_table.c.storage_state,
                        outbox_event_table.c.compacted_at_utc,
                    ).where(outbox_event_table.c.event_id == event_id)
                )
                .mappings()
                .one_or_none()
            )
            if row is None or row["storage_state"] != "full":
                continue
            if row["compacted_at_utc"] is not None:
                continue
            non_delivered = connection.execute(
                select(func.count())
                .select_from(outbox_delivery_table)
                .where(
                    outbox_delivery_table.c.event_id == event_id,
                    outbox_delivery_table.c.status != "delivered",
                )
            ).scalar_one()
            if int(non_delivered) != 0:
                blocked.add(event_id)
            else:
                eligible.add(event_id)
    return eligible, blocked


def test_compaction_sweep_compacts_only_candidates_and_skips_the_rest() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    lifecycle = make_lifecycle(engine)

    pre_compacted = [f"evt_pre_{i}" for i in range(6)]
    candidates = [f"evt_cand_{i}" for i in range(3)]
    blocked = [f"evt_pend_{i}" for i in range(12)]
    for event_id in pre_compacted:
        publish(engine, user_id=alice, event_id=event_id, deliver_event=True)
    with engine.begin() as connection:
        for event_id in pre_compacted:
            assert compact_event(connection, event_id, fixed_now())
    for event_id in candidates:
        publish(engine, user_id=alice, event_id=event_id, deliver_event=True)
    for event_id in blocked:
        publish(engine, user_id=alice, event_id=event_id, deliver_event=False)
    retire_account(engine, lifecycle, alice)

    captured: list[tuple[str, object]] = []

    def record_statement(conn, cursor, statement, parameters, context, executemany):
        del conn, cursor, context, executemany
        captured.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        receipt = request_compaction(engine, lifecycle, alice)
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)

    assert receipt.eligible_count == 3
    assert receipt.compacted_count == 3
    assert receipt.blocked_count == 12
    assert receipt.state == "accepted"

    # Already-compacted and still-pending events produce no roundtrips at all:
    # neither their identifiers nor any per-event statement shape appears.
    untouched_ids = pre_compacted + blocked
    touched = [
        (statement, parameters)
        for statement, parameters in captured
        if any(event_id in statement or event_id in str(parameters) for event_id in untouched_ids)
    ]
    assert touched == []
    # Exactly one SELECT drives the whole sweep over the recipient table.
    sweep_statements = [
        statement
        for statement, _ in captured
        if statement.lstrip().upper().startswith("SELECT") and "FROM outbox_recipient" in statement
    ]
    assert len(sweep_statements) == 1

    with engine.connect() as connection:
        for event_id in candidates:
            assert (
                connection.execute(
                    select(outbox_event_table.c.storage_state).where(
                        outbox_event_table.c.event_id == event_id
                    )
                ).scalar_one()
                == "compacted"
            )
            assert (
                connection.execute(
                    select(func.count())
                    .select_from(outbox_recipient_table)
                    .where(outbox_recipient_table.c.event_id == event_id)
                ).scalar_one()
                == 0
            )
        for event_id in blocked:
            assert (
                connection.execute(
                    select(outbox_event_table.c.storage_state).where(
                        outbox_event_table.c.event_id == event_id
                    )
                ).scalar_one()
                == "full"
            )


def test_sweep_classification_matches_the_per_event_path() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    lifecycle = make_lifecycle(engine)

    for event_id in (f"evt_cand_{i}" for i in range(4)):
        publish(engine, user_id=alice, event_id=event_id, deliver_event=True)
    for event_id in (f"evt_pend_{i}" for i in range(5)):
        publish(engine, user_id=alice, event_id=event_id, deliver_event=False)
    # One delivered event was already compacted by an earlier command: the
    # sweep must skip it, not count it eligible or blocked.
    with engine.begin() as connection:
        assert compact_event(connection, "evt_cand_0", fixed_now())
    retire_account(engine, lifecycle, alice)

    expected_eligible, expected_blocked = classify_per_event_path(engine, alice)
    assert expected_eligible == {f"evt_cand_{i}" for i in (1, 2, 3)}
    assert expected_blocked == {f"evt_pend_{i}" for i in range(5)}

    receipt = request_compaction(engine, lifecycle, alice)

    assert receipt.eligible_count == len(expected_eligible)
    assert receipt.compacted_count == len(expected_eligible)
    assert receipt.blocked_count == len(expected_blocked)
    with engine.connect() as connection:
        for event_id in expected_eligible:
            assert (
                connection.execute(
                    select(outbox_event_table.c.storage_state).where(
                        outbox_event_table.c.event_id == event_id
                    )
                ).scalar_one()
                == "compacted"
            )
        for event_id in expected_blocked:
            assert (
                connection.execute(
                    select(outbox_event_table.c.storage_state).where(
                        outbox_event_table.c.event_id == event_id
                    )
                ).scalar_one()
                == "full"
            )
            assert (
                connection.execute(
                    select(outbox_delivery_table.c.status).where(
                        outbox_delivery_table.c.event_id == event_id
                    )
                ).scalar_one()
                == "pending"
            )

    # Idempotent: the same operation returns the stored receipt unchanged.
    again = request_compaction(engine, lifecycle, alice)
    assert again.compacted_count == receipt.compacted_count
    assert again.eligible_count == receipt.eligible_count
    assert again.state == receipt.state
