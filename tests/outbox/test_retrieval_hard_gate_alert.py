"""Retrieval hard-gate breach alerts are outbox-only observability events."""

from __future__ import annotations

from sqlalchemy import select

from app.outbox.publisher import SqlAlchemyRetrievalHardGateAlertAdapter
from app.outbox.schema import outbox_event_table, outbox_recipient_table
from tests._support import build_engine, fixed_now, make_publisher


def test_hard_gate_breach_publishes_an_outbox_only_alert_event() -> None:
    engine = build_engine()
    adapter = SqlAlchemyRetrievalHardGateAlertAdapter(
        engine, make_publisher(engine, now=lambda: fixed_now())
    )

    first_event_id = adapter.publish_hard_gate_exceeded(
        space_ids=("space_2", "space_1", "space_2"),
        cap_tokens=24000,
        used_tokens=23990,
        excess_tokens=180,
        dropped_hit_count=2,
    )
    second_event_id = adapter.publish_hard_gate_exceeded(
        space_ids=("space_3",),
        cap_tokens=24000,
        used_tokens=24000,
        excess_tokens=64,
        dropped_hit_count=1,
    )

    assert first_event_id != second_event_id
    with engine.connect() as connection:
        events = connection.execute(select(outbox_event_table)).mappings().all()
        assert {event["event_id"] for event in events} == {first_event_id, second_event_id}
        assert {event["event_type"] for event in events} == {"retrieval_context_hard_gate_exceeded"}
        payloads = {event["event_id"]: event["payload_json"] for event in events}
        assert payloads[first_event_id]["space_ids"] == ["space_1", "space_2"]
        assert payloads[first_event_id]["cap_tokens"] == 24000
        assert payloads[first_event_id]["excess_tokens"] == 180
        assert payloads[first_event_id]["dropped_hit_count"] == 2
        # Outbox-only：不产生站内通知收件人。
        assert connection.execute(select(outbox_recipient_table)).first() is None
