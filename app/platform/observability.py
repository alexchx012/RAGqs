from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from .database import (
    _insert_do_nothing,
    platform_observability_aggregate_table,
    platform_observability_sample_table,
)
from .errors import PlatformError

HISTOGRAM_BOUNDARIES_MS = (1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000)
Window = Literal["today", "7d", "30d"]
Audience = Literal["ops", "admin"]
_DEFAULT_ROUTE_TEMPLATES = frozenset({"/v1/health"})
_ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"})
_ALLOWED_OUTCOMES = frozenset(
    {
        "success",
        "validation_error",
        "authentication_error",
        "authorization_error",
        "server_error",
        "gateway_timeout",
        "unhandled_error",
        "other",
    }
)
_ALLOWED_STATUS_FAMILIES = frozenset({"1xx", "2xx", "3xx", "4xx", "5xx", "other"})


class ObservabilityMetricsError(PlatformError):
    pass


@dataclass(frozen=True, slots=True)
class ObservabilitySample:
    observed_at_utc: datetime
    route_template: str
    method: str
    outcome_class: str
    status_family: str
    latency_ms: int
    sample_weight: float

    def __post_init__(self) -> None:
        timestamp = self.observed_at_utc
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        object.__setattr__(self, "observed_at_utc", timestamp.astimezone(UTC))
        if self.latency_ms < 0 or self.sample_weight <= 0 or not math.isfinite(self.sample_weight):
            raise ValueError(
                "observability latency and sample_weight must be bounded positive values"
            )


@dataclass(frozen=True, slots=True)
class ObservabilityReadRequest:
    caller: str
    audience: str
    window: str


@dataclass(frozen=True, slots=True)
class LatencyRead:
    p50_ms: int | None
    p95_ms: int | None
    p99_ms: int | None
    sample_weight: float


@dataclass(frozen=True, slots=True)
class ApiRead:
    sampled_request_weight: float
    server_error_weight: float
    error_rate: float | None
    latency: LatencyRead


@dataclass(frozen=True, slots=True)
class SamplingRead:
    success_sample_rate: float
    error_sample_rate: float
    weighted: bool


@dataclass(frozen=True, slots=True)
class ObservabilityMetricsRead:
    window: Window
    start_at_utc: datetime
    end_at_utc: datetime
    api: ApiRead
    sampling: SamplingRead
    data_state: Literal["available", "empty", "insufficient_sample"]


class ObservabilityMetricsPort(Protocol):
    def record(self, sample: ObservabilitySample) -> None: ...

    def read(self, request: ObservabilityReadRequest) -> ObservabilityMetricsRead: ...


def sample_success(stable_request_hash: str, rate: float) -> tuple[bool, float]:
    if rate <= 0:
        return False, 0.0
    if rate >= 1:
        return True, 1.0
    digest = hashlib.sha256(stable_request_hash.encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:8], "big") / 2**64
    return bucket < rate, 1.0 / rate if bucket < rate else 0.0


def _clamp_observed_at(sample: ObservabilitySample, now: datetime) -> ObservabilitySample:
    return replace(sample, observed_at_utc=now) if sample.observed_at_utc > now else sample


