"""Immediate dead-letter alerting and dispatcher metric completeness."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from _helpers import (
    build_engine,
    build_identity_service,
    fixed_now,
    make_publisher,
    provision_user,
)
from sqlalchemy import select

from app.outbox.dispatcher import OutboxDispatcher
from app.outbox.metrics import SqlAlchemyOutboxMetrics
from app.outbox.notifications import NotificationMaterializer
from app.outbox.schema import outbox_event_table, outbox_metric_table
from app.platform.database import platform_audit_table


class _MutableClock:
    def __init__(self, start: datetime) -> None:
        self._current = start

    def now(self) -> datetime:
        return self._current

    def advance(self, seconds: int) -> None:
        from datetime import timedelta

        self._current = self._current + timedelta(seconds=seconds)


def make_dispatcher(engine, *, now=None, metrics=None):
    materializer = NotificationMaterializer(engine, notification_retention_days=90)
    return OutboxDispatcher(
        engine,
        consumers={"in_app_notification": materializer},
        now=now or (lambda: fixed_now()),
        retention_days=30,
        notification_retention_days=90,
        metrics=metrics if metrics is not None else SqlAlchemyOutboxMetrics(),
    )


def publish(engine, publisher, *, user_ids, event_id="evt_1"):
    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

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


def metric_names(engine) -> set[str]:
    with engine.connect() as connection:
        return set(connection.execute(select(outbox_metric_table.c.metric_name)).scalars())


def test_dead_letter_writes_an_immediate_consumable_alert_and_metrics(
    caplog,
) -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())
    publish(engine, publisher, user_ids=(alice,))
    dispatcher = make_dispatcher(engine)
    caplog.set_level(logging.ERROR, logger="app.outbox.dispatcher")

    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.fail_and_schedule(
        claim,
        owner="worker-1",
        error_category="permanent",
        error_code="unsupported_schema",
    )

    with engine.connect() as connection:
        alerts = (
            connection.execute(
                select(platform_audit_table).where(
                    platform_audit_table.c.resource_type == "outbox_delivery",
                    platform_audit_table.c.result == "dead_lettered",
                )
            )
            .mappings()
            .all()
        )
        assert len(alerts) == 1
        assert alerts[0]["resource_id"] == "evt_1"
        assert alerts[0]["actor_id"] == "system:outbox"
    names = metric_names(engine)
    assert "outbox.deliveries.dead_letter" in names
    assert "outbox.deliveries.status.dead_letter" in names
    assert any("dead-lettered" in record.message for record in caplog.records)


def test_delivered_records_latency_and_status_metrics() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())
    publish(engine, publisher, user_ids=(alice,))
    dispatcher = make_dispatcher(engine)

    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.run_consumer_and_finalize(claim, owner="worker-1")

    names = metric_names(engine)
    assert "outbox.deliveries.status.delivered" in names
    assert "outbox.deliveries.latency_ms" in names
    with engine.connect() as connection:
        latency = connection.execute(
            select(outbox_metric_table.c.value).where(
                outbox_metric_table.c.metric_name == "outbox.deliveries.latency_ms"
            )
        ).scalar_one()
        assert float(latency) >= 0


def test_retry_wait_records_retry_and_status_metrics() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())
    publish(engine, publisher, user_ids=(alice,))
    dispatcher = make_dispatcher(engine)

    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    dispatcher.fail_and_schedule(
        claim, owner="worker-1", error_category="retryable", error_code="connection_error"
    )

    names = metric_names(engine)
    assert "outbox.deliveries.status.retry_wait" in names
    assert "outbox.deliveries.retry" in names


def test_lease_expiry_records_lease_expired_metric() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())
    publish(engine, publisher, user_ids=(alice,))
    clock = _MutableClock(datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC))
    dispatcher = make_dispatcher(engine, now=clock.now)

    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    clock.advance(seconds=120)
    dispatcher.recycle_expired_running()

    names = metric_names(engine)
    assert "outbox.deliveries.lease_expired" in names


def test_compact_due_events_prunes_expired_metrics_without_due_event() -> None:
    engine = build_engine()
    current = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)
    dispatcher = make_dispatcher(engine, now=lambda: current)
    with engine.begin() as connection:
        connection.execute(
            outbox_metric_table.insert(),
            [
                {
                    "metric_name": "outbox.deliveries.expired",
                    "observed_at_utc": current - timedelta(days=31),
                    "value": 1.0,
                    "event_id": None,
                },
                {
                    "metric_name": "outbox.deliveries.recent",
                    "observed_at_utc": current - timedelta(days=30),
                    "value": 1.0,
                    "event_id": None,
                },
            ],
        )

    assert dispatcher.compact_due_events(now=current) == 0

    assert metric_names(engine) == {"outbox.deliveries.recent"}


def test_oldest_pending_metric_is_recorded_when_claiming() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())
    publish(engine, publisher, user_ids=(alice,))
    dispatcher = make_dispatcher(engine)

    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None

    names = metric_names(engine)
    assert "outbox.deliveries.oldest_pending_seconds" in names


def test_zero_recipient_role_event_keeps_delivery_and_records_metric() -> None:
    """C5-1/#44：无 active ops 时发布角色快照事件——事件照常提交（业务状态不
    回滚）、pending delivery 存在、dispatcher 以零接收者完成并产生指标。"""
    engine = build_engine()
    build_identity_service(engine)  # 无任何 ops 用户
    publisher = make_publisher(engine, now=lambda: fixed_now())
    from app.outbox.ports import OutboxPublishCommand

    command = OutboxPublishCommand(
        event_id="evt_zero_ops",
        event_type="calibration_window_suggested",
        caller_principal="calibration",
        schema_version=1,
        aggregate_type="calibration_window_suggestion",
        aggregate_id="cws_zero",
        transition_version=1,
        occurred_at=fixed_now(),
        payload={"calibration_window_suggestion_id": "cws_zero"},
        trace_id="trace_zero",
        recipients=(),
    )
    with engine.begin() as connection:
        publisher.publish(command, connection=connection)

    dispatcher = make_dispatcher(engine)
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    assert claim.event_id == "evt_zero_ops"
    assert dispatcher.run_consumer_and_finalize(claim, owner="worker-1").status == "delivered"

    assert "outbox.deliveries.zero_recipients" in metric_names(engine)
    with engine.connect() as connection:
        event = (
            connection.execute(
                select(outbox_event_table.c.storage_state).where(
                    outbox_event_table.c.event_id == "evt_zero_ops"
                )
            )
            .scalars()
            .one()
        )
    assert event == "full"
