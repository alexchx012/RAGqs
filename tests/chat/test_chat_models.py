"""Unit tests for chat API value models."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from app.chat.models import datetime_to_rfc3339


def test_datetime_to_rfc3339_treats_naive_database_values_as_utc() -> None:
    naive = datetime(2026, 8, 13, 12, 0, 1, 987654)
    aware = datetime(2026, 8, 13, 20, 0, 1, 987654, tzinfo=timezone(timedelta(hours=8)))

    assert datetime_to_rfc3339(naive) == "2026-08-13T12:00:01Z"
    assert datetime_to_rfc3339(aware) == "2026-08-13T12:00:01Z"
    assert datetime_to_rfc3339(naive.replace(tzinfo=UTC)) == "2026-08-13T12:00:01Z"
