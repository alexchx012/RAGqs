"""Recoverable local expensive-stage metering."""

from __future__ import annotations

import secrets
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine

from app.platform.errors import PlatformError

from ._fingerprint import ledger_fingerprint
from ._sql import _insert_do_nothing
from .ledger import Clock, LocalMeasurement, OwnershipSnapshot, UsageLedger, _ownership_json
from .observability import UsageResourceMetrics
from .schema import local_usage_meter_table, local_usage_projection_table

_METER_FIELDS = (
    "item_count",
    "page_count",
    "input_bytes",
    "gpu_milliseconds",
    "cpu_milliseconds",
    "peak_vram_bytes",
)
_SCOPE_FIELDS = ("execution_kind", "execution_id", "stage", "resource_kind")


def _require_text(value: object, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlatformError("validation_error", f"{name} must be a non-empty string", {}, 422)
    text = value.strip()
    if len(text) > limit:
        raise PlatformError(
            "validation_error", f"{name} must be at most {limit} characters", {}, 422
        )
    return text


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _scope(**values: object) -> dict[str, str]:
    limits = {
        "execution_kind": 32,
        "execution_id": 128,
        "stage": 64,
        "resource_kind": 32,
    }
    return {name: _require_text(values[name], name, limit) for name, limit in limits.items()}


def _measurement_values(measurement: LocalMeasurement) -> dict[str, int | None]:
    values = asdict(measurement)
    result: dict[str, int | None] = {}
    for field in _METER_FIELDS:
        value = values[field]
        if value is None:
            result[field] = None
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise PlatformError(
                "validation_error", f"{field} must be a non-negative integer or null", {}, 422
            )
        result[field] = value
    return result


def _measurement_sources(measurement: LocalMeasurement) -> dict[str, str]:
    sources = measurement.measurement_sources
    if not isinstance(sources, dict):
        raise PlatformError("validation_error", "measurement_sources must be an object", {}, 422)
    for field, source in sources.items():
        if field not in _METER_FIELDS:
            raise PlatformError(
                "validation_error", f"Unknown local measurement source field {field!r}", {}, 422
            )
        if source not in {"provider_reported", "client_measured", "estimated"}:
            raise PlatformError(
                "validation_error", f"Invalid measurement source for {field!r}", {}, 422
            )
        if getattr(measurement, field) is None:
            raise PlatformError(
                "validation_error",
                f"measurement_sources may not describe null meter {field!r}",
                {},
                422,
            )
    for field in _METER_FIELDS:
        if getattr(measurement, field) is not None and field not in sources:
            raise PlatformError(
                "validation_error",
                f"non-null local meter {field!r} requires measurement_sources",
                {},
                422,
            )
    return dict(sources)


def _assert_monotonic(existing: Any, incoming: dict[str, int | None]) -> None:
    for field, new_value in incoming.items():
        old_value = existing[field]
        if old_value is None or new_value is None:
            if old_value is not None and new_value is None:
                raise PlatformError(
                    "validation_error", f"checkpoint {field} may not change to null", {}, 422
                )
            continue
        if new_value < old_value:
            raise PlatformError(
                "validation_error",
                f"checkpoint {field} must be monotonic",
                {"field": field, "previous": old_value, "candidate": new_value},
                422,
            )


def _ownership_from_json(value: Any) -> OwnershipSnapshot:
    if not isinstance(value, dict):
        raise PlatformError("metering_invariant", "meter ownership snapshot is invalid", {}, 500)
    source_space_ids = value.get("source_space_ids", ())
    return OwnershipSnapshot(
        actor_user_id=str(value["actor_user_id"]),
        actor_role_snapshot=str(value["actor_role_snapshot"]),
        actor_department_id_snapshot=value.get("actor_department_id_snapshot"),
        quota_subject_user_id=value.get("quota_subject_user_id"),
        cost_center_key=str(value["cost_center_key"]),
        space_id=value.get("space_id"),
        space_kind=value.get("space_kind"),
        space_owner_user_id=value.get("space_owner_user_id"),
        authorization_version=value.get("authorization_version"),
        fence_token=value.get("fence_token"),
        source_space_ids=tuple(str(item) for item in source_space_ids),
    )


class LocalUsageMeterService:
    """Persist only the latest bounded checkpoint and finalize one local usage fact."""

    def __init__(
        self,
        ledger: UsageLedger,
        clock: Clock,
        metrics: UsageResourceMetrics | None = None,
    ) -> None:
        self.ledger = ledger
        self.clock = clock
        self.metrics = metrics or UsageResourceMetrics()
        self.engine: Engine = ledger._engine

    def _row(self, connection: Connection, scope: dict[str, str], *, lock: bool = False):
        conditions = [local_usage_meter_table.c[name] == value for name, value in scope.items()]
        statement = select(local_usage_meter_table).where(*conditions)
        if lock:
            statement = statement.with_for_update()
        return connection.execute(statement).mappings().one_or_none()

    def start(
        self,
        *,
        execution_kind: str,
        execution_id: str,
        stage: str,
        resource_kind: str,
        ownership: OwnershipSnapshot,
        lease_expires_at_utc: datetime,
    ) -> dict[str, Any]:
        scope = _scope(
            execution_kind=execution_kind,
            execution_id=execution_id,
            stage=stage,
            resource_kind=resource_kind,
        )
        self.ledger._validate_ownership(ownership)
        with self.engine.begin() as connection:
            existing = self._row(connection, scope, lock=True)
            now = self.clock.now_utc(connection)
            if existing is not None:
                self.metrics.increment("local_usage_meter_start", outcome="replayed")
                return dict(existing)
            values = {
                "meter_id": f"lum_{secrets.token_urlsafe(9)}",
                **scope,
                "status": "running",
                "ownership_json": _ownership_json(ownership),
                "started_at_utc": _utc(now),
                "lease_expires_at_utc": _utc(lease_expires_at_utc),
                "checkpoint_sequence": 0,
                "tail_estimated": 0,
                "created_at_utc": now,
                "updated_at_utc": now,
            }
            inserted = _insert_do_nothing(connection, local_usage_meter_table, values, list(scope))
            if not inserted:
                existing = self._row(connection, scope, lock=True)
                if existing is None:
                    raise PlatformError(
                        "metering_invariant",
                        "Local usage meter disappeared after concurrent start",
                        {},
                        500,
                    )
                return dict(existing)
            self.metrics.increment("local_usage_meter_start", outcome="created")
            return values

    def checkpoint(
        self,
        *,
        execution_kind: str,
        execution_id: str,
        stage: str,
        resource_kind: str,
        sequence: int,
        measurement: LocalMeasurement,
    ) -> dict[str, Any]:
        scope = _scope(
            execution_kind=execution_kind,
            execution_id=execution_id,
            stage=stage,
            resource_kind=resource_kind,
        )
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise PlatformError("validation_error", "sequence must be a positive integer", {}, 422)
        values = _measurement_values(measurement)
        sources = _measurement_sources(measurement)
        with self.engine.begin() as connection:
            existing = self._row(connection, scope, lock=True)
            if existing is None:
                raise PlatformError("local_usage_meter_not_found", "Meter was not found", {}, 404)
            if existing["status"] != "running":
                raise PlatformError(
                    "local_usage_meter_terminal", "Meter is already terminal", {}, 409
                )
            if sequence <= existing["checkpoint_sequence"]:
                raise PlatformError(
                    "validation_error", "checkpoint sequence must increase", {}, 422
                )
            _assert_monotonic(existing, values)
            now = self.clock.now_utc(connection)
            update_values = {
                **values,
                "measurement_sources": sources,
                "checkpoint_sequence": sequence,
                "updated_at_utc": now,
            }
            connection.execute(
                local_usage_meter_table.update()
                .where(local_usage_meter_table.c.meter_id == existing["meter_id"])
                .values(**update_values)
            )
            self.metrics.observe(
                "local_usage_checkpoint_latency_seconds",
                max(0.0, (_utc(now) - _utc(existing["updated_at_utc"])).total_seconds()),
            )
            self.metrics.increment("local_usage_checkpoint")
            return {**dict(existing), **update_values}

    def finalize(
        self,
        *,
        execution_kind: str,
        execution_id: str,
        stage: str,
        resource_kind: str,
        result: str,
        measurement: LocalMeasurement,
        ownership: OwnershipSnapshot,
        started_at_utc: datetime,
        replay_generation: int = 0,
        tail_estimated: bool = False,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        scope = _scope(
            execution_kind=execution_kind,
            execution_id=execution_id,
            stage=stage,
            resource_kind=resource_kind,
        )
        result = _require_text(result, "result", 32)
        error_code = _require_text(error_code, "error_code", 64) if error_code is not None else None
        values = _measurement_values(measurement)
        sources = _measurement_sources(measurement)
        self.ledger._validate_ownership(ownership)
        try:
            with self.engine.begin() as connection:
                existing = self._row(connection, scope, lock=True)
                if existing is None:
                    raise PlatformError(
                        "local_usage_meter_not_found", "Meter was not found", {}, 404
                    )
                if existing["status"] != "running":
                    return dict(existing)
                _assert_monotonic(existing, values)
                event_id, calendar_version, effective_period, recorded_period = (
                    self._insert_local_usage(
                        connection,
                        scope=scope,
                        measurement=measurement,
                        values=values,
                        ownership=ownership,
                        result=result,
                        started_at_utc=_utc(started_at_utc),
                        replay_generation=replay_generation,
                    )
                )
                now = self.clock.now_utc(connection)
                update_values = {
                    **values,
                    "checkpoint_sequence": int(existing["checkpoint_sequence"]) + 1,
                    "status": "completed" if result != "abandoned" else "abandoned",
                    "completed_at_utc": now if result != "abandoned" else None,
                    "abandoned_at_utc": now if result == "abandoned" else None,
                    "tail_estimated": int(tail_estimated),
                    "result": result,
                    "error_code": error_code,
                    "measurement_sources": sources,
                    "usage_event_id": event_id,
                    "updated_at_utc": now,
                }
                connection.execute(
                    local_usage_meter_table.update()
                    .where(local_usage_meter_table.c.meter_id == existing["meter_id"])
                    .values(**update_values)
                )
                self._upsert_local_projection(
                    connection,
                    scope=scope,
                    values=values,
                    result=result,
                    error_code=error_code,
                    tail_estimated=tail_estimated,
                    sources=sources,
                    ownership=ownership,
                    event_id=event_id,
                    calendar_version=calendar_version,
                    effective_period=effective_period,
                    recorded_period=recorded_period,
                    now=now,
                )
                self.metrics.increment("local_usage_meter_finalize", outcome=result)
                return {**dict(existing), **update_values, "usage_event_id": event_id}
        except PlatformError as exc:
            # A4：本地用量落账指纹冲突——事务已回滚，best-effort 告警后原 409 照常冒泡。
            self.ledger._alert_usage_invariant_conflict(exc)
            raise

    def recover_expired(self, *, now_utc: datetime | None = None) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        with self.engine.begin() as connection:
            current = now_utc or self.clock.now_utc(connection)
            rows = (
                connection.execute(
                    select(local_usage_meter_table)
                    .where(
                        local_usage_meter_table.c.status == "running",
                        local_usage_meter_table.c.lease_expires_at_utc <= current,
                    )
                    .with_for_update()
                )
                .mappings()
                .all()
            )
            expired = [dict(row) for row in rows]
        for row in expired:
            scope = {name: row[name] for name in _SCOPE_FIELDS}
            measurement = LocalMeasurement(
                **{field: row[field] for field in _METER_FIELDS},
                measurement_sources=dict(row["measurement_sources"] or {}),
            )
            results.append(
                self.finalize(
                    **scope,
                    result="abandoned",
                    measurement=measurement,
                    ownership=_ownership_from_json(row["ownership_json"]),
                    started_at_utc=_utc(row["started_at_utc"]),
                    tail_estimated=True,
                    error_code="meter_lease_expired",
                )
            )
            self.metrics.increment("local_usage_meter_recovered")
        return results

    def _insert_local_usage(
        self,
        connection: Connection,
        *,
        scope: dict[str, str],
        measurement: LocalMeasurement,
        values: dict[str, int | None],
        ownership: OwnershipSnapshot,
        result: str,
        started_at_utc: datetime,
        replay_generation: int,
    ) -> tuple[str, str, str, str]:
        now = self.clock.now_utc(connection)
        lock = self.ledger.calendar.lock_or_verify(connection)
        effective_period = self.ledger.calendar.period_for(lock, started_at_utc)
        recorded_period = self.ledger.calendar.period_for(lock, now)
        measurement_payload = asdict(measurement)
        measurement_sources = measurement_payload.pop("measurement_sources", {})
        fingerprint_payload = {
            "scope": tuple(scope[name] for name in _SCOPE_FIELDS),
            "ownership": _ownership_json(ownership),
            "started_at_utc": started_at_utc,
            "measurement": measurement_payload,
            "result": result,
            "effective_period": effective_period,
        }
        if measurement_sources:
            fingerprint_payload["measurement_sources"] = measurement_sources
        if replay_generation:
            fingerprint_payload["replay_generation"] = replay_generation
        fingerprint = ledger_fingerprint("local_usage", fingerprint_payload)
        persisted = self.ledger._insert_usage_once(
            connection,
            index_elements=list(_SCOPE_FIELDS),
            values={
                "usage_event_id": f"ue_{secrets.token_urlsafe(9)}",
                "event_kind": "local_usage",
                **scope,
                "replay_generation": replay_generation,
                "cost_center_key": ownership.cost_center_key,
                **values,
                "result": result,
                "event_fingerprint": fingerprint,
                "ownership_json": _ownership_json(ownership),
                "started_at_utc": _utc(started_at_utc),
                "completed_at_utc": now,
                "effective_calendar_version_id": lock.version_id,
                "effective_at_utc": started_at_utc,
                "effective_period": effective_period,
                "recorded_calendar_version_id": lock.version_id,
                "recorded_at_utc": now,
                "recorded_period": recorded_period,
                "created_at_utc": now,
            },
        )
        return str(persisted), lock.version_id, effective_period, recorded_period

    def _upsert_local_projection(
        self,
        connection: Connection,
        *,
        scope: dict[str, str],
        values: dict[str, int | None],
        result: str,
        error_code: str | None,
        tail_estimated: bool,
        sources: dict[str, str],
        ownership: OwnershipSnapshot,
        event_id: str,
        calendar_version: str,
        effective_period: str,
        recorded_period: str,
        now: datetime,
    ) -> None:
        projection_values = {
            "usage_event_id": event_id,
            **scope,
            **values,
            "result": result,
            "error_code": error_code,
            "tail_estimated": int(tail_estimated),
            "measurement_sources": sources,
            "ownership_json": _ownership_json(ownership),
            "effective_calendar_version_id": calendar_version,
            "effective_at_utc": self._row_by_scope(connection, scope)["started_at_utc"],
            "effective_period": effective_period,
            "recorded_calendar_version_id": calendar_version,
            "recorded_at_utc": _utc(now),
            "recorded_period": recorded_period,
            "updated_at_utc": _utc(now),
        }
        existing = (
            connection.execute(
                select(local_usage_projection_table).where(
                    local_usage_projection_table.c.usage_event_id == event_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            connection.execute(
                local_usage_projection_table.update()
                .where(local_usage_projection_table.c.usage_event_id == event_id)
                .values(**projection_values)
            )
            return
        inserted = _insert_do_nothing(
            connection,
            local_usage_projection_table,
            {
                "local_usage_projection_id": f"lup_{secrets.token_urlsafe(9)}",
                **projection_values,
            },
            ["usage_event_id"],
        )
        if not inserted:
            connection.execute(
                local_usage_projection_table.update()
                .where(local_usage_projection_table.c.usage_event_id == event_id)
                .values(**projection_values)
            )

    def _row_by_scope(self, connection: Connection, scope: dict[str, str]):
        return self._row(connection, scope)


__all__ = ["LocalUsageMeterService"]
