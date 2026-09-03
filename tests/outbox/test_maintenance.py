"""Regular retention maintenance: per-user 50-record cap and expiry receipting."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.outbox.dispatcher import OutboxDispatcher
from app.outbox.maintenance import NotificationRetentionMaintenance
from app.outbox.metrics import SqlAlchemyOutboxMetrics
from app.outbox.notifications import NotificationMaterializer
from app.outbox.schema import (
    notification_context_ack_table,
    notification_delivery_receipt_table,
    notification_inbox_table,
    notification_table,
)
from tests._support import (
    build_engine,
    build_identity_service,
    fixed_now,
    make_publisher,
    provision_user,
)


class _MutableClock:
    def __init__(self, start: datetime) -> None:
        self._current = start

    def now(self) -> datetime:
        return self._current

    def advance(self, seconds: int) -> None:
        self._current = self._current + timedelta(seconds=seconds)


def deliver(engine, *, user_ids, event_id, clock_now=None):
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
        now=clock_now or (lambda: fixed_now()),
        retention_days=30,
        notification_retention_days=90,
        metrics=SqlAlchemyOutboxMetrics(),
    )
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")


def notification_ids(engine, user_id: str) -> list[str]:
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(notification_table.c.id)
                .where(notification_table.c.recipient_user_id == user_id)
                .order_by(notification_table.c.notification_seq)
            )
            .scalars()
            .all()
        )
    return [str(row) for row in rows]


def receipt_events(engine, user_id: str) -> set[str]:
    with engine.connect() as connection:
        return set(
            connection.execute(
                select(notification_delivery_receipt_table.c.event_id).where(
                    notification_delivery_receipt_table.c.recipient_user_id == user_id
                )
            ).scalars()
        )


def test_maintenance_keeps_only_the_latest_50_notifications_per_user() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    for index in range(55):
        deliver(engine, user_ids=(alice,), event_id=f"evt_{index}")
    # 物化事务内裁剪已把该用户压到 50 条在线（evt_0..evt_4 已带收据）；补 5 条
    # 存量行（旧版本遗留数据语义）使后台 retire 任务扫到超限存量。
    with engine.begin() as connection:
        for index in range(5):
            connection.execute(
                notification_table.insert().values(
                    id=f"n_legacy_{index}",
                    event_id=f"evt_legacy_{index}",
                    recipient_user_id=alice,
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
    maintenance = NotificationRetentionMaintenance(engine, now=lambda: fixed_now())

    retired = maintenance.run_once(limit=200)

    assert retired >= 5
    remaining = notification_ids(engine, alice)
    assert len(remaining) == 50
    receipts = receipt_events(engine, alice)
    # 5 条来自物化内裁剪，5 条来自后台 retire。
    assert len(receipts) == 10
    with engine.connect() as connection:
        inbox = connection.execute(
            select(notification_inbox_table.c.next_notification_seq).where(
                notification_inbox_table.c.recipient_user_id == alice
            )
        ).scalar_one()
        # sequence/watermark never regress
        assert inbox == 56


def test_maintenance_receipts_expired_notifications_and_deletes_rows() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    deliver(engine, user_ids=(alice,), event_id="evt_1")
    deliver(engine, user_ids=(alice,), event_id="evt_2")
    # evt_1 expires long ago; evt_2 stays fresh.
    with engine.begin() as connection:
        connection.execute(
            update(notification_table)
            .where(notification_table.c.event_id == "evt_1")
            .values(retire_after_at_utc=datetime(2000, 1, 1, tzinfo=UTC))
        )
    maintenance = NotificationRetentionMaintenance(engine, now=lambda: fixed_now())

    retired = maintenance.run_once()

    assert retired == 1
    with engine.connect() as connection:
        assert (
            connection.execute(
                select(notification_table).where(notification_table.c.event_id == "evt_1")
            ).all()
            == []
        )
        assert (
            connection.execute(
                select(notification_context_ack_table).where(
                    notification_context_ack_table.c.event_id == "evt_1"
                )
            ).all()
            == []
        )
        assert "evt_1" in receipt_events(engine, alice)
        assert "evt_2" not in receipt_events(engine, alice)


def test_maintenance_is_idempotent_and_receipts_are_permanent() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    for index in range(52):
        deliver(engine, user_ids=(alice,), event_id=f"evt_{index}")
    # 物化事务内裁剪后无超限存量；补 2 条存量行（旧版本遗留数据语义）验证
    # 后台 retire 的幂等与收据永久性。
    with engine.begin() as connection:
        for index in range(2):
            connection.execute(
                notification_table.insert().values(
                    id=f"n_legacy_{index}",
                    event_id=f"evt_legacy_{index}",
                    recipient_user_id=alice,
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
    maintenance = NotificationRetentionMaintenance(engine, now=lambda: fixed_now())

    first = maintenance.run_once(limit=200)
    second = maintenance.run_once(limit=200)

    assert first >= 2
    assert second == 0
    with engine.connect() as connection:
        assert len(connection.execute(select(notification_table)).all()) == 50
        # 2 条来自物化内裁剪，2 条来自后台 retire；第二次运行零新增。
        assert len(connection.execute(select(notification_delivery_receipt_table)).all()) == 4


def test_maintenance_does_not_touch_unread_watermark_or_read_state() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    for index in range(51):
        deliver(engine, user_ids=(alice,), event_id=f"evt_{index}")
    from app.outbox.service import NotificationService

    service = NotificationService(engine, now=lambda: fixed_now())
    service.mark_read(alice, notification_ids(engine, alice)[0])
    service.read_all(alice)
    with engine.connect() as connection:
        before = (
            connection.execute(
                select(notification_inbox_table).where(
                    notification_inbox_table.c.recipient_user_id == alice
                )
            )
            .mappings()
            .one()
        )
    assert before["read_through_seq"] == 51

    maintenance = NotificationRetentionMaintenance(engine, now=lambda: fixed_now())
    maintenance.run_once(limit=200)

    with engine.connect() as connection:
        after = (
            connection.execute(
                select(notification_inbox_table).where(
                    notification_inbox_table.c.recipient_user_id == alice
                )
            )
            .mappings()
            .one()
        )
        assert after["read_through_seq"] == 51
        assert after["next_notification_seq"] == 52
