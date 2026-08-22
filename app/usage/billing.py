"""Immutable provider billing source import and reconciliation."""

from __future__ import annotations

import re
import secrets
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.engine import Connection

from app.platform.errors import PlatformError

from ._fingerprint import ledger_fingerprint
from ._sql import _insert_do_nothing
from .ledger import Clock, OwnershipSnapshot, UsageLedger
from .observability import UsageResourceMetrics
from .price import normalize_currency_code
from .schema import (
    provider_billing_cost_adjustment_table,
    provider_billing_reconciliation_group_table,
    provider_billing_source_group_table,
    provider_billing_source_record_table,
    usage_cost_projection_table,
    usage_event_table,
)


@dataclass(frozen=True, slots=True)
class BillingSourceRecord:
    provider: str
    provider_account_id: str
    billing_source_record_id: str
    model: str
    operation: str
    provider_request_id: str | None
    service_start_utc: datetime | None
    service_end_utc: datetime | None
    service_month: str | None
    measurements: dict[str, int]
    amount: Decimal
    currency_code: str
    source_status: str
    source_metadata: dict[str, str]


@dataclass(frozen=True, slots=True)
class ReconciledMonth:
    adjustment_id: str
    period: str
    amount_delta: Decimal


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    source_record_id: str
    status: str
    amount_delta: Decimal | None
    adjustment_event_id: str | None
    adjustments: tuple[ReconciledMonth, ...]


def _require_text(value: object, name: str, limit: int, *, allow_empty: bool = False) -> str:
    if value is None and allow_empty:
        return ""
    if not isinstance(value, str) or (not value.strip() and not allow_empty):
        raise PlatformError("validation_error", f"{name} must be a non-empty string", {}, 422)
    text = value.strip()
    if len(text) > limit:
        raise PlatformError(
            "validation_error", f"{name} must be at most {limit} characters", {}, 422
        )
    return text