class InMemoryObservabilityMetrics:
    def __init__(
        self,
        *,
        now: Callable[[], datetime],
        retention_days: int = 90,
        success_sample_rate: float = 0.1,
        allowed_route_templates: Iterable[str] | None = None,
        max_route_templates: int = 100,
        minimum_sample_weight: float = 1.0,
    ) -> None:
        if retention_days < 31 or retention_days > 366:
            raise ValueError("observability retention must be between 31 and 366 days")
        if not 0 <= success_sample_rate <= 1:
            raise ValueError("observability success sample rate must be between 0 and 1")
        if max_route_templates < 1:
            raise ValueError("observability max route templates must be positive")
        self._now = now
        self.retention_days = retention_days
        self.success_sample_rate = success_sample_rate
        self.allowed_route_templates = frozenset(
            allowed_route_templates or _DEFAULT_ROUTE_TEMPLATES
        )
        self.max_route_templates = max_route_templates
        self.minimum_sample_weight = minimum_sample_weight
        self._samples: list[ObservabilitySample] = []
        self._seen_routes: set[str] = set()

    @property
    def samples(self) -> tuple[ObservabilitySample, ...]:
        return tuple(self._samples)

    def _utc_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def _sanitize(self, sample: ObservabilitySample) -> ObservabilitySample:
        route = sample.route_template
        if route not in self.allowed_route_templates:
            route = "other"
        elif route not in self._seen_routes and len(self._seen_routes) >= self.max_route_templates:
            route = "other"
        if route != "other":
            self._seen_routes.add(route)
        method = sample.method.upper() if sample.method.upper() in _ALLOWED_METHODS else "other"
        outcome = sample.outcome_class if sample.outcome_class in _ALLOWED_OUTCOMES else "other"
        status = (
            sample.status_family if sample.status_family in _ALLOWED_STATUS_FAMILIES else "other"
        )
        return ObservabilitySample(
            observed_at_utc=sample.observed_at_utc,
            route_template=route,
            method=method,
            outcome_class=outcome,
            status_family=status,
            latency_ms=sample.latency_ms,
            sample_weight=sample.sample_weight,
        )

    def record(self, sample: ObservabilitySample) -> None:
        current = self._utc_now()
        normalized = _clamp_observed_at(sample, current)
        if normalized.observed_at_utc < current - timedelta(days=self.retention_days):
            return
        self._samples.append(self._sanitize(normalized))

    def prune(self) -> None:
        current = self._utc_now()
        self._samples = [
            item
            for item in self._samples
            if item.observed_at_utc >= current - timedelta(days=self.retention_days)
        ]

    @staticmethod
    def _window_bounds(now: datetime, window: str) -> tuple[datetime, datetime]:
        if window == "today":
            start = datetime(now.year, now.month, now.day, tzinfo=UTC)
        elif window in {"7d", "30d"}:
            start = now - timedelta(days=int(window[:-1]))
        else:
            raise ObservabilityMetricsError(
                "invalid_window",
                "The observability window is invalid",
                {},
                422,
            )
        return start, now

    def _validate_request(self, request: ObservabilityReadRequest) -> Window:
        if request.caller != "retention-ops":
            raise ObservabilityMetricsError(
                "forbidden",
                "The metrics caller is not allowed",
                {},
                403,
            )
        if request.audience not in {"ops", "admin"}:
            raise ObservabilityMetricsError(
                "invalid_audience",
                "The metrics audience is invalid",
                {},
                422,
            )
        if request.window not in {"today", "7d", "30d"}:
            raise ObservabilityMetricsError(
                "invalid_window",
                "The observability window is invalid",
                {},
                422,
            )
        return request.window  # type: ignore[return-value]

    @staticmethod
    def _weighted_percentile(samples: list[ObservabilitySample], percentile: float) -> int | None:
        if not samples:
            return None
        ordered = sorted(samples, key=lambda item: item.latency_ms)
        total = sum(item.sample_weight for item in ordered)
        threshold = total * percentile
        cumulative = 0.0
        for item in ordered:
            cumulative += item.sample_weight
            if cumulative >= threshold:
                return item.latency_ms
        return ordered[-1].latency_ms

    def read(self, request: ObservabilityReadRequest) -> ObservabilityMetricsRead:
        window = self._validate_request(request)
        now = self._utc_now()
        start, end = self._window_bounds(now, window)
        selected = [item for item in self._samples if start <= item.observed_at_utc < end]
        total_weight = sum(item.sample_weight for item in selected)
        server_error_weight = sum(
            item.sample_weight
            for item in selected
            if item.status_family == "5xx"
            or item.outcome_class in {"gateway_timeout", "unhandled_error"}
        )
        latency = LatencyRead(
            p50_ms=self._weighted_percentile(selected, 0.50),
            p95_ms=self._weighted_percentile(selected, 0.95),
            p99_ms=self._weighted_percentile(selected, 0.99),
            sample_weight=total_weight,
        )
        if not selected:
            state: Literal["available", "empty", "insufficient_sample"] = "empty"
            api = ApiRead(0, 0, None, LatencyRead(None, None, None, 0))
        elif total_weight < self.minimum_sample_weight:
            state = "insufficient_sample"
            api = ApiRead(
                total_weight, server_error_weight, None, LatencyRead(None, None, None, total_weight)
            )
        else:
            state = "available"
            api = ApiRead(
                total_weight, server_error_weight, server_error_weight / total_weight, latency
            )
        return ObservabilityMetricsRead(
            window=window,
            start_at_utc=start,
            end_at_utc=end,
            api=api,
            sampling=SamplingRead(self.success_sample_rate, 1.0, True),
            data_state=state,
        )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _floor_hour(value: datetime) -> datetime:
    value = _as_utc(value)
    return value.replace(minute=0, second=0, microsecond=0)


