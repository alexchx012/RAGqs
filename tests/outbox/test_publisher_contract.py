"""Publisher contract: trusted caller, producer matrix, strict payload schemas."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.outbox.schema import (
    outbox_delivery_table,
    outbox_event_table,
    outbox_recipient_table,
)
from app.platform.errors import PlatformError
from tests._support import (
    build_engine,
    build_identity_service,
    fixed_now,
    make_publisher,
    provision_user,
)


def as_utc(value):
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class _AcceptingGraphReceipt:
    def verify_activated_receipt(
        self, *, aggregate_id: str, graph_generation_id: str, connection
    ) -> bool:
        del aggregate_id, graph_generation_id, connection
        return True


def make_command(**overrides):
    from app.outbox.ports import OutboxPublishCommand, RecipientSelection

    values = dict(
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
        trace_id="trace_x",
        recipients=(RecipientSelection(recipient_user_id="user_x"),),
    )
    values.update(overrides)
    return OutboxPublishCommand(**values)


def publish(engine, publisher, *, user_ids, **overrides):
    from app.outbox.ports import RecipientSelection

    recipients = tuple(RecipientSelection(recipient_user_id=u) for u in user_ids)
    return publisher.publish(
        make_command(recipients=recipients, **overrides),
        connection=engine.connect().__enter__(),
    )


def test_raw_publisher_has_no_generic_publish_entrypoint() -> None:
    from app.outbox.publisher import SqlAlchemyOutboxPublisher

    assert "publish" not in SqlAlchemyOutboxPublisher.__dict__


def test_publish_rejects_a_caller_without_producer_rights_for_the_event() -> None:
    engine = build_engine()
    publisher = make_publisher(
        engine,
        now=lambda: fixed_now(),
        graph_activated_receipt_port=_AcceptingGraphReceipt(),
    )

    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            publisher._publish_authorized(
                make_command(),
                connection=connection,
                caller="quota",
            )
    assert raised.value.status_code == 403
    assert raised.value.code == "producer_not_authorized"


def test_publish_rejects_a_wrong_aggregate_type_for_the_event() -> None:
    engine = build_engine()
    publisher = make_publisher(
        engine,
        now=lambda: fixed_now(),
        graph_activated_receipt_port=_AcceptingGraphReceipt(),
    )

    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            publisher.publish(
                make_command(aggregate_type="quota_request"),
                connection=connection,
            )
    assert raised.value.status_code == 422


def test_publish_rejects_extra_or_sensitive_payload_fields() -> None:
    engine = build_engine()
    publisher = make_publisher(
        engine,
        now=lambda: fixed_now(),
        graph_activated_receipt_port=_AcceptingGraphReceipt(),
    )

    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            publisher.publish(
                make_command(
                    payload={
                        "job_id": "job_1",
                        "document_id": "doc_1",
                        "document_version_id": "docv_1",
                        "publication_id": "pub_1",
                        "title": "secret filename",
                    }
                ),
                connection=connection,
            )
    assert raised.value.status_code == 422


def test_publish_rejects_nested_sensitive_payload() -> None:
    engine = build_engine()
    publisher = make_publisher(
        engine,
        now=lambda: fixed_now(),
        graph_activated_receipt_port=_AcceptingGraphReceipt(),
    )

    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            publisher.publish(
                make_command(
                    payload={
                        "job_id": "job_1",
                        "document_id": "doc_1",
                        "document_version_id": "docv_1",
                        "publication_id": "pub_1",
                        "extra": {"snippet": "secret text"},
                    }
                ),
                connection=connection,
            )
    assert raised.value.status_code == 422


def test_publish_rejects_wrong_payload_field_types() -> None:
    engine = build_engine()
    publisher = make_publisher(
        engine,
        now=lambda: fixed_now(),
        graph_activated_receipt_port=_AcceptingGraphReceipt(),
    )

    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            publisher.publish(
                make_command(
                    payload={
                        "job_id": "job_1",
                        "document_id": 12345,
                        "document_version_id": "docv_1",
                        "publication_id": "pub_1",
                    }
                ),
                connection=connection,
            )
    assert raised.value.status_code == 422


def test_graph_succeeded_requires_generation_and_failed_forbids_it() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(
        engine,
        now=lambda: fixed_now(),
        graph_activated_receipt_port=_AcceptingGraphReceipt(),
    )
    base = {
        "graph_build_id": "gb_1",
        "status": "succeeded",
        "source_revision": 1,
    }
    from app.outbox.ports import RecipientSelection as RS

    graph_recipients = (RS(recipient_user_id=alice),)

    with pytest.raises(PlatformError) as missing:
        with engine.begin() as connection:
            publisher.publish(
                make_command(
                    caller_principal="knowledge_graph",
                    event_type="graph_build_completed",
                    aggregate_type="graph_build_run",
                    payload={**base, "graph_generation_id": None},
                    recipients=graph_recipients,
                ),
                connection=connection,
            )
    assert missing.value.code == "invalid_event_payload"

    # failed must not carry graph_generation_id and requires a stable failure class.
    with pytest.raises(PlatformError) as forbidden:
        with engine.begin() as connection:
            publisher.publish(
                make_command(
                    caller_principal="knowledge_graph",
                    event_type="graph_build_completed",
                    aggregate_type="graph_build_run",
                    payload={
                        **base,
                        "status": "failed",
                        "graph_generation_id": "gen_1",
                        "failure_class": "staging_error",
                    },
                    recipients=graph_recipients,
                ),
                connection=connection,
            )
    assert forbidden.value.code == "invalid_event_payload"

    with pytest.raises(PlatformError) as missing_index:
        with engine.begin() as connection:
            publisher.publish(
                make_command(
                    caller_principal="knowledge_graph",
                    event_type="graph_build_completed",
                    aggregate_type="graph_build_run",
                    payload={**base, "graph_generation_id": "gen_1"},
                    recipients=graph_recipients,
                ),
                connection=connection,
            )
    assert missing_index.value.code == "invalid_event_payload"

    with engine.begin() as connection:
        publisher.publish(
            make_command(
                caller_principal="knowledge_graph",
                event_type="graph_build_completed",
                aggregate_type="graph_build_run",
                payload={
                    **base,
                    "graph_generation_id": "gen_1",
                    "index_generation_id": "index_gen_1",
                    "failure_class": None,
                },
                recipients=graph_recipients,
            ),
            connection=connection,
        )
    with engine.connect() as connection:
        payload = connection.execute(
            select(outbox_event_table.c.payload_json).where(
                outbox_event_table.c.event_id == "evt_1"
            )
        ).scalar_one()
    assert payload == {
        "schema_version": 1,
        "event_id": "evt_1",
        "event_type": "graph_build_completed",
        "aggregate_type": "graph_build_run",
        "aggregate_id": "job_1",
        "transition_version": 1,
        "graph_build_id": "gb_1",
        "status": "succeeded",
        "source_revision": 1,
        "graph_generation_id": "gen_1",
        "index_generation_id": "index_gen_1",
        "failure_class": None,
        "occurred_at": fixed_now().isoformat(),
    }


def test_graph_failed_and_cancelled_events_require_stable_failure_class() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(
        engine,
        now=lambda: fixed_now(),
        graph_activated_receipt_port=_AcceptingGraphReceipt(),
    )
    from app.outbox.ports import RecipientSelection as RS

    for status in ("failed", "cancelled"):
        with pytest.raises(PlatformError) as missing_failure:
            with engine.begin() as connection:
                publisher.publish(
                    make_command(
                        caller_principal="knowledge_graph",
                        event_type="graph_build_completed",
                        aggregate_type="graph_build_run",
                        payload={
                            "graph_build_id": f"gb_{status}",
                            "status": status,
                            "source_revision": 1,
                            "graph_generation_id": None,
                            "index_generation_id": None,
                            "failure_class": None,
                        },
                        recipients=(RS(recipient_user_id=alice),),
                    ),
                    connection=connection,
                )
        assert missing_failure.value.code == "invalid_event_payload"


def test_calibration_requires_active_ops_role_snapshot_recipients() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    ops = provision_user(identity, username="ops1", role="ops")
    publisher = make_publisher(
        engine,
        now=lambda: fixed_now(),
        graph_activated_receipt_port=_AcceptingGraphReceipt(),
    )

    # The publisher freezes ALL active ops inside the transaction regardless
    # of any caller-provided recipients.
    with engine.begin() as connection:
        receipt = publisher.publish(
            make_command(
                caller_principal="calibration",
                event_type="calibration_window_suggested",
                aggregate_type="calibration_window_suggestion",
                payload={"calibration_window_suggestion_id": "cws_1"},
            ),
            connection=connection,
        )
    assert receipt.reused is False
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(outbox_recipient_table).where(outbox_recipient_table.c.event_id == "evt_1")
            )
            .mappings()
            .all()
        )
        assert len(rows) == 1
        assert rows[0]["recipient_kind"] == "role_snapshot"
        assert rows[0]["required_role"] == "ops"
        assert rows[0]["role_snapshot"] == "ops"
        assert rows[0]["recipient_user_id"] == ops


def test_graph_requires_a_single_identity_recipient() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    bob = provision_user(identity, username="bob")
    publisher = make_publisher(
        engine,
        now=lambda: fixed_now(),
        graph_activated_receipt_port=_AcceptingGraphReceipt(),
    )

    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            publisher.publish(
                make_command(
                    caller_principal="knowledge_graph",
                    event_type="graph_build_completed",
                    aggregate_type="graph_build_run",
                    payload={
                        "graph_build_id": "gb_1",
                        "status": "succeeded",
                        "source_revision": 1,
                        "graph_generation_id": "gen_1",
                    },
                    recipients=(
                        __import__(
                            "app.outbox.ports", fromlist=["RecipientSelection"]
                        ).RecipientSelection(recipient_user_id=alice),
                        __import__(
                            "app.outbox.ports", fromlist=["RecipientSelection"]
                        ).RecipientSelection(recipient_user_id=bob),
                    ),
                ),
                connection=connection,
            )
    assert raised.value.status_code == 422


def test_publish_uses_database_now_for_created_and_selected_at() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    occurred_at = datetime(2020, 1, 1, tzinfo=UTC)
    publisher = make_publisher(
        engine,
        now=lambda: fixed_now(),
        graph_activated_receipt_port=_AcceptingGraphReceipt(),
    )

    from app.outbox.ports import RecipientSelection as RS

    with engine.begin() as connection:
        publisher.publish(
            make_command(occurred_at=occurred_at, recipients=(RS(recipient_user_id=alice),)),
            connection=connection,
        )

    with engine.connect() as connection:
        event = (
            connection.execute(
                select(outbox_event_table).where(outbox_event_table.c.event_id == "evt_1")
            )
            .mappings()
            .one()
        )
        recipient = (
            connection.execute(
                select(outbox_recipient_table).where(outbox_recipient_table.c.event_id == "evt_1")
            )
            .mappings()
            .one()
        )
        delivery = (
            connection.execute(
                select(outbox_delivery_table).where(outbox_delivery_table.c.event_id == "evt_1")
            )
            .mappings()
            .one()
        )
    # Business time stays exactly the occurred_at; created/selected/next_attempt
    # use the caller transaction's database time (>= business time).
    assert as_utc(event["occurred_at_utc"]) == occurred_at
    assert as_utc(event["created_at_utc"]) >= occurred_at
    assert event["created_at_utc"] != occurred_at
    assert as_utc(recipient["selected_at_utc"]) >= occurred_at
    assert recipient["selected_at_utc"] != occurred_at
    assert as_utc(delivery["next_attempt_at_utc"]) >= occurred_at
    assert delivery["next_attempt_at_utc"] != occurred_at


def test_idempotent_reuse_returns_the_existing_event_id() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(
        engine,
        now=lambda: fixed_now(),
        graph_activated_receipt_port=_AcceptingGraphReceipt(),
    )

    from app.outbox.ports import RecipientSelection as RS

    with engine.begin() as connection:
        first = publisher.publish(
            make_command(recipients=(RS(recipient_user_id=alice),)), connection=connection
        )
    with engine.begin() as connection:
        second = publisher.publish(
            make_command(
                event_id="evt_RETRY_DIFFERENT_ID",
                recipients=(RS(recipient_user_id=alice),),
            ),
            connection=connection,
        )

    assert first.reused is False
    assert second.reused is True
    # The reused receipt reports the real stored event id, never a made-up one.
    assert second.event_id == "evt_1"
    with engine.connect() as connection:
        assert len(connection.execute(select(outbox_event_table)).all()) == 1


def test_calibration_with_no_active_ops_keeps_the_event() -> None:
    engine = build_engine()
    publisher = make_publisher(
        engine,
        now=lambda: fixed_now(),
        graph_activated_receipt_port=_AcceptingGraphReceipt(),
    )

    with engine.begin() as connection:
        receipt = publisher.publish(
            make_command(
                caller_principal="calibration",
                event_type="calibration_window_suggested",
                aggregate_type="calibration_window_suggestion",
                payload={"calibration_window_suggestion_id": "cws_empty"},
            ),
            connection=connection,
        )
    assert receipt.reused is False
    with engine.connect() as connection:
        events = connection.execute(select(outbox_event_table)).all()
        recipients = connection.execute(select(outbox_recipient_table)).all()
        deliveries = connection.execute(select(outbox_delivery_table)).mappings().all()
    assert len(events) == 1
    assert len(recipients) == 0
    # 零接收者角色事件仍保留 pending delivery（C5-1）：dispatcher 完成该消费
    # 者并产生零接收者指标，业务事务不回滚。
    assert len(deliveries) == 1
    assert deliveries[0]["status"] == "pending"


def test_submission_payload_schemas_follow_the_contract() -> None:
    """§13 payload 契约：approved 携带 document_id/job_id，rejected reason 可选，
    invalidated 必带 reason，闭集外字段一律拒绝。"""
    from app.outbox.ports import RecipientSelection

    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(
        engine,
        now=lambda: fixed_now(),
        graph_activated_receipt_port=_AcceptingGraphReceipt(),
    )

    def submission_command(event_id, event_type, submission_id, payload):
        return make_command(
            event_id=event_id,
            caller_principal="submissions",
            event_type=event_type,
            aggregate_type="knowledge_submission",
            aggregate_id=submission_id,
            payload=payload,
            recipients=(RecipientSelection(recipient_user_id=alice),),
        )

    with engine.begin() as connection:
        publisher.publish(
            submission_command(
                "evt_sub_approved",
                "submission_approved",
                "sub_1",
                {"submission_id": "sub_1", "document_id": "doc_1", "job_id": "job_1"},
            ),
            connection=connection,
        )

    # approved 闭集外字段被拒绝（document_version_id 不在 schema 中）。
    with pytest.raises(PlatformError) as extra:
        with engine.begin() as connection:
            publisher.publish(
                submission_command(
                    "evt_sub_extra",
                    "submission_approved",
                    "sub_1",
                    {
                        "submission_id": "sub_1",
                        "document_id": "doc_1",
                        "job_id": "job_1",
                        "document_version_id": "docv_1",
                    },
                ),
                connection=connection,
            )
    assert extra.value.code == "invalid_event_payload"

    # approved 缺少 document_id/job_id 被拒绝。
    with pytest.raises(PlatformError) as missing:
        with engine.begin() as connection:
            publisher.publish(
                submission_command(
                    "evt_sub_missing",
                    "submission_approved",
                    "sub_2",
                    {"submission_id": "sub_2"},
                ),
                connection=connection,
            )
    assert missing.value.code == "invalid_event_payload"

    # rejected reason 可选；invalidated 必带 reason。
    with engine.begin() as connection:
        publisher.publish(
            submission_command(
                "evt_sub_rejected_plain",
                "submission_rejected",
                "sub_3",
                {"submission_id": "sub_3"},
            ),
            connection=connection,
        )
        publisher.publish(
            submission_command(
                "evt_sub_rejected_reason",
                "submission_rejected",
                "sub_4",
                {"submission_id": "sub_4", "reason": "reviewer_reason"},
            ),
            connection=connection,
        )
    with pytest.raises(PlatformError) as invalidated_missing:
        with engine.begin() as connection:
            publisher.publish(
                submission_command(
                    "evt_sub_invalidated_missing",
                    "submission_invalidated",
                    "sub_5",
                    {"submission_id": "sub_5"},
                ),
                connection=connection,
            )
    assert invalidated_missing.value.code == "invalid_event_payload"
    with engine.begin() as connection:
        publisher.publish(
            submission_command(
                "evt_sub_invalidated_reason",
                "submission_invalidated",
                "sub_6",
                {"submission_id": "sub_6", "reason": "identity_authorization_changed"},
            ),
            connection=connection,
        )
