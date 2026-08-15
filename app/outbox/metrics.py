"""Outbox operational metrics: consumable counters and gauges.

Metrics are written into the outbox_metric table inside the same transaction
that changes delivery state, so a dead-lettered delivery always carries its
status, latency and oldest-pending signals atomically.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete
from sqlalchemy.engine import Connection

from .schema import outbox_metric_table


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class SqlAlchemyOutboxMetrics:
    """Persists bounded operational metrics in the caller's transaction."""

    def record(
        self,
        connection: Connection,
        *,
        metric_name: str,
        observed_at: datetime,
        event_id: str | None = None,
        value: float | None = None,
    ) -> None:
        normalized = _utc(observed_at)
        connection.execute(
            outbox_metric_table.insert().values(
                metric_name=metric_name,
                observed_at_utc=normalized,
                value=1.0 if value is None else value,
                event_id=event_id,
            )
        )

    def prune_before(self, connection: Connection, *, cutoff: datetime) -> int:
        result = connection.execute(
            delete(outbox_metric_table).where(outbox_metric_table.c.observed_at_utc < _utc(cutoff))
        )
        return int(result.rowcount or 0)


class NoopOutboxMetrics:
    """Explicit no-op for tests that do not assert on metric rows."""

    def record(
        self,
        connection: Connection,
        *,
        metric_name: str,
        observed_at: datetime,
        event_id: str | None = None,
        value: float | None = None,
    ) -> None:
        del connection, metric_name, observed_at, event_id, value

    def prune_before(self, connection: Connection, *, cutoff: datetime) -> int:
        del connection, cutoff
        return 0
