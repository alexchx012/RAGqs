"""Subscription leases: recovery-stream liveness, renewal and disconnect grace."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from sqlalchemy import and_, select, update
from sqlalchemy.engine import Connection

from .schema import chat_generation_table, chat_subscription_lease_table

DEFAULT_SUBSCRIPTION_LEASE_SECONDS = 90
DEFAULT_HEARTBEAT_SECONDS = 30
DEFAULT_DISCONNECT_GRACE_SECONDS = 60


def create_lease(
    connection: Connection,
    *,
    generation_id: str,
    auth_session_id: str,
    now: datetime,
    lease_seconds: int = DEFAULT_SUBSCRIPTION_LEASE_SECONDS,
) -> dict[str, object]:
    lease = {
        "id": f"lease_{secrets.token_hex(12)}",
        "generation_id": generation_id,
        "auth_session_id": auth_session_id,
        "lease_token": secrets.token_hex(16),
        "expires_at_utc": now + timedelta(seconds=lease_seconds),
        "created_at_utc": now,
        "last_renewed_at_utc": now,
    }
    connection.execute(chat_subscription_lease_table.insert().values(**lease))
    connection.execute(
        update(chat_generation_table)
        .where(chat_generation_table.c.id == generation_id)
        .values(disconnect_deadline_at_utc=None, updated_at_utc=now)
    )
    return lease


def renew_lease(
    connection: Connection,
    *,
    lease_token: str,
    now: datetime,
    lease_seconds: int = DEFAULT_SUBSCRIPTION_LEASE_SECONDS,
) -> bool:
    """Renew a live lease; returns False when the lease was invalidated/revoked."""

    row = (
        connection.execute(
            select(
                chat_subscription_lease_table.c.id,
                chat_subscription_lease_table.c.generation_id,
            ).where(chat_subscription_lease_table.c.lease_token == lease_token)
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return False
    connection.execute(
        update(chat_subscription_lease_table)
        .where(chat_subscription_lease_table.c.lease_token == lease_token)
        .values(
            expires_at_utc=now + timedelta(seconds=lease_seconds),
            last_renewed_at_utc=now,
        )
    )
    return True


def invalidate_lease(
    connection: Connection,
    *,
    lease_token: str,
    now: datetime,
    grace_seconds: int = DEFAULT_DISCONNECT_GRACE_SECONDS,
) -> None:
    """Invalidate one lease; the last live lease arms the disconnect deadline."""

    row = (
        connection.execute(
            select(
                chat_subscription_lease_table.c.id,
                chat_subscription_lease_table.c.generation_id,
            ).where(chat_subscription_lease_table.c.lease_token == lease_token)
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return
    connection.execute(
        chat_subscription_lease_table.delete().where(
            chat_subscription_lease_table.c.lease_token == lease_token
        )
    )
    generation_id = str(row["generation_id"])
    active_count = (
        connection.execute(
            select(chat_subscription_lease_table.c.id).where(
                chat_subscription_lease_table.c.generation_id == generation_id
            )
        )
        .mappings()
        .first()
    )
    if active_count is None:
        connection.execute(
            update(chat_generation_table)
            .where(
                and_(
                    chat_generation_table.c.id == generation_id,
                    chat_generation_table.c.status.in_(["running", "stop_requested"]),
                )
            )
            .values(
                disconnect_deadline_at_utc=now + timedelta(seconds=grace_seconds),
                updated_at_utc=now,
            )
        )


def generation_has_active_lease(connection: Connection, *, generation_id: str) -> bool:
    row = connection.execute(
        select(chat_subscription_lease_table.c.id).where(
            chat_subscription_lease_table.c.generation_id == generation_id
        )
    ).first()
    return row is not None


def invalidate_all_generation_leases(
    connection: Connection, *, generation_id: str, now: datetime
) -> int:
    result = connection.execute(
        chat_subscription_lease_table.delete().where(
            chat_subscription_lease_table.c.generation_id == generation_id
        )
    )
    return int(result.rowcount or 0)
