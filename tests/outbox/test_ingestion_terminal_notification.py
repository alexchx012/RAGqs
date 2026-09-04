"""Ingestion 终态通知的 outbox 键冲突容忍合同：通知问题不回滚终态迁移。"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import select, update

from app.outbox.publisher import SqlAlchemyIngestionOutboxAdapter
from app.outbox.schema import outbox_event_table
from tests._support import (
    build_engine,
    build_identity_service,
    fixed_now,
    make_publisher,
    provision_user,
)


@pytest.fixture()
def ingestion_notifications():
    engine = build_engine()
    user_id = provision_user(build_identity_service(engine), username="creator")
    port = SqlAlchemyIngestionOutboxAdapter(make_publisher(engine))
    return engine, port, user_id


def _publish_terminal(port, connection, *, user_id, job_id="job_1", reason="boom"):
    return port.publish_ingestion_terminal_event(
        event_type="ingestion_failed",
        job_id=job_id,
        document_id="doc_1",
        document_version_id="docv_1",
        publication_id=None,
        transition_version=1,
        recipient_user_id=user_id,
        occurred_at=fixed_now(),
        reason=reason,
        connection=connection,
    )


def test_same_key_same_fingerprint_reuses_event(ingestion_notifications) -> None:
    engine, port, user_id = ingestion_notifications
    with engine.begin() as connection:
        first = _publish_terminal(port, connection, user_id=user_id)
        second = _publish_terminal(port, connection, user_id=user_id)
    assert first == second
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(outbox_event_table.c.event_id).where(
                    outbox_event_table.c.aggregate_id == "job_1"
                )
            )
            .scalars()
            .all()
        )
    assert rows == list(first)


def test_conflicting_fingerprint_skips_notification_and_logs(
    ingestion_notifications, caplog: pytest.LogCaptureFixture
) -> None:
    """同键异指纹（线上僵尸现场）：跳过通知并告警，不再抛 outbox_fingerprint_conflict。"""

    engine, port, user_id = ingestion_notifications
    with engine.begin() as connection:
        (event_id,) = _publish_terminal(port, connection, user_id=user_id)
        connection.execute(
            update(outbox_event_table)
            .where(outbox_event_table.c.event_id == event_id)
            .values(payload_fingerprint="tampered-legacy-row")
        )

    with caplog.at_level(logging.WARNING, logger="app.outbox.publisher"):
        with engine.begin() as connection:
            outcome = _publish_terminal(
                port, connection, user_id=user_id, reason="a-different-failure"
            )

    assert outcome == ()
    skipped = [r for r in caplog.records if "ingestion terminal notification skipped" in r.message]
    assert len(skipped) == 1
    message = skipped[0].getMessage()
    assert "job_id=job_1" in message
    assert "code=outbox_fingerprint_conflict" in message


def test_other_platform_errors_still_propagate(ingestion_notifications) -> None:
    """容忍范围仅限通知键冲突；其它 PlatformError 照常上抛。"""

    from app.platform.errors import PlatformError

    engine, port, user_id = ingestion_notifications
    with pytest.raises(PlatformError) as raised:
        with engine.begin() as connection:
            port.publish_ingestion_terminal_event(
                event_type="ingestion_completed",  # 通知端口不支持的事件族
                job_id="job_1",
                document_id="doc_1",
                document_version_id="docv_1",
                publication_id=None,
                transition_version=1,
                recipient_user_id=user_id,
                occurred_at=fixed_now(),
                reason=None,
                connection=connection,
            )
    assert raised.value.code == "invalid_event_payload"