def _ceil_hour(value: datetime) -> datetime:
    floor = _floor_hour(value)
    return floor if floor == value else floor + timedelta(hours=1)


class SqlAlchemyObservabilityMetrics:
    """Database-backed, bounded telemetry for the core-owned aggregate read port."""

    def __init__(
        self,
        engine: Engine,
        *,
        now: Callable[[], datetime],
        retention_days: int = 90,
        success_sample_rate: float = 0.1,
        allowed_route_templates: Iterable[str] | None = None,
        max_route_templates: int = 100,
        minimum_sample_weight: float = 1.0,
    ) -> None:
        if retention_days < 31 or retention_days > 366:
            raise ValueError("observability retention must be between 31 and 366 days")
        if not 0 <= success_sample_rate <= 1:
            raise ValueError("observability success sample rate must be between 0 and 1")
        if max_route_templates < 1:
            raise ValueError("observability max route templates must be positive")
        self._engine = engine
        self._now = now
        self.retention_days = retention_days
        self.success_sample_rate = success_sample_rate
        self.allowed_route_templates = frozenset(
            allowed_route_templates or _DEFAULT_ROUTE_TEMPLATES
        )
        self.max_route_templates = max_route_templates
        self.minimum_sample_weight = minimum_sample_weight

    def _utc_now(self) -> datetime:
        return _as_utc(self._now())

    def _sanitize(self, connection: Connection, sample: ObservabilitySample) -> ObservabilitySample:
        route = sample.route_template
        if route not in self.allowed_route_templates:
            route = "other"
        elif route != "other":
            seen = connection.execute(
                select(
                    func.count(func.distinct(platform_observability_sample_table.c.route_template))
                ).where(platform_observability_sample_table.c.route_template != "other")
            ).scalar_one()
            already_seen = connection.execute(
                select(platform_observability_sample_table.c.id)
                .where(platform_observability_sample_table.c.route_template == route)
                .limit(1)
            ).scalar_one_or_none()
            if already_seen is None and int(seen) >= self.max_route_templates:
                route = "other"
        method = sample.method.upper() if sample.method.upper() in _ALLOWED_METHODS else "other"
        outcome = sample.outcome_class if sample.outcome_class in _ALLOWED_OUTCOMES else "other"
        status = (
            sample.status_family if sample.status_family in _ALLOWED_STATUS_FAMILIES else "other"
        )
        return ObservabilitySample(
            observed_at_utc=sample.observed_at_utc,
            route_template=route,
            method=method,
            outcome_class=outcome,
            status_family=status,
            latency_ms=sample.latency_ms,
            sample_weight=sample.sample_weight,
        )

    @staticmethod
    def _latency_bucket(latency_ms: int) -> int:
        for boundary in HISTOGRAM_BOUNDARIES_MS:
            if latency_ms <= boundary:
                return boundary
        return HISTOGRAM_BOUNDARIES_MS[-1]

    @staticmethod
    def _aggregate_key(sample: ObservabilitySample, retention_days: int) -> dict[str, object]:
        return {
            "bucket_start_utc": _floor_hour(sample.observed_at_utc),
            "route_template": sample.route_template,
            "method": sample.method,
            "outcome_class": sample.outcome_class,
            "status_family": sample.status_family,
            "latency_bucket_ms": SqlAlchemyObservabilityMetrics._latency_bucket(sample.latency_ms),
            "retention_days": retention_days,
        }

    @staticmethod
    def _aggregate_expires_at(sample: ObservabilitySample, retention_days: int) -> datetime:
        return _floor_hour(sample.observed_at_utc) + timedelta(hours=1, days=retention_days)

    def _trim_expired(self, connection: Connection, now: datetime) -> None:
        connection.execute(
            delete(platform_observability_sample_table).where(
                platform_observability_sample_table.c.expires_at_utc <= now
            )
        )
        connection.execute(
            delete(platform_observability_aggregate_table).where(
                platform_observability_aggregate_table.c.expires_at_utc <= now
            )
        )

    def record(self, sample: ObservabilitySample) -> None:
        try:
            now = self._utc_now()
            normalized_input = _clamp_observed_at(sample, now)
            if normalized_input.observed_at_utc < now - timedelta(days=self.retention_days):
                return
            with self._engine.begin() as connection:
                normalized = self._sanitize(connection, normalized_input)
                connection.execute(
                    platform_observability_sample_table.insert().values(
                        observed_at_utc=normalized.observed_at_utc,
                        route_template=normalized.route_template,
                        method=normalized.method,
                        outcome_class=normalized.outcome_class,
                        status_family=normalized.status_family,
                        latency_ms=normalized.latency_ms,
                        sample_weight=normalized.sample_weight,
                        retention_days=self.retention_days,
                        expires_at_utc=normalized.observed_at_utc
                        + timedelta(days=self.retention_days),
                    )
                )
                key = self._aggregate_key(normalized, self.retention_days)
                inserted = connection.execute(
                    _insert_do_nothing(
                        connection,
                        platform_observability_aggregate_table,
                        {
                            **key,
                            "expires_at_utc": self._aggregate_expires_at(
                                normalized, self.retention_days
                            ),
                            "sample_weight": normalized.sample_weight,
                            "sample_count": 1,
                        },
                        list(key),
                    )
                ).rowcount
                if inserted != 1:
                    predicates = [
                        platform_observability_aggregate_table.c[column] == value
                        for column, value in key.items()
                    ]
                    connection.execute(
                        update(platform_observability_aggregate_table)
                        .where(and_(*predicates))
                        .values(
                            sample_weight=(
                                platform_observability_aggregate_table.c.sample_weight
                                + normalized.sample_weight
                            ),
                            sample_count=platform_observability_aggregate_table.c.sample_count + 1,
                        )
                    )
        except SQLAlchemyError as exc:
            raise ObservabilityMetricsError(
                "observability_metrics_unavailable",
                "Observability metrics are temporarily unavailable",
                {},
                503,
            ) from exc

    def prune(self) -> None:
        """Apply the retention policy to raw samples and fixed aggregate buckets."""

        try:
            with self._engine.begin() as connection:
                self._trim_expired(connection, self._utc_now())
        except SQLAlchemyError as exc:
            raise ObservabilityMetricsError(
                "observability_metrics_unavailable",
                "Observability metrics are temporarily unavailable",
                {},
                503,
            ) from exc

    @staticmethod
    def _weighted_percentile(samples: list[tuple[int, float]], percentile: float) -> int | None:
        if not samples:
            return None
        ordered = sorted(samples)
        total = sum(weight for _, weight in ordered)
        threshold = total * percentile
        cumulative = 0.0
        for latency_ms, weight in ordered:
            cumulative += weight
            if cumulative >= threshold:
                return latency_ms
        return ordered[-1][0]

    def _read_histogram(
        self,
        connection: Connection,
        start: datetime,
        end: datetime,
    ) -> list[tuple[int, float, str, str]]:
        first_full_hour = _ceil_hour(start)
        last_full_hour = _floor_hour(end)
        entries: list[tuple[int, float, str, str]] = []
        ranges: tuple[tuple[datetime, datetime], ...]
        if first_full_hour < last_full_hour:
            aggregates = connection.execute(
                select(
                    platform_observability_aggregate_table.c.latency_bucket_ms,
                    platform_observability_aggregate_table.c.sample_weight,
                    platform_observability_aggregate_table.c.outcome_class,
                    platform_observability_aggregate_table.c.status_family,
                ).where(
                    and_(
                        platform_observability_aggregate_table.c.bucket_start_utc
                        >= first_full_hour,
                        platform_observability_aggregate_table.c.bucket_start_utc < last_full_hour,
                    )
                )
            ).mappings()
            entries.extend(
                (
                    int(row["latency_bucket_ms"]),
                    float(row["sample_weight"]),
                    str(row["outcome_class"]),
                    str(row["status_family"]),
                )
                for row in aggregates
            )
            ranges = ((start, min(first_full_hour, end)), (max(last_full_hour, start), end))
        else:
            ranges = ((start, end),)

        for range_start, range_end in ranges:
            if range_start >= range_end:
                continue
            samples = connection.execute(
                select(
                    platform_observability_sample_table.c.latency_ms,
                    platform_observability_sample_table.c.sample_weight,
                    platform_observability_sample_table.c.outcome_class,
                    platform_observability_sample_table.c.status_family,
                ).where(
                    and_(
                        platform_observability_sample_table.c.observed_at_utc >= range_start,
                        platform_observability_sample_table.c.observed_at_utc < range_end,
                    )
                )
            ).mappings()
            entries.extend(
                (
                    int(row["latency_ms"]),
                    float(row["sample_weight"]),
                    str(row["outcome_class"]),
                    str(row["status_family"]),
                )
                for row in samples
            )
        return entries

    @staticmethod
    def _window_bounds(now: datetime, window: str) -> tuple[datetime, datetime]:
        if window == "today":
            start = datetime(now.year, now.month, now.day, tzinfo=UTC)
        elif window in {"7d", "30d"}:
            start = now - timedelta(days=int(window[:-1]))
        else:
            raise ObservabilityMetricsError(
                "invalid_window",
                "The observability window is invalid",
                {},
                422,
            )
        return start, now

    @staticmethod
    def _validate_request(request: ObservabilityReadRequest) -> Window:
        if request.caller != "retention-ops":
            raise ObservabilityMetricsError(
                "forbidden",
                "The metrics caller is not allowed",
                {},
                403,
            )
        if request.audience not in {"ops", "admin"}:
            raise ObservabilityMetricsError(
                "invalid_audience",
                "The metrics audience is invalid",
                {},
                422,
            )
        if request.window not in {"today", "7d", "30d"}:
            raise ObservabilityMetricsError(
                "invalid_window",
                "The observability window is invalid",
                {},
                422,
            )
        return request.window  # type: ignore[return-value]

    def read(self, request: ObservabilityReadRequest) -> ObservabilityMetricsRead:
        window = self._validate_request(request)
        try:
            now = self._utc_now()
            start, end = self._window_bounds(now, window)
            with self._engine.begin() as connection:
                selected = self._read_histogram(connection, start, end)
        except SQLAlchemyError as exc:
            raise ObservabilityMetricsError(
                "observability_metrics_unavailable",
                "Observability metrics are temporarily unavailable",
                {},
                503,
            ) from exc

        total_weight = sum(weight for _, weight, _, _ in selected)
        server_error_weight = sum(
            weight
            for _, weight, outcome, status in selected
            if status == "5xx" or outcome in {"gateway_timeout", "unhandled_error"}
        )
        latency_samples = [(latency_ms, weight) for latency_ms, weight, _, _ in selected]
        if not selected:
            state: Literal["available", "empty", "insufficient_sample"] = "empty"
            api = ApiRead(0, 0, None, LatencyRead(None, None, None, 0))
        elif total_weight < self.minimum_sample_weight:
            state = "insufficient_sample"
            api = ApiRead(
                total_weight,
                server_error_weight,
                None,
                LatencyRead(None, None, None, total_weight),
            )
        else:
            state = "available"
            api = ApiRead(
                total_weight,
                server_error_weight,
                server_error_weight / total_weight,
                LatencyRead(
                    self._weighted_percentile(latency_samples, 0.50),
                    self._weighted_percentile(latency_samples, 0.95),
                    self._weighted_percentile(latency_samples, 0.99),
                    total_weight,
                ),
            )
        return ObservabilityMetricsRead(
            window=window,
            start_at_utc=start,
            end_at_utc=end,
            api=api,
            sampling=SamplingRead(self.success_sample_rate, 1.0, True),
            data_state=state,
        )
