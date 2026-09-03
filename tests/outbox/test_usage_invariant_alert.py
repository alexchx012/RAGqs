"""Usage ledger invariant conflict alerts are ops-visible outbox notifications.

A45：计量账本唯一键异指纹冲突 → 高优先级不变量告警（复用既有 outbox 事件/告警
通道，接收者为全部 active ops role snapshot）。发布失败与既有 409/回滚语义解耦，
本文件只验证 outbox 通道本身的登记、接收者与站内通知物化。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, update

from app.identity.schema import identity_user_table
from app.outbox.dispatcher import OutboxDispatcher
from app.outbox.notifications import NotificationMaterializer
from app.outbox.publisher import SqlAlchemyUsageInvariantAlertAdapter
from app.outbox.schema import notification_table, outbox_event_table, outbox_recipient_table
from app.platform.errors import PlatformError
from tests._support import (
    build_engine,
    build_identity_service,
    fixed_now,
    make_publisher,
    provision_user,
)


def _provision_ops_mix(engine) -> str:
    identity = build_identity_service(engine)
    active_ops = provision_user(identity, username="alert-ops", role="ops")
    inactive_ops = provision_user(identity, username="resting-ops", role="ops")
    provision_user(identity, username="plain-user", role="user")
    with engine.begin() as connection:
        connection.execute(
            update(identity_user_table)
            .where(identity_user_table.c.id == inactive_ops)
            .values(lifecycle_status="inactive")
        )
    return active_ops


def test_usage_invariant_conflict_alert_reaches_active_ops_notifications() -> None:
    engine = build_engine()
    active_ops = _provision_ops_mix(engine)
    adapter = SqlAlchemyUsageInvariantAlertAdapter(
        engine, make_publisher(engine, now=lambda: fixed_now())
    )

    with engine.begin() as connection:
        event_id = adapter.publish_usage_ledger_invariant_conflict(
            unique_key_fields=["provider_call_id"],
            provider_call_id="pc_conflict_1",
        )

    with engine.connect() as connection:
        event = (
            connection.execute(
                select(outbox_event_table).where(outbox_event_table.c.event_id == event_id)
            )
            .mappings()
            .one()
        )
        assert event["event_type"] == "usage_ledger_invariant_conflict"
        assert event["aggregate_type"] == "usage_ledger_row"
        assert event["aggregate_id"] == "pc_conflict_1"
        assert event["payload_json"] == {
            "unique_key_fields": ["provider_call_id"],
            "provider_call_id": "pc_conflict_1",
        }
        recipients = connection.execute(select(outbox_recipient_table)).mappings().all()
        assert [recipient["recipient_user_id"] for recipient in recipients] == [active_ops]
        assert {recipient["recipient_kind"] for recipient in recipients} == {"role_snapshot"}
        assert {recipient["required_role"] for recipient in recipients} == {"ops"}

    dispatcher = OutboxDispatcher(
        engine,
        consumers={"in_app_notification": NotificationMaterializer(engine)},
        now=lambda: fixed_now(),
    )
    claim = dispatcher.claim_one(owner="worker-1")
    assert claim is not None
    assert dispatcher.run_consumer_and_finalize(claim, owner="worker-1").status == "delivered"

    with engine.connect() as connection:
        notifications = connection.execute(select(notification_table)).mappings().all()
    assert {notification["title"] for notification in notifications} == {
        "Usage ledger invariant conflict"
    }
    assert all(
        "https://" not in str(notification["payload_json"]) for notification in notifications
    )


def test_usage_invariant_conflict_alert_reuses_event_for_the_same_row() -> None:
    """同一账本行的重复冲突按 (aggregate, transition) 唯一键复用同一告警事件。"""
    engine = build_engine()
    _provision_ops_mix(engine)
    adapter = SqlAlchemyUsageInvariantAlertAdapter(
        engine, make_publisher(engine, now=lambda: fixed_now())
    )
    with engine.begin() as connection:
        first = adapter.publish_usage_ledger_invariant_conflict(
            unique_key_fields=["provider_call_id"],
            provider_call_id="pc_conflict_1",
        )
        second = adapter.publish_usage_ledger_invariant_conflict(
            unique_key_fields=["provider_call_id"],
            provider_call_id="pc_conflict_1",
        )
    assert first == second
    with engine.connect() as connection:
        events = (
            connection.execute(
                select(outbox_event_table.c.event_id).where(
                    outbox_event_table.c.event_type == "usage_ledger_invariant_conflict"
                )
            )
            .scalars()
            .all()
        )
    assert events == [first]


def test_usage_invariant_conflict_alert_requires_key_fields() -> None:
    engine = build_engine()
    _provision_ops_mix(engine)
    adapter = SqlAlchemyUsageInvariantAlertAdapter(
        engine, make_publisher(engine, now=lambda: fixed_now())
    )
    with pytest.raises(PlatformError) as exc:
        with engine.begin() as connection:
            adapter.publish_usage_ledger_invariant_conflict(unique_key_fields=[])
    assert exc.value.code == "invalid_event_payload"
    with engine.connect() as connection:
        assert connection.execute(select(outbox_event_table)).first() is None
