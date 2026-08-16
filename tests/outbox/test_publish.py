"""Transactional publish port contract."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from _helpers import (
    build_engine,
    build_identity_service,
    create_publish_domain_tables,
    fixed_now,
    make_publisher,
    provision_user,
    publish_business_table,
)
from sqlalchemy import select

from app.outbox.ports import OutboxPublishCommand, RecipientSelection
from app.platform.errors import PlatformError


def make_publish_command(
    *,
    user_ids: tuple[str, ...],
    event_type: str = "ingestion_completed",
    aggregate_type: str = "ingestion_job",
    aggregate_id: str = "job_1",
    transition_version: int = 1,
    payload: dict | None = None,
    event_id: str = "evt_1",
) -> OutboxPublishCommand:
    return OutboxPublishCommand(
        event_id=event_id,
        caller_principal="ingestion",
        event_type=event_type,
        schema_version=1,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        transition_version=transition_version,
        occurred_at=fixed_now(),
        payload=payload
        or {
            "job_id": "job_1",
            "document_id": "doc_1",
            "document_version_id": "docv_1",
            "publication_id": "pub_1",
        },
        trace_id="trace_test",
        recipients=tuple(
            RecipientSelection(
                recipient_user_id=user_id,
                recipient_kind="identity",
                selection_reason="direct_operator",
            )
            for user_id in user_ids
        ),
    )


def test_publish_writes_event_recipients_and_initial_deliveries_atomically() -> None:
    engine = build_engine()
    create_publish_domain_tables(engine)
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())

    with engine.begin() as connection:
        connection.execute(publish_business_table.insert().values(id="doc_1", status="indexed"))
        receipt = publisher.publish(make_publish_command(user_ids=(alice,)), connection=connection)

    assert receipt.reused is False
    with engine.connect() as connection:
        business_state = connection.execute(
            select(publish_business_table).where(publish_business_table.c.id == "doc_1")
        ).one()
        assert business_state.status == "indexed"
        from app.outbox.schema import (
            outbox_delivery_table,
            outbox_event_table,
            outbox_recipient_table,
        )

        event = connection.execute(select(outbox_event_table)).mappings().one()
        assert event["event_id"] == "evt_1"
        assert event["storage_state"] == "full"
        recipient = connection.execute(select(outbox_recipient_table)).mappings().one()
        assert recipient["recipient_user_id"] == alice
        delivery = connection.execute(select(outbox_delivery_table)).mappings().one()
        assert delivery["consumer_name"] == "in_app_notification"
        assert delivery["status"] == "pending"
        assert delivery["version"] == 1
        assert delivery["replay_generation"] == 1
        assert delivery["attempt_number"] == 0
        assert delivery["fence_token"] is None


def test_publish_is_all_or_nothing_across_event_recipient_and_delivery() -> None:
    engine = build_engine()
    create_publish_domain_tables(engine)
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())

    with pytest.raises(PlatformError):
        with engine.begin() as connection:
            connection.execute(publish_business_table.insert().values(id="doc_x", status="indexed"))
            # Unknown recipient user forces a rollback of the whole transaction.
            publisher.publish(
                make_publish_command(user_ids=(alice, "user_missing"), event_id="evt_abort"),
                connection=connection,
            )

    with engine.connect() as connection:
        assert connection.execute(select(publish_business_table)).all() == []
        from app.outbox.schema import outbox_event_table

        assert connection.execute(select(outbox_event_table)).all() == []


def test_publish_reuses_existing_event_with_the_same_fingerprint() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())

    with engine.begin() as connection:
        first = publisher.publish(make_publish_command(user_ids=(alice,)), connection=connection)
    with engine.begin() as connection:
        second = publisher.publish(make_publish_command(user_ids=(alice,)), connection=connection)

    assert first.reused is False
    assert second.reused is True
    assert first.fingerprint == second.fingerprint
    with engine.connect() as connection:
        from app.outbox.schema import (
            outbox_delivery_table,
            outbox_event_table,
            outbox_recipient_table,
        )

        assert connection.execute(select(outbox_event_table)).all() == [
            connection.execute(select(outbox_event_table)).all()[0]
        ]
        assert len(connection.execute(select(outbox_recipient_table)).all()) == 1
        assert len(connection.execute(select(outbox_delivery_table)).all()) == 1


def test_publish_fingerprint_conflict_rolls_back_and_records_invariant_alert() -> None:
    engine = build_engine()
    create_publish_domain_tables(engine)
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())

    with engine.begin() as connection:
        publisher.publish(make_publish_command(user_ids=(alice,)), connection=connection)
    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            connection.execute(publish_business_table.insert().values(id="doc_2", status="indexed"))
            publisher.publish(
                make_publish_command(
                    user_ids=(alice,),
                    event_id="evt_conflict",
                    payload={
                        "job_id": "job_1",
                        "document_id": "doc_CHANGED",
                        "document_version_id": "docv_1",
                        "publication_id": "pub_1",
                    },
                ),
                connection=connection,
            )

    assert raised.value.code == "outbox_fingerprint_conflict"
    with engine.connect() as connection:
        assert connection.execute(select(publish_business_table)).all() == []
        from app.outbox.schema import outbox_event_table

        assert len(connection.execute(select(outbox_event_table)).all()) == 1


def test_publish_fingerprint_conflict_logs_without_a_second_engine_checkout(
    caplog, monkeypatch
) -> None:
    class _PostgresDialectConnection:
        def __init__(self, connection) -> None:
            self._connection = connection
            self.dialect = SimpleNamespace(name="postgresql")

        def __getattr__(self, name):
            return getattr(self._connection, name)

    class _CheckoutProbe:
        def __init__(self) -> None:
            self.calls = 0

        def begin(self):
            self.calls += 1
            raise AssertionError("fingerprint conflict must not checkout another connection")

    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())
    with engine.begin() as connection:
        publisher.publish(make_publish_command(user_ids=(alice,)), connection=connection)
    checkout_probe = _CheckoutProbe()
    monkeypatch.setattr(publisher, "_engine", checkout_probe)
    caplog.set_level(logging.ERROR, logger="app.outbox.publisher")

    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            publisher.publish(
                make_publish_command(
                    user_ids=(alice,),
                    event_id="evt_conflict",
                    payload={
                        "job_id": "job_1",
                        "document_id": "doc_CHANGED",
                        "document_version_id": "docv_1",
                        "publication_id": "pub_1",
                    },
                ),
                connection=_PostgresDialectConnection(connection),
            )

    assert raised.value.code == "outbox_fingerprint_conflict"
    assert checkout_probe.calls == 0
    assert any("outbox fingerprint conflict" in record.message for record in caplog.records)


def test_publish_captures_recipient_role_and_lifecycle_snapshot() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice", role="ops")
    publisher = make_publisher(engine, now=lambda: fixed_now())

    with engine.begin() as connection:
        publisher.publish(make_publish_command(user_ids=(alice,)), connection=connection)

    with engine.connect() as connection:
        from app.outbox.schema import outbox_recipient_table

        recipient = (
            connection.execute(
                select(outbox_recipient_table).where(
                    outbox_recipient_table.c.recipient_user_id == alice
                )
            )
            .mappings()
            .one()
        )
    assert recipient["role_snapshot"] == "ops"
    assert recipient["lifecycle_snapshot"] == "active"


def test_publish_rejects_unsupported_event_type_and_malformed_payload() -> None:
    engine = build_engine()
    identity = build_identity_service(engine)
    alice = provision_user(identity, username="alice")
    publisher = make_publisher(engine, now=lambda: fixed_now())

    # Unsupported event types are rejected before payload validation.
    with pytest.raises(PlatformError) as raised_type:
        with engine.begin() as connection:
            publisher.publish(
                make_publish_command(user_ids=(alice,), event_type="unknown_event"),
                connection=connection,
            )
    assert raised_type.value.status_code == 422
    assert raised_type.value.code == "unsupported_event_type"

    with pytest.raises(PlatformError) as raised_payload:
        with engine.begin() as connection:
            publisher.publish(
                make_publish_command(user_ids=(alice,), payload={"missing": "required"}),
                connection=connection,
            )
    assert raised_payload.value.status_code == 422


def test_publish_rejects_unknown_recipient_account() -> None:
    engine = build_engine()
    publisher = make_publisher(engine, now=lambda: fixed_now())

    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            publisher.publish(
                make_publish_command(user_ids=("user_missing",)),
                connection=connection,
            )
    assert raised.value.status_code == 422
