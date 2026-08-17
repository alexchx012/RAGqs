"""Outbox dispatcher: claiming, lease/fence, retry, dead-letter and replay.

The dispatcher runs per-consumer, per-event deliveries in short transactions.
Claims use the caller transaction (production: FOR UPDATE SKIP LOCKED);
completion, failure, renewal and materialization are fenced on (running, owner,
fence token) so a stale runner can never commit notification state.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import StaticPool

from app.platform.context import current_context
from app.platform.database import platform_audit_table
from app.platform.errors import PlatformError
from app.platform.persistence import FenceViolation

from .compaction import compact_event
from .notifications import DELETED_DOCUMENT_TITLE
from .ports import (
    V1_CONSUMER,
    DeliveryClaim,
    DeliveryMaterialization,
    DeliveryOutcome,
    OpsDeliveryView,
    OpsReplayReceipt,
)
from .schema import (
    outbox_delivery_attempt_table,
    outbox_delivery_table,
    outbox_document_tombstone_table,
    outbox_event_table,
    outbox_recipient_table,
    outbox_replay_idempotency_table,
)

MAX_CYCLE_ATTEMPTS = 8
LEASE_SECONDS = 60
RETRY_DELAYS_SECONDS = (5, 30, 120, 600, 1800, 7200, 21600)

_PENDING_STATUSES = ("pending", "retry_wait")

_logger = logging.getLogger(__name__)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _new_attempt_id() -> str:
    return f"attempt_{secrets.token_urlsafe(18)}"


def _new_notification_id() -> str:
    return f"notification_{secrets.token_urlsafe(18)}"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    delays: tuple[int, ...] = RETRY_DELAYS_SECONDS
    max_attempts: int = MAX_CYCLE_ATTEMPTS

    def delay_for_cycle_attempt(self, cycle_attempt_number: int) -> int | None:
        """Return the delay before the next retry, or None to dead-letter."""
        if cycle_attempt_number >= self.max_attempts:
            return None
        return self.delays[cycle_attempt_number - 1]


class DeliveryConsumer(Protocol):
    """One materializer for one consumer name."""

    def materialize(
        self,
        connection: Connection,
        *,
        event: dict[str, object],
        recipient: dict[str, object],
        notification_id: str,
        notification_type: str,
        title: str,
        document_id: str | None,
        document_version_id: str | None,
        redacted: bool,
        now: datetime,
    ) -> DeliveryMaterialization | None: ...


class OutboxDispatcher:
    """Lease-and-fence driven dispatcher for one or more consumers."""

    def __init__(
        self,
        engine: Engine,
        *,
        consumers: dict[str, DeliveryConsumer],
        now: Callable[[], datetime],
        retention_days: int = 30,
        notification_retention_days: int = 90,
        metrics: Any = None,
        lease_seconds: int = LEASE_SECONDS,
        clock: Any = None,
    ) -> None:
        self._engine = engine
        self._consumers = consumers
        self._now = now
        self._clock = clock
        self._retention_days = retention_days
        self._notification_retention_days = notification_retention_days
        self._metrics = metrics
        self._lease_seconds = lease_seconds

    @property
    def heartbeat_requires_exclusive_connection(self) -> bool:
        """Whether heartbeat and finalization would share one DBAPI connection."""
        return isinstance(self._engine.pool, StaticPool)

    def _current_time(self, connection: Connection) -> datetime:
        """Database time on the caller's transaction connection when available."""
        if self._clock is not None:
            value = self._clock.now_utc(connection)
            return value if isinstance(value, datetime) else _utc(self._now())
        return _utc(self._now())

    def _record_metric(
        self,
        connection: Connection,
        metric_name: str,
        *,
        event_id: str | None = None,
        value: float | None = None,
    ) -> None:
        if self._metrics is None:
            return
        self._metrics.record(
            connection,
            metric_name=metric_name,
            observed_at=self._current_time(connection),
            event_id=event_id,
            value=value,
        )

    def _record_status_metric(
        self,
        connection: Connection,
        status: str,
        *,
        event_id: str | None = None,
    ) -> None:
        self._record_metric(
            connection,
            f"outbox.deliveries.status.{status}",
            event_id=event_id,
            value=1.0,
        )

    def _alert_dead_letter(
        self,
        connection: Connection,
        *,
        event_id: str,
        consumer_name: str,
        error_code: str,
        now: datetime,
    ) -> None:
        """Immediately emit a consumable dead-letter alert (audit row + log)."""
        context = current_context()
        connection.execute(
            platform_audit_table.insert().values(
                actor_id="system:outbox",
                resource_type="outbox_delivery",
                resource_id=event_id,
                request_id=context.request_id if context is not None else "req_outbox",
                occurred_at_utc=now,
                result="dead_lettered",
                details_json={"consumer_name": consumer_name, "error_code": error_code},
            )
        )
        _logger.error(
            "outbox delivery dead-lettered event_id=%s consumer=%s error_code=%s",
            event_id,
            consumer_name,
            error_code,
        )

    def claim_one(self, owner: str, *, limit: int = 1) -> DeliveryClaim | None:
        """Claim at most one due delivery with a fresh attempt and fence."""
        normalized_owner = owner.strip()
        if not normalized_owner:
            raise ValueError("dispatcher owner must not be empty")
        with self._engine.begin() as connection:
            now = self._current_time(connection)
            due = self._claim_candidates(connection, now=now, limit=limit)
            if not due:
                return None
            row = dict(due[0])
            event_id = str(row["event_id"])
            consumer_name = str(row["consumer_name"])
            next_attempt = int(row["attempt_number"]) + 1
            cycle_attempt = self._cycle_attempt_after(row)
            version = int(row["version"]) + 1
            fence_token = int(row["fence_token"] or 0) + 1
            lease_expires_at = now + timedelta(seconds=self._lease_seconds)
            attempt_id = _new_attempt_id()
            claimed = connection.execute(
                update(outbox_delivery_table)
                .where(
                    outbox_delivery_table.c.event_id == event_id,
                    outbox_delivery_table.c.consumer_name == consumer_name,
                    outbox_delivery_table.c.status.in_(_PENDING_STATUSES),
                    outbox_delivery_table.c.version == row["version"],
                )
                .values(
                    status="running",
                    version=version,
                    attempt_number=next_attempt,
                    cycle_attempt_number=cycle_attempt,
                    error_category=None,
                    error_code=None,
                    next_attempt_at_utc=None,
                    lease_owner=normalized_owner,
                    lease_expires_at_utc=lease_expires_at,
                    fence_token=fence_token,
                )
            ).rowcount
            if claimed != 1:
                # Lost the race: another runner claimed it first.
                return None
            connection.execute(
                outbox_delivery_attempt_table.insert().values(
                    delivery_attempt_id=attempt_id,
                    event_id=event_id,
                    consumer_name=consumer_name,
                    replay_generation=int(row["replay_generation"]),
                    attempt_number=next_attempt,
                    cycle_attempt_number=cycle_attempt,
                    fence_token=fence_token,
                    started_at_utc=now,
                    ended_at_utc=None,
                    status="running",
                    error_category=None,
                    error_code=None,
                )
            )
            self._record_metric(connection, "outbox.attempts.started", event_id=event_id)
            payload = None
            notification_type = None
            title = None
            document_id = None
            document_version_id = None
            redacted_title = DELETED_DOCUMENT_TITLE
            if consumer_name == V1_CONSUMER:
                event = (
                    connection.execute(
                        select(outbox_event_table).where(outbox_event_table.c.event_id == event_id)
                    )
                    .mappings()
                    .one()
                )
                payload = dict(event["payload_json"] or {})
                event_type = str(event["event_type"])
                notification_type = event_type
                title = _default_title(event_type)
                document_id = _document_id_for(event_type, payload)
                document_version_id = _document_version_id_for(event_type, payload)
                occurred_at = event.get("occurred_at_utc")
                if isinstance(occurred_at, datetime):
                    self._record_metric(
                        connection,
                        "outbox.deliveries.oldest_pending_seconds",
                        event_id=event_id,
                        value=max(0.0, (now - _utc(occurred_at)).total_seconds()),
                    )
            return DeliveryClaim(
                event_id=event_id,
                consumer_name=consumer_name,
                version=version,
                replay_generation=int(row["replay_generation"]),
                attempt_number=next_attempt,
                cycle_attempt_number=cycle_attempt,
                fence_token=fence_token,
                lease_expires_at=lease_expires_at,
                started_at=now,
                attempt_id=attempt_id,
                payload=payload,
                notification_type=notification_type,
                title=title,
                redacted_title=redacted_title,
                document_id=document_id,
                document_version_id=document_version_id,
            )

    def _claim_candidates(
        self,
        connection: Connection,
        *,
        now: datetime,
        limit: int,
    ) -> list[Any]:
        claimable = select(outbox_delivery_table).where(
            or_(
                and_(
                    outbox_delivery_table.c.status == "pending",
                    outbox_delivery_table.c.next_attempt_at_utc.is_(None),
                ),
                and_(
                    outbox_delivery_table.c.status == "pending",
                    outbox_delivery_table.c.next_attempt_at_utc <= now,
                ),
                and_(
                    outbox_delivery_table.c.status == "retry_wait",
                    outbox_delivery_table.c.next_attempt_at_utc <= now,
                ),
            )
        )
        if connection.dialect.name == "postgresql":
            claimable = claimable.with_for_update(skip_locked=True)
        return list(connection.execute(claimable.limit(limit)).mappings().all())

    @staticmethod
    def _cycle_attempt_after(row: dict[str, Any]) -> int:
        """The cycle attempt is per replay_generation, never the global attempt.

        retry_wait rows keep their stored cycle attempt and advance it; any
        pending row (first attempt or after a replay) starts a fresh cycle at 1.
        """
        stored = int(row.get("cycle_attempt_number") or 0)
        if str(row["status"]) == "retry_wait":
            return stored + 1
        return 1

    def run_consumer_and_finalize(
        self,
        claim: DeliveryClaim,
        *,
        owner: str,
    ) -> DeliveryOutcome:
        """Materialize all recipients and commit the delivered state fenced."""
        consumer = self._consumers.get(claim.consumer_name)
        if consumer is None:
            return self.fail_and_schedule(
                claim,
                owner=owner,
                error_category="permanent",
                error_code="unsupported_consumer",
            )
        try:
            with self._engine.begin() as connection:
                self._assert_fence(connection, claim, owner=owner)
                now = self._current_time(connection)
                delivered_at = now
                recipients = self._recipients_for(connection, claim.event_id)
                if claim.consumer_name == V1_CONSUMER:
                    # event 与 tombstone 只依赖 claim，对同一 event 的全部
                    # 收件人只读取一次；per-document redaction 锁同样只取一次。
                    if claim.document_id is not None and connection.dialect.name == "postgresql":
                        connection.execute(
                            text("SELECT pg_advisory_xact_lock(hashtext(:lock))"),
                            {"lock": f"ragqs:documents:redact:{claim.document_id}"},
                        )
                    event = (
                        connection.execute(
                            select(outbox_event_table).where(
                                outbox_event_table.c.event_id == claim.event_id
                            )
                        )
                        .mappings()
                        .one()
                    )
                    redacted = self._event_redacted(connection, claim)
                    title = (
                        claim.redacted_title
                        if redacted
                        else (claim.title if claim.title is not None else claim.redacted_title)
                    )
                    for recipient in recipients:
                        consumer.materialize(
                            connection,
                            event=dict(event),
                            recipient=recipient,
                            notification_id=_new_notification_id(),
                            notification_type=claim.notification_type or "unknown",
                            title=title,
                            document_id=claim.document_id,
                            document_version_id=claim.document_version_id,
                            redacted=redacted,
                            now=now,
                        )
                delivered = connection.execute(
                    update(outbox_delivery_table)
                    .where(
                        outbox_delivery_table.c.event_id == claim.event_id,
                        outbox_delivery_table.c.consumer_name == claim.consumer_name,
                        outbox_delivery_table.c.status == "running",
                        outbox_delivery_table.c.lease_owner == owner,
                        outbox_delivery_table.c.fence_token == claim.fence_token,
                        outbox_delivery_table.c.lease_expires_at_utc > now,
                    )
                    .values(
                        status="delivered",
                        version=claim.version,
                        error_category=None,
                        error_code=None,
                        next_attempt_at_utc=None,
                        lease_owner=None,
                        lease_expires_at_utc=None,
                        fence_token=outbox_delivery_table.c.fence_token,
                        delivered_at_utc=delivered_at,
                    )
                ).rowcount
                if delivered != 1:
                    raise FenceViolation(f"stale fence for {claim.event_id}")
                attempt_updated = connection.execute(
                    update(outbox_delivery_attempt_table)
                    .where(
                        outbox_delivery_attempt_table.c.delivery_attempt_id == claim.attempt_id,
                        outbox_delivery_attempt_table.c.status == "running",
                        outbox_delivery_attempt_table.c.fence_token == claim.fence_token,
                    )
                    .values(status="delivered", ended_at_utc=delivered_at)
                ).rowcount
                if attempt_updated != 1:
                    raise FenceViolation(f"stale attempt for {claim.event_id}")
                self._record_metric(
                    connection, "outbox.deliveries.delivered", event_id=claim.event_id
                )
                self._record_status_metric(connection, "delivered", event_id=claim.event_id)
                self._record_metric(
                    connection,
                    "outbox.deliveries.latency_ms",
                    event_id=claim.event_id,
                    value=max(0.0, (delivered_at - _utc(claim.started_at)).total_seconds() * 1000),
                )
                self._maybe_freeze_compaction(connection, claim.event_id, now)
            return DeliveryOutcome(status="delivered")
        except FenceViolation:
            raise
        except (PlatformError, ValueError, SQLAlchemyError) as exc:
            # Unified permanent/retryable boundary: contract violations dead
            # letter; anything else schedules a retry. FenceViolation is never
            # swallowed.
            return self._handle_materialization_error(claim, owner=owner, exc=exc)
        except Exception as exc:
            # Any unexpected consumer exception is retryable internal_error;
            # FenceViolation above is the only exception that aborts.
            return self._handle_materialization_error(claim, owner=owner, exc=exc)

    @staticmethod
    def _event_redacted(connection: Connection, claim: DeliveryClaim) -> bool:
        """A permanent (document, version) tombstone marks the projection deleted.

        The tombstone survives compaction and replay, so later materialization
        can never restore the original filename/title/snippet; it renders the
        fixed deleted-document text and keeps only opaque identifiers. The
        document_version_id must match EXACTLY.
        """
        if claim.document_id is None or claim.document_version_id is None:
            return False
        matched = connection.execute(
            select(outbox_document_tombstone_table.c.document_id).where(
                outbox_document_tombstone_table.c.document_id == claim.document_id,
                outbox_document_tombstone_table.c.document_version_id == claim.document_version_id,
            )
        ).scalar_one_or_none()
        return matched is not None

    def _recipients_for(self, connection: Connection, event_id: str) -> list[dict[str, object]]:
        rows = (
            connection.execute(
                select(outbox_recipient_table).where(outbox_recipient_table.c.event_id == event_id)
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]

    def fail_and_schedule(
        self,
        claim: DeliveryClaim,
        *,
        owner: str,
        error_category: str,
        error_code: str,
    ) -> DeliveryOutcome:
        """Record a failed attempt and schedule the next retry or dead-letter."""
        if error_category not in {"retryable", "permanent", "lease_expired"}:
            raise ValueError(f"unknown error category: {error_category}")
        with self._engine.begin() as connection:
            self._assert_fence(connection, claim, owner=owner)
            now = self._current_time(connection)
            policy = RetryPolicy()
            delay = policy.delay_for_cycle_attempt(claim.cycle_attempt_number)
            permanent = error_category == "permanent"
            if permanent or delay is None:
                next_status = "dead_letter"
                next_at = None
                category = "permanent" if permanent else "retryable"
            else:
                next_status = "retry_wait"
                next_at = now + _jittered_delay(delay)
                category = error_category
            updated = connection.execute(
                update(outbox_delivery_table)
                .where(
                    outbox_delivery_table.c.event_id == claim.event_id,
                    outbox_delivery_table.c.consumer_name == claim.consumer_name,
                    outbox_delivery_table.c.status == "running",
                    outbox_delivery_table.c.lease_owner == owner,
                    outbox_delivery_table.c.fence_token == claim.fence_token,
                    outbox_delivery_table.c.lease_expires_at_utc > now,
                )
                .values(
                    status=next_status,
                    version=claim.version,
                    cycle_attempt_number=claim.cycle_attempt_number,
                    error_category=category,
                    error_code=error_code,
                    next_attempt_at_utc=next_at,
                    lease_owner=None,
                    lease_expires_at_utc=None,
                    fence_token=outbox_delivery_table.c.fence_token,
                )
            ).rowcount
            if updated != 1:
                raise FenceViolation(f"stale fence for {claim.event_id}")
            connection.execute(
                update(outbox_delivery_attempt_table)
                .where(outbox_delivery_attempt_table.c.delivery_attempt_id == claim.attempt_id)
                .values(
                    status="failed",
                    ended_at_utc=now,
                    error_category=category,
                    error_code=error_code,
                )
            )
            if next_status == "dead_letter":
                self._alert_dead_letter(
                    connection,
                    event_id=claim.event_id,
                    consumer_name=claim.consumer_name,
                    error_code=error_code,
                    now=now,
                )
                self._record_metric(
                    connection, "outbox.deliveries.dead_letter", event_id=claim.event_id
                )
                self._record_status_metric(connection, "dead_letter", event_id=claim.event_id)
            else:
                self._record_metric(
                    connection, "outbox.deliveries.retry_wait", event_id=claim.event_id
                )
                self._record_metric(connection, "outbox.deliveries.retry", event_id=claim.event_id)
                self._record_status_metric(connection, "retry_wait", event_id=claim.event_id)
            return DeliveryOutcome(
                status="dead_letter" if next_status == "dead_letter" else "failed",
                error_category=category,  # type: ignore[arg-type]
                error_code=error_code,
                retry_after_seconds=delay,
            )

    def _handle_materialization_error(
        self,
        claim: DeliveryClaim,
        *,
        owner: str,
        exc: BaseException,
    ) -> DeliveryOutcome:
        """Permanent payload/contract errors dead-letter; everything else retries."""
        if _is_permanent_materialization_error(exc):
            return self.fail_and_schedule(
                claim,
                owner=owner,
                error_category="permanent",
                error_code=_permanent_error_code(exc),
            )
        return self.fail_and_schedule(
            claim,
            owner=owner,
            error_category="retryable",
            error_code="internal_error",
        )

    def recycle_expired_running(self, *, limit: int = 100) -> int:
        """Reclaim expired running attempts as expired, consuming an automatic attempt."""
        recycled = 0
        with self._engine.begin() as connection:
            now = self._current_time(connection)
            expired = (
                connection.execute(
                    select(
                        outbox_delivery_table.c.event_id,
                        outbox_delivery_table.c.consumer_name,
                        outbox_delivery_table.c.attempt_number,
                        outbox_delivery_table.c.replay_generation,
                        outbox_delivery_table.c.cycle_attempt_number,
                        outbox_delivery_table.c.lease_owner,
                        outbox_delivery_table.c.fence_token,
                    ).where(
                        and_(
                            outbox_delivery_table.c.status == "running",
                            outbox_delivery_table.c.lease_expires_at_utc <= now,
                        )
                    )
                    .order_by(
                        outbox_delivery_table.c.lease_expires_at_utc,
                        outbox_delivery_table.c.event_id,
                        outbox_delivery_table.c.consumer_name,
                    )
                    .limit(limit)
                )
                .mappings()
                .all()
            )
            for row in expired:
                event_id = str(row["event_id"])
                consumer_name = str(row["consumer_name"])
                cycle_attempt = int(row["cycle_attempt_number"])
                policy = RetryPolicy()
                delay = policy.delay_for_cycle_attempt(cycle_attempt)
                if delay is None:
                    next_status = "dead_letter"
                    next_at = None
                else:
                    next_status = "retry_wait"
                    next_at = now + _jittered_delay(delay)
                updated = connection.execute(
                    update(outbox_delivery_table)
                    .where(
                        outbox_delivery_table.c.event_id == event_id,
                        outbox_delivery_table.c.consumer_name == consumer_name,
                        outbox_delivery_table.c.status == "running",
                        outbox_delivery_table.c.lease_owner == row["lease_owner"],
                        outbox_delivery_table.c.fence_token == row["fence_token"],
                    )
                    .values(
                        status=next_status,
                        cycle_attempt_number=cycle_attempt,
                        error_category="lease_expired",
                        error_code="lease_expired",
                        next_attempt_at_utc=next_at,
                        lease_owner=None,
                        lease_expires_at_utc=None,
                        fence_token=outbox_delivery_table.c.fence_token,
                    )
                ).rowcount
                if updated != 1:
                    continue
                attempts = (
                    connection.execute(
                        select(outbox_delivery_attempt_table.c.delivery_attempt_id).where(
                            outbox_delivery_attempt_table.c.event_id == event_id,
                            outbox_delivery_attempt_table.c.consumer_name == consumer_name,
                            outbox_delivery_attempt_table.c.fence_token == row["fence_token"],
                            outbox_delivery_attempt_table.c.status == "running",
                        )
                    )
                    .scalars()
                    .all()
                )
                for attempt_id in attempts:
                    connection.execute(
                        update(outbox_delivery_attempt_table)
                        .where(outbox_delivery_attempt_table.c.delivery_attempt_id == attempt_id)
                        .values(
                            status="expired",
                            ended_at_utc=now,
                            error_category="lease_expired",
                            error_code="lease_expired",
                        )
                    )
                if next_status == "dead_letter":
                    self._alert_dead_letter(
                        connection,
                        event_id=event_id,
                        consumer_name=consumer_name,
                        error_code="lease_expired",
                        now=now,
                    )
                self._record_metric(
                    connection, "outbox.deliveries.lease_expired", event_id=event_id
                )
                self._record_status_metric(connection, next_status, event_id=event_id)
                recycled += 1
        return recycled

    def renew_lease(self, claim: DeliveryClaim, *, owner: str) -> DeliveryClaim | None:
        """Renew a running delivery lease using database time (called every 20s).

        A lease that already expired cannot be renewed: the update requires
        `lease_expires_at_utc > now`, so a stale runner stops immediately.
        """
        with self._engine.begin() as connection:
            now = self._current_time(connection)
            expires_at = now + timedelta(seconds=self._lease_seconds)
            updated = connection.execute(
                update(outbox_delivery_table)
                .where(
                    outbox_delivery_table.c.event_id == claim.event_id,
                    outbox_delivery_table.c.consumer_name == claim.consumer_name,
                    outbox_delivery_table.c.status == "running",
                    outbox_delivery_table.c.lease_owner == owner,
                    outbox_delivery_table.c.fence_token == claim.fence_token,
                    outbox_delivery_table.c.lease_expires_at_utc > now,
                )
                .values(lease_expires_at_utc=expires_at)
            ).rowcount
            if updated != 1:
                return None
            return DeliveryClaim(
                event_id=claim.event_id,
                consumer_name=claim.consumer_name,
                version=claim.version,
                replay_generation=claim.replay_generation,
                attempt_number=claim.attempt_number,
                cycle_attempt_number=claim.cycle_attempt_number,
                fence_token=claim.fence_token,
                lease_expires_at=expires_at,
                started_at=claim.started_at,
                attempt_id=claim.attempt_id,
                payload=claim.payload,
                notification_type=claim.notification_type,
                title=claim.title,
                redacted_title=claim.redacted_title,
                document_id=claim.document_id,
                document_version_id=claim.document_version_id,
            )

    def _assert_fence(
        self,
        connection: Connection,
        claim: DeliveryClaim,
        *,
        owner: str,
    ) -> None:
        active = connection.execute(
            select(outbox_delivery_table.c.event_id).where(
                outbox_delivery_table.c.event_id == claim.event_id,
                outbox_delivery_table.c.consumer_name == claim.consumer_name,
                outbox_delivery_table.c.status == "running",
                outbox_delivery_table.c.lease_owner == owner,
                outbox_delivery_table.c.fence_token == claim.fence_token,
            )
        ).scalar_one_or_none()
        if active is None:
            raise FenceViolation(f"stale fence for {claim.event_id}")
        # The lease must also be unexpired at database time.
        db_now = self._current_time(connection)
        if claim.lease_expires_at <= db_now:
            raise FenceViolation(f"expired lease for {claim.event_id}")

    def _maybe_freeze_compaction(
        self,
        connection: Connection,
        event_id: str,
        now: datetime,
    ) -> None:
        """Freeze compact_after_at when every delivery first reached delivered."""
        remaining = connection.execute(
            select(func.count())
            .select_from(outbox_delivery_table)
            .where(
                outbox_delivery_table.c.event_id == event_id,
                outbox_delivery_table.c.status != "delivered",
            )
        ).scalar_one()
        if int(remaining) != 0:
            return
        latest = connection.execute(
            select(func.max(outbox_delivery_table.c.delivered_at_utc)).where(
                outbox_delivery_table.c.event_id == event_id
            )
        ).scalar_one()
        compact_after = _utc(latest) + timedelta(days=self._retention_days) if latest else None
        if compact_after is None:
            return
        connection.execute(
            update(outbox_event_table)
            .where(
                outbox_event_table.c.event_id == event_id,
                outbox_event_table.c.storage_state == "full",
                outbox_event_table.c.compact_after_at_utc.is_(None),
            )
            .values(compact_after_at_utc=compact_after)
        )

    # ------------------------------------------------------------------
    # Ops views and replay
    # ------------------------------------------------------------------

    def ops_view(self, event_id: str, *, consumer_name: str) -> OpsDeliveryView | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(
                        outbox_delivery_table.c.event_id,
                        outbox_delivery_table.c.consumer_name,
                        outbox_delivery_table.c.status,
                        outbox_delivery_table.c.version,
                        outbox_delivery_table.c.replay_generation,
                        outbox_delivery_table.c.attempt_number,
                        outbox_delivery_table.c.error_category,
                        outbox_delivery_table.c.error_code,
                        outbox_delivery_table.c.next_attempt_at_utc,
                        outbox_delivery_table.c.lease_expires_at_utc,
                        outbox_event_table.c.storage_state,
                    )
                    .join(
                        outbox_event_table,
                        outbox_event_table.c.event_id == outbox_delivery_table.c.event_id,
                    )
                    .where(
                        outbox_delivery_table.c.event_id == event_id,
                        outbox_delivery_table.c.consumer_name == consumer_name,
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        return OpsDeliveryView(
            event_id=event_id,
            consumer_name=consumer_name,
            status=str(row["status"]),
            version=int(row["version"]),
            replay_generation=int(row["replay_generation"]),
            attempt_number=int(row["attempt_number"]),
            error_category=row["error_category"],
            error_code=row["error_code"],
            replayable=bool(row["storage_state"] == "full" and row["status"] == "dead_letter"),
            next_attempt_at=row["next_attempt_at_utc"],
            lease_expires_at=row["lease_expires_at_utc"],
        )

    def replay(
        self,
        event_id: str,
        *,
        consumer_name: str,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
    ) -> OpsReplayReceipt:
        """Replay a dead-lettered full event into a fresh pending cycle."""
        with self._engine.begin() as connection:
            now = self._current_time(connection)
            event = (
                connection.execute(
                    select(
                        outbox_event_table.c.event_id,
                        outbox_event_table.c.storage_state,
                    ).where(outbox_event_table.c.event_id == event_id)
                )
                .mappings()
                .one_or_none()
            )
            if event is None:
                raise PlatformError("not_found", "Event was not found", {}, 404)
            if event["storage_state"] != "full":
                raise PlatformError(
                    "outbox_delivery_not_replayable",
                    "Compacted events cannot be replayed",
                    {},
                    409,
                )
            delivery = (
                connection.execute(
                    select(outbox_delivery_table).where(
                        outbox_delivery_table.c.event_id == event_id,
                        outbox_delivery_table.c.consumer_name == consumer_name,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if delivery is None:
                raise PlatformError(
                    "outbox_delivery_not_replayable",
                    "Delivery was not found",
                    {},
                    409,
                )
            # Reserve the idempotency key *before* any state check so two
            # concurrent replays with the same key serialize: the loser reads
            # the winner's completed reservation and returns the original
            # receipt instead of failing a stale CAS.
            reserved = False
            try:
                if connection.dialect.name == "postgresql":
                    from sqlalchemy.dialects.postgresql import insert as postgresql_insert

                    inserted = connection.execute(
                        postgresql_insert(outbox_replay_idempotency_table)
                        .values(
                            event_id=event_id,
                            consumer_name=consumer_name,
                            idempotency_key=idempotency_key,
                            request_hash=request_hash,
                            completed=False,
                            response_json=None,
                            created_at_utc=now,
                        )
                        .on_conflict_do_nothing(
                            index_elements=[
                                "event_id",
                                "consumer_name",
                                "idempotency_key",
                            ]
                        )
                    ).rowcount
                    reserved = inserted == 1
                else:
                    connection.execute(
                        outbox_replay_idempotency_table.insert().values(
                            event_id=event_id,
                            consumer_name=consumer_name,
                            idempotency_key=idempotency_key,
                            request_hash=request_hash,
                            completed=False,
                            response_json=None,
                            created_at_utc=now,
                        )
                    )
                    reserved = True
            except Exception:
                # SQLite raises IntegrityError on the concurrent key.
                reserved = False
            existing = (
                connection.execute(
                    select(
                        outbox_replay_idempotency_table.c.request_hash,
                        outbox_replay_idempotency_table.c.completed,
                        outbox_replay_idempotency_table.c.response_json,
                    ).where(
                        outbox_replay_idempotency_table.c.event_id == event_id,
                        outbox_replay_idempotency_table.c.consumer_name == consumer_name,
                        outbox_replay_idempotency_table.c.idempotency_key == idempotency_key,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None and not reserved:
                if existing["request_hash"] != request_hash:
                    raise PlatformError(
                        "idempotency_key_conflict",
                        "Idempotency key was reused with a different request",
                        {},
                        409,
                    )
                if existing["completed"] and isinstance(existing["response_json"], dict):
                    response = dict(existing["response_json"])
                    return OpsReplayReceipt(
                        event_id=str(response["event_id"]),
                        consumer_name=str(response["consumer_name"]),
                        status="pending",
                        replay_generation=int(response["replay_generation"]),
                        version=int(response["version"]),
                    )
                raise PlatformError(
                    "idempotency_in_progress", "Request is still in progress", {}, 409
                )
            if delivery["status"] != "dead_letter":
                raise PlatformError(
                    "outbox_delivery_not_replayable",
                    "Only dead-lettered deliveries can be replayed",
                    {},
                    409,
                )
            if int(delivery["version"]) != expected_version:
                raise PlatformError(
                    "version_conflict",
                    "Delivery version is no longer current",
                    {},
                    409,
                )
            next_version = int(delivery["version"]) + 1
            next_replay_generation = int(delivery["replay_generation"]) + 1
            updated = connection.execute(
                update(outbox_delivery_table)
                .where(
                    outbox_delivery_table.c.event_id == event_id,
                    outbox_delivery_table.c.consumer_name == consumer_name,
                    outbox_delivery_table.c.version == expected_version,
                    outbox_delivery_table.c.status == "dead_letter",
                )
                .values(
                    status="pending",
                    version=next_version,
                    replay_generation=next_replay_generation,
                    attempt_number=int(delivery["attempt_number"]),
                    cycle_attempt_number=0,
                    error_category=None,
                    error_code=None,
                    next_attempt_at_utc=now,
                    lease_owner=None,
                    lease_expires_at_utc=None,
                    fence_token=outbox_delivery_table.c.fence_token,
                )
            ).rowcount
            if updated != 1:
                raise PlatformError(
                    "outbox_delivery_not_replayable",
                    "Delivery state changed before replay",
                    {},
                    409,
                )
            response = {
                "event_id": event_id,
                "consumer_name": consumer_name,
                "status": "pending",
                "replay_generation": next_replay_generation,
                "version": next_version,
            }
            connection.execute(
                update(outbox_replay_idempotency_table)
                .where(
                    outbox_replay_idempotency_table.c.event_id == event_id,
                    outbox_replay_idempotency_table.c.consumer_name == consumer_name,
                    outbox_replay_idempotency_table.c.idempotency_key == idempotency_key,
                    outbox_replay_idempotency_table.c.request_hash == request_hash,
                    outbox_replay_idempotency_table.c.completed.is_(False),
                )
                .values(completed=True, response_json=response)
            )
            self._record_metric(connection, "outbox.deliveries.replayed", event_id=event_id)
            return OpsReplayReceipt(
                event_id=event_id,
                consumer_name=consumer_name,
                status="pending",
                replay_generation=next_replay_generation,
                version=next_version,
            )

    def compact_due_events(self, *, now: datetime | None = None) -> int:
        """Compact full events whose retention elapsed and all deliveries are delivered."""
        compacted = 0
        with self._engine.begin() as connection:
            current = _utc(now) if now is not None else self._current_time(connection)
            if self._metrics is not None:
                self._metrics.prune_before(
                    connection,
                    cutoff=current - timedelta(days=self._retention_days),
                )
            candidates = (
                connection.execute(
                    select(outbox_event_table.c.event_id).where(
                        outbox_event_table.c.storage_state == "full",
                        outbox_event_table.c.compact_after_at_utc.is_not(None),
                        outbox_event_table.c.compact_after_at_utc <= current,
                    )
                )
                .scalars()
                .all()
            )
            for event_id in candidates:
                if compact_event(connection, event_id, current):
                    compacted += 1
        return compacted


def _is_permanent_materialization_error(exc: BaseException) -> bool:
    """Permanent: unsupported schema/payload contract violations that retrying
    can never fix. Retryable: transient/internal/connection errors."""
    if isinstance(exc, PlatformError):
        return exc.status_code in {400, 403, 404, 409, 422}
    return False


def _permanent_error_code(exc: BaseException) -> str:
    if isinstance(exc, PlatformError):
        return exc.code
    return "internal_error"


def _default_title(event_type: str) -> str:
    titles = {
        "ingestion_completed": "Document ingestion completed",
        "ocr_low_confidence": "Low-confidence OCR result",
        "submission_approved": "Submission approved",
        "submission_rejected": "Submission rejected",
        "submission_invalidated": "Submission invalidated",
        "quota_approved": "Quota request approved",
        "quota_rejected": "Quota request rejected",
        "calibration_window_suggested": "Calibration window suggested",
        "graph_build_completed": "Knowledge graph build completed",
        "evaluation_judge_configuration_missing": "Evaluation judge configuration missing",
    }
    return titles.get(event_type, "Notification")


def _document_id_for(event_type: str, payload: dict[str, object]) -> str | None:
    if event_type in {"ingestion_completed", "ocr_low_confidence"}:
        value = payload.get("document_id")
        return str(value) if value else None
    return None


def _document_version_id_for(event_type: str, payload: dict[str, object]) -> str | None:
    if event_type in {"ingestion_completed", "ocr_low_confidence"}:
        value = payload.get("document_version_id")
        return str(value) if value else None
    return None


def _jittered_delay(delay_seconds: int) -> timedelta:
    import random

    factor = 1.0 + random.uniform(-0.2, 0.2)
    return timedelta(seconds=max(1, int(delay_seconds * factor)))
