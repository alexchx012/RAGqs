"""Event-first persistence: immutable append log with monotonic sequence numbers."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Connection

from app.platform.errors import PlatformError

from .models import StoredEvent
from .schema import chat_generation_event_table, chat_generation_table


def append_event(
    connection: Connection,
    *,
    generation_id: str,
    event_type: str,
    data: Mapping[str, Any],
    now: datetime,
) -> int:
    """Append one event with the next sequence; callers must hold the generation lock."""

    current = connection.execute(
        select(func.coalesce(func.max(chat_generation_event_table.c.event_seq), 0)).where(
            chat_generation_event_table.c.generation_id == generation_id
        )
    ).scalar_one()
    event_seq = int(current) + 1
    connection.execute(
        chat_generation_event_table.insert().values(
            generation_id=generation_id,
            event_seq=event_seq,
            event_type=event_type,
            data_json=dict(data),
            created_at_utc=now,
        )
    )
    return event_seq


def has_terminal_event(connection: Connection, *, generation_id: str) -> bool:
    row = connection.execute(
        select(chat_generation_event_table.c.event_type).where(
            chat_generation_event_table.c.generation_id == generation_id,
            chat_generation_event_table.c.event_type.in_(["done", "error", "stopped"]),
        )
    ).first()
    return row is not None


def list_events_after(
    connection: Connection,
    *,
    generation_id: str,
    after_seq: int,
) -> list[StoredEvent]:
    rows = (
        connection.execute(
            select(
                chat_generation_event_table.c.event_seq,
                chat_generation_event_table.c.event_type,
                chat_generation_event_table.c.data_json,
            )
            .where(
                chat_generation_event_table.c.generation_id == generation_id,
                chat_generation_event_table.c.event_seq > after_seq,
            )
            .order_by(chat_generation_event_table.c.event_seq)
        )
        .mappings()
        .all()
    )
    return [
        StoredEvent(
            seq=int(row["event_seq"]),
            event_type=str(row["event_type"]),
            data=dict(row["data_json"]),
        )
        for row in rows
    ]


def lock_generation_row(connection: Connection, *, generation_id: str) -> dict[str, Any] | None:
    """Lock the generation row for a conditional state transition."""

    statement = (
        select(chat_generation_table)
        .where(chat_generation_table.c.id == generation_id)
        .with_for_update()
    )
    if connection.dialect.name == "sqlite":
        statement = select(chat_generation_table).where(chat_generation_table.c.id == generation_id)
    row = connection.execute(statement).mappings().one_or_none()
    if row is None:
        raise PlatformError("generation_not_found", "Generation was not found", {}, 404)
    return dict(row)


def require_generation_running_status(row: Mapping[str, Any], statuses: set[str]) -> None:
    if row["status"] not in statuses:
        raise PlatformError(
            "generation_state_conflict",
            "Generation is not in an expected state",
            {"status": row["status"]},
            409,
        )