def _money(value: Decimal, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise PlatformError("validation_error", f"{name} must be a finite Decimal", {}, 422)
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -10:
        raise PlatformError("validation_error", f"{name} exceeds 10 decimal places", {}, 422)
    if abs(value) >= Decimal("99999999999999999999.9999999999"):
        raise PlatformError("validation_error", f"{name} exceeds the storage range", {}, 422)
    return value


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


_SENSITIVE_METADATA_FRAGMENTS = (
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
    "request_body",
    "response_body",
    "image",
)


def _source_metadata(value: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, item in value.items():
        normalized = str(key)
        lowered = normalized.casefold()
        if any(fragment in lowered for fragment in _SENSITIVE_METADATA_FRAGMENTS):
            raise PlatformError(
                "billing_source_metadata_rejected",
                "Billing source metadata contains a sensitive field",
                {"field": normalized},
                422,
            )
        if not isinstance(item, (str, int, float)) or isinstance(item, bool):
            raise PlatformError(
                "validation_error", "Billing source metadata values must be scalar", {}, 422
            )
        result[normalized] = item
    return result


class ProviderBillingService:
    """Import normalized bill rows and append idempotent cost corrections."""

    def __init__(
        self,
        ledger: UsageLedger,
        clock: Clock,
        metrics: UsageResourceMetrics | None = None,
    ) -> None:
        self.ledger = ledger
        self.clock = clock
        self.metrics = metrics or UsageResourceMetrics()
        self.engine = ledger._engine

    def import_record(self, record: BillingSourceRecord) -> str:
        if not str(record.billing_source_record_id or "").strip():
            raise PlatformError(
                "billing_source_id_missing",
                "A provider billing source record requires a stable source ID",
                {},
                422,
            )
        values = self._record_values(record)
        identity = [
            "provider",
            "provider_account_id",
            "billing_source_record_id",
        ]
        with self.engine.begin() as connection:
            existing = (
                connection.execute(
                    select(provider_billing_source_record_table).where(
                        *[
                            provider_billing_source_record_table.c[name] == values[name]
                            for name in identity
                        ]
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if existing["content_fingerprint"] != values["content_fingerprint"]:
                    raise PlatformError(
                        "billing_source_conflict",
                        "The billing source identity was imported with different content",
                        {"provider": values["provider"]},
                        409,
                    )
                self.metrics.increment("provider_billing_import", outcome="replayed")
                return str(existing["provider_billing_source_record_id"])
            inserted = _insert_do_nothing(
                connection,
                provider_billing_source_record_table,
                values,
                identity,
            )
            if not inserted:
                existing = (
                    connection.execute(
                        select(provider_billing_source_record_table).where(
                            *[
                                provider_billing_source_record_table.c[name] == values[name]
                                for name in identity
                            ]
                        )
                    )
                    .mappings()
                    .one()
                )
                if existing["content_fingerprint"] != values["content_fingerprint"]:
                    raise PlatformError(
                        "billing_source_conflict",
                        "The billing source identity was imported with different content",
                        {"provider": values["provider"]},
                        409,
                    )
                self.metrics.increment("provider_billing_import", outcome="replayed")
                return str(existing["provider_billing_source_record_id"])
            self.metrics.increment("provider_billing_import", outcome="created")
            return str(values["provider_billing_source_record_id"])

    def reconcile(
        self,
        source_record_id: str,
        *,
        ownership: OwnershipSnapshot,
        allocations: list[dict[str, object]] | None = None,
    ) -> ReconciliationResult:
        self.ledger._validate_ownership(ownership)
        with self.engine.begin() as connection:
            source = self._source(connection, source_record_id)
            if source["provider_request_id"]:
                return self._reconcile_request_linked(
                    connection, source=source, ownership=ownership
                )
            return self._reconcile_group(
                connection,
                source=source,
                ownership=ownership,
                allocations=(
                    allocations
                    if allocations is not None
                    else (
                        [
                            {
                                "period": source["service_month"],
                                "amount_delta": Decimal(str(source["amount"])),
                            }
                        ]
                        if source["service_month"] is not None
                        else []
                    )
                ),
            )

    def cost_projection(self, *, provider: str | None = None) -> list[dict[str, Any]]:
        self.rebuild_cost_projection()
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(usage_cost_projection_table).order_by(
                    usage_cost_projection_table.c.target_kind,
                    usage_cost_projection_table.c.target_id,
                    usage_cost_projection_table.c.effective_period,
                )
            ).mappings()
            result = [dict(row) for row in rows]
            if provider is None:
                return result
            source_ids = {
                str(row["provider_billing_source_record_id"])
                for row in connection.execute(
                    select(
                        provider_billing_source_record_table.c.provider_billing_source_record_id
                    ).where(provider_billing_source_record_table.c.provider == provider)
                )
            }
            return [
                row
                for row in result
                if row["provider_billing_source_record_id"] in source_ids
                or row["target_kind"] in {"provider_event", "local_usage"}
            ]

    def rebuild_cost_projection(self) -> int:
        """Rebuild all derived cost projection rows without mutating source facts."""

        with self.engine.begin() as connection:
            now = _utc(self.clock.now_utc(connection))
            lock = self.ledger.calendar.lock_or_verify(connection)
            recorded_period = self.ledger.calendar.period_for(lock, now)
            sources = (
                connection.execute(select(provider_billing_source_record_table)).mappings().all()
            )
            source_by_request = {
                (row["provider"], row["provider_request_id"]): row
                for row in sources
                if row["provider_request_id"] is not None
            }
            adjustments = (
                connection.execute(
                    select(usage_event_table).where(
                        usage_event_table.c.event_kind == "cost_adjustment"
                    )
                )
                .mappings()
                .all()
            )
            adjustment_by_reference = {
                (row["referenced_usage_event_id"], row["adjustment_source_id"]): row
                for row in adjustments
            }
            groups = {
                str(row["provider_billing_reconciliation_group_id"]): row
                for row in connection.execute(
                    select(provider_billing_reconciliation_group_table)
                ).mappings()
            }
            links = connection.execute(select(provider_billing_source_group_table)).mappings()
            group_adjustments = connection.execute(
                select(provider_billing_cost_adjustment_table)
            ).mappings()
            adjustments_by_source: dict[str, list[Any]] = {}
            for row in group_adjustments:
                adjustments_by_source.setdefault(str(row["adjustment_source_id"]), []).append(row)

            connection.execute(usage_cost_projection_table.delete())
            rows: list[dict[str, Any]] = []
            usage_events = (
                connection.execute(
                    select(usage_event_table).where(
                        usage_event_table.c.event_kind.in_(("provider_usage", "local_usage"))
                    )
                )
                .mappings()
                .all()
            )
            for event in usage_events:
                source = source_by_request.get((event["provider"], event["provider_request_id"]))
                adjustment = (
                    adjustment_by_reference.get(
                        (
                            event["usage_event_id"],
                            source["provider_billing_source_record_id"],
                        )
                    )
                    if source is not None
                    else None
                )
                estimated = (
                    Decimal(str(event["estimated_cost_amount"]))
                    if event["estimated_cost_status"] == "complete"
                    and event["estimated_cost_amount"] is not None
                    else None
                )
                adjustment_amount = (
                    Decimal(str(adjustment["estimated_cost_amount"]))
                    if adjustment is not None
                    else None
                )
                projected = (
                    Decimal(str(source["amount"]))
                    if source is not None and adjustment is not None
                    else estimated
                )
                rows.append(
                    {
                        "target_kind": (
                            "provider_event"
                            if event["event_kind"] == "provider_usage"
                            else "local_usage"
                        ),
                        "target_id": str(event["usage_event_id"]),
                        "provider_billing_source_record_id": (
                            str(source["provider_billing_source_record_id"])
                            if source is not None
                            else None
                        ),
                        "currency_code": event["currency_code"],
                        "estimated_amount": (
                            estimated if event["event_kind"] == "provider_usage" else None
                        ),
                        "adjustment_amount": adjustment_amount,
                        "projected_amount": projected,
                        "cost_status": (
                            "reconciled"
                            if source is not None and adjustment is not None
                            else "provisional"
                        ),
                        "effective_calendar_version_id": event["effective_calendar_version_id"],
                        "effective_at_utc": _utc(event["effective_at_utc"]),
                        "effective_period": event["effective_period"],
                        "recorded_calendar_version_id": lock.version_id,
                        "recorded_at_utc": now,
                        "recorded_period": recorded_period,
                        "rebuilt_at_utc": now,
                    }
                )

            source_by_id = {str(row["provider_billing_source_record_id"]): row for row in sources}
            for link in links:
                source = source_by_id.get(str(link["provider_billing_source_record_id"]))
                if source is None or source["provider_request_id"] is not None:
                    continue
                group = groups.get(str(link["provider_billing_reconciliation_group_id"]))
                if group is None:
                    continue
                allocated = adjustments_by_source.get(
                    str(source["provider_billing_source_record_id"]), []
                )
                if not allocated:
                    rows.append(
                        {
                            "target_kind": "reconciliation_group",
                            "target_id": str(group["provider_billing_reconciliation_group_id"]),
                            "provider_billing_source_record_id": str(
                                source["provider_billing_source_record_id"]
                            ),
                            "currency_code": source["currency_code"],
                            "estimated_amount": None,
                            "adjustment_amount": None,
                            "projected_amount": None,
                            "cost_status": "billing_period_unallocated",
                            "effective_calendar_version_id": lock.version_id,
                            "effective_at_utc": _utc(
                                source["service_start_utc"] or source["service_end_utc"] or now
                            ),
                            "effective_period": source["service_month"] or recorded_period,
                            "recorded_calendar_version_id": lock.version_id,
                            "recorded_at_utc": now,
                            "recorded_period": recorded_period,
                            "rebuilt_at_utc": now,
                        }
                    )
                    continue
                for adjustment in allocated:
                    rows.append(
                        {
                            "target_kind": "reconciliation_group",
                            "target_id": str(group["provider_billing_reconciliation_group_id"]),
                            "provider_billing_source_record_id": str(
                                source["provider_billing_source_record_id"]
                            ),
                            "currency_code": adjustment["currency_code"],
                            "estimated_amount": None,
                            "adjustment_amount": Decimal(str(adjustment["amount_delta"])),
                            "projected_amount": Decimal(str(adjustment["amount_delta"])),
                            "cost_status": "reconciled",
                            "effective_calendar_version_id": adjustment[
                                "effective_calendar_version_id"
                            ],
                            "effective_at_utc": _utc(adjustment["effective_at_utc"]),
                            "effective_period": adjustment["effective_period"],
                            "recorded_calendar_version_id": lock.version_id,
                            "recorded_at_utc": now,
                            "recorded_period": recorded_period,
                            "rebuilt_at_utc": now,
                        }
                    )
            for row in rows:
                connection.execute(
                    usage_cost_projection_table.insert().values(
                        usage_cost_projection_id=f"ucp_{secrets.token_urlsafe(9)}",
                        **row,
                    )
                )
            status_counts: dict[str, int] = {}
            for row in rows:
                status_counts[str(row["cost_status"])] = (
                    status_counts.get(str(row["cost_status"]), 0) + 1
                )
            for status, count in status_counts.items():
                self.metrics.increment_by("usage_cost_projection_status", count, outcome=status)
            self.metrics.increment("usage_cost_projection_rebuild")
            return len(rows)

    def _upsert_cost_projection(
        self,
        connection: Connection,
        *,
        target_kind: str,
        target_id: str,
        source_record_id: str | None,
        currency_code: str | None,
        estimated_amount: Decimal | None,
        adjustment_amount: Decimal | None,
        projected_amount: Decimal | None,
        cost_status: str,
        effective_at_utc: datetime,
        effective_period: str,
        calendar_version: str,
        recorded_period: str,
        now: datetime,
    ) -> None:
        values = {
            "target_kind": target_kind,
            "target_id": target_id,
            "provider_billing_source_record_id": source_record_id,
            "currency_code": currency_code,
            "estimated_amount": estimated_amount,
            "adjustment_amount": adjustment_amount,
            "projected_amount": projected_amount,
            "cost_status": cost_status,
            "effective_calendar_version_id": calendar_version,
            "effective_at_utc": effective_at_utc,
            "effective_period": effective_period,
            "recorded_calendar_version_id": calendar_version,
            "recorded_at_utc": now,
            "recorded_period": recorded_period,
            "rebuilt_at_utc": now,
        }
        existing = (
            connection.execute(
                select(usage_cost_projection_table).where(
                    usage_cost_projection_table.c.target_kind == target_kind,
                    usage_cost_projection_table.c.target_id == target_id,
                    usage_cost_projection_table.c.effective_period == effective_period,
                    usage_cost_projection_table.c.provider_billing_source_record_id.is_(
                        source_record_id
                    ),
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            connection.execute(
                usage_cost_projection_table.update()
                .where(
                    usage_cost_projection_table.c.usage_cost_projection_id
                    == existing["usage_cost_projection_id"]
                )
                .values(**values)
            )
            return
        connection.execute(
            usage_cost_projection_table.insert().values(
                usage_cost_projection_id=f"ucp_{secrets.token_urlsafe(9)}",
                **values,
            )
        )

    def _record_values(self, record: BillingSourceRecord) -> dict[str, Any]:
        provider = _require_text(record.provider, "provider", 64)
        account = _require_text(record.provider_account_id, "provider_account_id", 128)
        source_id = _require_text(record.billing_source_record_id, "billing_source_record_id", 128)
        model = _require_text(record.model, "model", 128)
        operation = _require_text(record.operation, "operation", 64)
        request_id = (
            _require_text(record.provider_request_id, "provider_request_id", 256)
            if record.provider_request_id is not None
            else None
        )
        status = _require_text(record.source_status, "source_status", 32)
        currency = normalize_currency_code(record.currency_code)
        amount = _money(record.amount, "amount")
        if amount < 0:
            raise PlatformError("validation_error", "amount must be non-negative", {}, 422)
        if (
            record.service_start_utc is None
            and record.service_end_utc is None
            and record.service_month is None
        ):
            raise PlatformError("validation_error", "service period is required", {}, 422)
        if record.service_month is not None and not re.fullmatch(
            r"\d{4}-(0[1-9]|1[0-2])", record.service_month
        ):
            raise PlatformError("validation_error", "service_month must be YYYY-MM", {}, 422)
        if not isinstance(record.measurements, dict) or not isinstance(
            record.source_metadata, dict
        ):
            raise PlatformError("validation_error", "billing metadata must be objects", {}, 422)
        payload = asdict(record)
        return {
            "provider_billing_source_record_id": f"pbs_{secrets.token_urlsafe(9)}",
            "provider": provider,
            "provider_account_id": account,
            "billing_source_record_id": source_id,
            "model": model,
            "operation": operation,
            "provider_request_id": request_id,
            "service_start_utc": (
                _utc(record.service_start_utc) if record.service_start_utc else None
            ),
            "service_end_utc": _utc(record.service_end_utc) if record.service_end_utc else None,
            "service_month": record.service_month,
            "measurements": dict(record.measurements),
            "amount": amount,
            "currency_code": currency,
            "source_status": status,
            "source_metadata": _source_metadata(record.source_metadata),
            "content_fingerprint": ledger_fingerprint("provider_billing_source", payload),
            "created_at_utc": _utc(self.clock.now_utc()),
        }

    def _source(self, connection: Connection, source_record_id: str):
        row = (
            connection.execute(
                select(provider_billing_source_record_table).where(
                    provider_billing_source_record_table.c.provider_billing_source_record_id
                    == source_record_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise PlatformError("billing_source_not_found", "Billing source was not found", {}, 404)
        return row

    def _reconcile_request_linked(
        self,
        connection: Connection,
        *,
        source: Any,
        ownership: OwnershipSnapshot,
    ) -> ReconciliationResult:
        usage = (
            connection.execute(
                select(usage_event_table).where(
                    usage_event_table.c.event_kind == "provider_usage",
                    usage_event_table.c.provider == source["provider"],
                    usage_event_table.c.provider_request_id == source["provider_request_id"],
                )
            )
            .mappings()
            .one_or_none()
        )
        if usage is None:
            raise PlatformError("usage_event_not_found", "Provider usage was not found", {}, 404)
        if usage["currency_code"] != source["currency_code"]:
            raise PlatformError(
                "billing_currency_mismatch",
                "Billing and usage currencies do not match",
                {},
                422,
            )
        delta = Decimal(str(source["amount"])) - Decimal(str(usage["estimated_cost_amount"]))
        event_id = self.ledger._append_adjustment(
            event_kind="cost_adjustment",
            referenced_event_id=str(usage["usage_event_id"]),
            adjustment_source_namespace="provider_billing",
            adjustment_source_id=str(source["provider_billing_source_record_id"]),
            adjustment_allocation_key="request",
            ownership=ownership,
            result="cost_adjusted",
            extra_values={
                "estimated_cost_amount": delta,
                "currency_code": source["currency_code"],
                "estimated_cost_status": "complete",
            },
            connection=connection,
        )
        lock = self.ledger.calendar.lock_or_verify(connection)
        now = _utc(self.clock.now_utc(connection))
        self._upsert_cost_projection(
            connection,
            target_kind="provider_event",
            target_id=str(usage["usage_event_id"]),
            source_record_id=str(source["provider_billing_source_record_id"]),
            currency_code=str(source["currency_code"]),
            estimated_amount=Decimal(str(usage["estimated_cost_amount"])),
            adjustment_amount=delta,
            projected_amount=Decimal(str(source["amount"])),
            cost_status="reconciled",
            effective_at_utc=_utc(usage["effective_at_utc"]),
            effective_period=str(usage["effective_period"]),
            calendar_version=str(usage["effective_calendar_version_id"]),
            recorded_period=self.ledger.calendar.period_for(lock, now),
            now=now,
        )
        self.metrics.increment("provider_billing_reconcile", outcome="request_linked")
        return ReconciliationResult(
            source_record_id=str(source["provider_billing_source_record_id"]),
            status="reconciled",
            amount_delta=delta,
            adjustment_event_id=str(event_id),
            adjustments=(),
        )

    def _reconcile_group(
        self,
        connection: Connection,
        *,
        source: Any,
        ownership: OwnershipSnapshot,
        allocations: list[dict[str, object]],
    ) -> ReconciliationResult:
        now = _utc(self.clock.now_utc(connection))
        lock = self.ledger.calendar.lock_or_verify(connection)
        recorded_period = self.ledger.calendar.period_for(lock, now)
        group_id = self._ensure_group(connection, source=source, now=now)
        if not allocations:
            self._upsert_cost_projection(
                connection,
                target_kind="reconciliation_group",
                target_id=group_id,
                source_record_id=str(source["provider_billing_source_record_id"]),
                currency_code=str(source["currency_code"]),
                estimated_amount=None,
                adjustment_amount=None,
                projected_amount=None,
                cost_status="billing_period_unallocated",
                effective_at_utc=_utc(
                    source["service_start_utc"]
                    or source["service_end_utc"]
                    or datetime(1970, 1, 1, tzinfo=UTC)
                ),
                effective_period=source["service_month"] or recorded_period,
                calendar_version=lock.version_id,
                recorded_period=recorded_period,
                now=now,
            )
            self.metrics.increment(
                "provider_billing_reconcile", outcome="billing_period_unallocated"
            )
            return ReconciliationResult(group_id, "billing_period_unallocated", None, None, ())

        total = Decimal("0")
        normalized: list[tuple[str, Decimal]] = []
        for allocation in allocations:
            period = _require_text(allocation.get("period"), "allocation period", 7)
            amount = _money(Decimal(str(allocation.get("amount_delta"))), "amount_delta")
            if not period or len(period) != 7 or period[4] != "-":
                raise PlatformError(
                    "validation_error", "allocation period must be YYYY-MM", {}, 422
                )
            total += amount
            normalized.append((period, amount))
        if total != Decimal(str(source["amount"])):
            raise PlatformError(
                "billing_allocation_mismatch",
                "Month allocations must sum to the billed amount",
                {},
                422,
            )

        adjustments: list[ReconciledMonth] = []
        for period, amount in normalized:
            adjustment_id = self._insert_group_adjustment(
                connection,
                source=source,
                group_id=group_id,
                period=period,
                amount=amount,
                lock_version=lock.version_id,
                timezone=lock.timezone,
                now=now,
                recorded_period=recorded_period,
            )
            self._upsert_cost_projection(
                connection,
                target_kind="reconciliation_group",
                target_id=group_id,
                source_record_id=str(source["provider_billing_source_record_id"]),
                currency_code=str(source["currency_code"]),
                estimated_amount=None,
                adjustment_amount=amount,
                projected_amount=amount,
                cost_status="reconciled",
                effective_at_utc=datetime(
                    int(period[:4]), int(period[5:]), 1, tzinfo=ZoneInfo(lock.timezone)
                ).astimezone(UTC),
                effective_period=period,
                calendar_version=lock.version_id,
                recorded_period=recorded_period,
                now=now,
            )
            adjustments.append(ReconciledMonth(adjustment_id, period, amount))
        self.metrics.increment("provider_billing_reconcile", outcome="month_group")
        return ReconciliationResult(group_id, "reconciled", None, None, tuple(adjustments))

    def _ensure_group(self, connection: Connection, *, source: Any, now: datetime) -> str:
        group_key = ":".join(
            (
                source["provider"],
                source["provider_account_id"],
                source["model"],
                source["operation"],
                source["currency_code"],
                source["service_month"] or "",
                source["service_start_utc"].isoformat() if source["service_start_utc"] else "",
                source["service_end_utc"].isoformat() if source["service_end_utc"] else "",
            )
        )
        existing = (
            connection.execute(
                select(provider_billing_reconciliation_group_table)
                .where(provider_billing_reconciliation_group_table.c.group_key == group_key)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        values = {
            "provider_billing_reconciliation_group_id": f"pbr_{secrets.token_urlsafe(9)}",
            "group_key": group_key,
            "provider": source["provider"],
            "provider_account_id": source["provider_account_id"],
            "model": source["model"],
            "operation": source["operation"],
            "service_start_utc": source["service_start_utc"],
            "service_end_utc": source["service_end_utc"],
            "service_month": source["service_month"],
            "currency_code": source["currency_code"],
            "created_at_utc": now,
        }
        if existing is not None:
            group_id = str(existing["provider_billing_reconciliation_group_id"])
        else:
            inserted = _insert_do_nothing(
                connection,
                provider_billing_reconciliation_group_table,
                values,
                ["group_key"],
            )
            if inserted:
                group_id = str(values["provider_billing_reconciliation_group_id"])
            else:
                existing = (
                    connection.execute(
                        select(provider_billing_reconciliation_group_table).where(
                            provider_billing_reconciliation_group_table.c.group_key == group_key
                        )
                    )
                    .mappings()
                    .one()
                )
                group_id = str(existing["provider_billing_reconciliation_group_id"])
        _insert_do_nothing(
            connection,
            provider_billing_source_group_table,
            {
                "provider_billing_source_group_id": f"pbsg_{secrets.token_urlsafe(9)}",
                "provider_billing_source_record_id": source["provider_billing_source_record_id"],
                "provider_billing_reconciliation_group_id": group_id,
                "created_at_utc": now,
            },
            [
                "provider_billing_source_record_id",
                "provider_billing_reconciliation_group_id",
            ],
        )
        return group_id

    def _insert_group_adjustment(
        self,
        connection: Connection,
        *,
        source: Any,
        group_id: str,
        period: str,
        amount: Decimal,
        lock_version: str,
        timezone: str,
        now: datetime,
        recorded_period: str,
    ) -> str:
        existing = (
            connection.execute(
                select(provider_billing_cost_adjustment_table).where(
                    provider_billing_cost_adjustment_table.c.adjustment_source_id
                    == source["provider_billing_source_record_id"],
                    provider_billing_cost_adjustment_table.c.adjustment_allocation_key == period,
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            if Decimal(str(existing["amount_delta"])) != amount:
                raise PlatformError(
                    "billing_allocation_conflict",
                    "The billing allocation already exists with a different amount",
                    {},
                    409,
                )
            return str(existing["provider_billing_cost_adjustment_id"])
        year, month = (int(value) for value in period.split("-"))
        effective_at = datetime(year, month, 1, tzinfo=ZoneInfo(timezone)).astimezone(UTC)
        adjustment_id = f"pba_{secrets.token_urlsafe(9)}"
        values = {
            "provider_billing_cost_adjustment_id": adjustment_id,
            "provider_billing_reconciliation_group_id": group_id,
            "event_kind": "cost_adjustment",
            "adjustment_source_namespace": "provider_billing",
            "adjustment_source_id": str(source["provider_billing_source_record_id"]),
            "adjustment_allocation_key": period,
            "amount_delta": amount,
            "currency_code": source["currency_code"],
            "effective_calendar_version_id": lock_version,
            "effective_at_utc": effective_at,
            "effective_period": period,
            "recorded_calendar_version_id": lock_version,
            "recorded_at_utc": now,
            "recorded_period": recorded_period,
            "created_at_utc": now,
        }
        inserted = _insert_do_nothing(
            connection,
            provider_billing_cost_adjustment_table,
            values,
            [
                "event_kind",
                "adjustment_source_namespace",
                "adjustment_source_id",
                "adjustment_allocation_key",
            ],
        )
        if not inserted:
            existing = (
                connection.execute(
                    select(provider_billing_cost_adjustment_table).where(
                        provider_billing_cost_adjustment_table.c.event_kind == "cost_adjustment",
                        provider_billing_cost_adjustment_table.c.adjustment_source_namespace
                        == "provider_billing",
                        provider_billing_cost_adjustment_table.c.adjustment_source_id
                        == values["adjustment_source_id"],
                        provider_billing_cost_adjustment_table.c.adjustment_allocation_key
                        == period,
                    )
                )
                .mappings()
                .one()
            )
            if Decimal(str(existing["amount_delta"])) != amount:
                raise PlatformError(
                    "billing_allocation_conflict",
                    "The billing allocation already exists with a different amount",
                    {},
                    409,
                )
            return str(existing["provider_billing_cost_adjustment_id"])
        return adjustment_id


__all__ = [
    "BillingSourceRecord",
    "ProviderBillingService",
    "ReconciliationResult",
    "ReconciledMonth",
]
