"""Business calendar: fixed-key singleton row + business-timezone month math.

The calendar is a single-row table (`business_calendar_version`, PK `id='instance'`)
whose row is created exactly once, concurrently safe via `INSERT ... ON CONFLICT
DO NOTHING`. Every fact timestamp in the ledger is stored as UTC; the business
timezone only participates in deriving the YYYY-MM period and the UTC instant of
the next month's start (reset_at), which is DST-aware.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, select
from sqlalchemy.engine import Connection

from app.platform.errors import PlatformError
from app.platform.persistence import DatabaseClock

from ._sql import _insert_do_nothing
from .schema import business_calendar_version_table

_INSTANCE_ID = "instance"


@dataclass(frozen=True, slots=True)
class CalendarLock:
    version_id: str
    timezone: str
    effective_from_utc: datetime


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _ledger_invariant(message: str) -> PlatformError:
    """Abnormal calendar row state is a ledger invariant violation, never a retry."""
    return PlatformError(
        "calendar_ledger_invariant",
        message,
        {},
        500,
        False,
    )


class BusinessCalendarService:
    def __init__(self, engine: Engine, clock: DatabaseClock, timezone: str) -> None:
        self._engine = engine
        self._clock = clock
        self._timezone = timezone
        ZoneInfo(timezone)

    def lock_or_verify(self, connection: Connection) -> CalendarLock:
        """Ensure the singleton row exists and return the locked calendar version.

        Concurrent callers race on the primary key: exactly one insert wins,
        the rest observe the existing row and verify their timezone against it.
        """
        existing = self._read_existing_lock(connection)
        if existing is not None:
            return existing

        now = self._clock.now_utc(connection)
        version_id = f"cal_{secrets.token_urlsafe(9)}"
        inserted = _insert_do_nothing(
            connection,
            business_calendar_version_table,
            {
                "id": _INSTANCE_ID,
                "version_id": version_id,
                "timezone": self._timezone,
                "effective_from_utc": now,
                "created_at_utc": now,
            },
            ["id"],
        )
        if inserted:
            return CalendarLock(version_id, self._timezone, now)

        lock = self._read_existing_lock(connection)
        if lock is not None:
            return lock
        raise _ledger_invariant("business calendar singleton row is missing after lock attempt")

    def _read_existing_lock(self, connection: Connection) -> CalendarLock | None:
        row = (
            connection.execute(
                select(
                    business_calendar_version_table.c.version_id,
                    business_calendar_version_table.c.timezone,
                    business_calendar_version_table.c.effective_from_utc,
                ).where(business_calendar_version_table.c.id == _INSTANCE_ID)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        effective_from = row["effective_from_utc"]
        if not isinstance(effective_from, datetime):
            raise _ledger_invariant("business calendar effective_from_utc is not a timestamp")
        if row["timezone"] != self._timezone:
            raise PlatformError(
                "calendar_timezone_conflict",
                "Business timezone no longer matches the locked calendar version",
                {"locked_timezone": row["timezone"]},
                503,
                True,
            )
        return CalendarLock(
            version_id=str(row["version_id"]),
            timezone=str(row["timezone"]),
            effective_from_utc=_utc(effective_from),
        )

    def _local_month_start(self, lock: CalendarLock, at_utc: datetime) -> datetime:
        tz = ZoneInfo(lock.timezone)
        local = _utc(at_utc).astimezone(tz)
        return datetime(local.year, local.month, 1, tzinfo=tz)

    def month_start_utc(self, lock: CalendarLock, at_utc: datetime) -> datetime:
        return self._local_month_start(lock, at_utc).astimezone(UTC)

    def next_month_start_utc(self, lock: CalendarLock, at_utc: datetime) -> datetime:
        start = self._local_month_start(lock, at_utc)
        if start.month == 12:
            nxt = datetime(start.year + 1, 1, 1, tzinfo=start.tzinfo)
        else:
            nxt = datetime(start.year, start.month + 1, 1, tzinfo=start.tzinfo)
        return nxt.astimezone(UTC)

    def period_for(self, lock: CalendarLock, at_utc: datetime) -> str:
        local = _utc(at_utc).astimezone(ZoneInfo(lock.timezone))
        return f"{local.year:04d}-{local.month:02d}"


def get_calendar_service(
    engine: Engine, clock: DatabaseClock, timezone: str
) -> BusinessCalendarService:
    """Create a runtime-scoped service with the exact injected engine and clock.

    Cross-runtime consistency is enforced by the fixed database row in ``lock_or_verify``;
    a process cache would retain engines and could silently reuse another runtime's clock.
    """
    return BusinessCalendarService(engine, clock, timezone)
