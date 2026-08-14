"""Startup configuration alerts are normal in-app outbox notifications."""

from __future__ import annotations

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
from app.outbox.notifications import NotificationMaterializer
from app.outbox.publisher import SqlAlchemyStartupConfigurationAlertAdapter
from app.outbox.schema import (
    notification_table,
    outbox_event_table,
    outbox_recipient_table,
)


def test_missing_evaluation_judge_configuration_alerts_active_ops_without_values() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    active_ops = provision_user(identity, username="active-ops", role="ops")
    inactive_ops = provision_user(identity, username="inactive-ops", role="ops")
    provision_user(identity, username="active-user", role="user")
    with engine.begin() as connection:
        connection.execute(
            update(identity_user_table)
            .where(identity_user_table.c.id == inactive_ops)
            .values(lifecycle_status="inactive")
        )

    adapter = SqlAlchemyStartupConfigurationAlertAdapter(
        make_publisher(engine, now=lambda: fixed_now())
    )
    with engine.begin() as connection:
        first_event_id = adapter.publish_missing_evaluation_judge_configuration(
            missing_variable_names=(
                "RAG_EVALUATION_JUDGE_BASE_URL",
                "RAG_EVALUATION_JUDGE_API_KEY",
            ),
            occurred_at=fixed_now(),
            connection=connection,
        )
    with engine.begin() as connection:
        second_event_id = adapter.publish_missing_evaluation_judge_configuration(
            missing_variable_names=("RAG_EVALUATION_JUDGE_API_KEY",),
            occurred_at=fixed_now(),
            connection=connection,
        )

    assert first_event_id != second_event_id
    with engine.connect() as connection:
        events = connection.execute(select(outbox_event_table)).mappings().all()
        assert {event["event_id"] for event in events} == {first_event_id, second_event_id}
        assert {event["event_type"] for event in events} == {
            "evaluation_judge_configuration_missing"
        }
        assert {tuple(event["payload_json"]["missing_variable_names"]) for event in events} == {
            ("RAG_EVALUATION_JUDGE_BASE_URL", "RAG_EVALUATION_JUDGE_API_KEY"),
            ("RAG_EVALUATION_JUDGE_API_KEY",),
        }
        recipients = connection.execute(select(outbox_recipient_table)).mappings().all()
        assert {recipient["recipient_user_id"] for recipient in recipients} == {active_ops}
        assert {recipient["recipient_kind"] for recipient in recipients} == {"role_snapshot"}
        assert {recipient["required_role"] for recipient in recipients} == {"ops"}

    dispatcher = OutboxDispatcher(
        engine,
        consumers={"in_app_notification": NotificationMaterializer(engine)},
        now=lambda: fixed_now(),
    )
    for _ in range(2):
        claim = dispatcher.claim_one(owner="worker-1")
        assert claim is not None
        assert dispatcher.run_consumer_and_finalize(claim, owner="worker-1").status == "delivered"

    with engine.connect() as connection:
        notifications = connection.execute(select(notification_table)).mappings().all()
    assert {notification["title"] for notification in notifications} == {
        "Evaluation judge configuration missing"
    }
    assert all(
        "https://" not in str(notification["payload_json"]) for notification in notifications
    )
