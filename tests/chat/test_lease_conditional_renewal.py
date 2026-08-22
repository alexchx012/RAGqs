"""fix-frontend-contract-misc：订阅租约 lease_token 条件续租。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine

from app.chat.leases import invalidate_lease, renew_lease
from app.chat.schema import chat_metadata, chat_subscription_lease_table

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _engine():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    chat_metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            chat_subscription_lease_table.insert().values(
                id="gen_1",
                generation_id="generation_1",
                auth_session_id="session_1",
                lease_token="token-live",
                expires_at_utc=NOW + timedelta(seconds=30),
                created_at_utc=NOW,
                last_renewed_at_utc=NOW,
            )
        )
        connection.execute(
            chat_subscription_lease_table.insert().values(
                id="gen_2",
                generation_id="generation_2",
                auth_session_id="session_2",
                lease_token="token-expired",
                expires_at_utc=NOW - timedelta(seconds=1),
                created_at_utc=NOW,
                last_renewed_at_utc=NOW,
            )
        )
    return engine


def test_live_lease_renews_by_token() -> None:
    engine = _engine()
    with engine.begin() as connection:
        assert renew_lease(connection, lease_token="token-live", now=NOW) is True


def test_expired_lease_cannot_be_resurrected() -> None:
    engine = _engine()
    with engine.begin() as connection:
        assert renew_lease(connection, lease_token="token-expired", now=NOW) is False


def test_invalidated_or_unknown_token_never_renews() -> None:
    engine = _engine()
    with engine.begin() as connection:
        invalidate_lease(connection, lease_token="token-live", now=NOW, grace_seconds=0)
        assert renew_lease(connection, lease_token="token-live", now=NOW) is False
        assert renew_lease(connection, lease_token="token-unknown", now=NOW) is False
