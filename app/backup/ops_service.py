"""Backup/restore operations layer service.

Exposes the archived internal `BackupRestoreService` as instance-level
operations: a versioned schedule/retention policy, manual backup/restore
commands and the external idempotency protocol (Q5/Q6/Q8). The internal state
machine is only ever invoked through its public methods; this service owns the
policy row, the ops idempotency command records, the Q9 restore-period
whitelist decisions and the operator-attributed audit records.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import Connection, func, select, update
from sqlalchemy.exc import IntegrityError

from app.platform.context import current_context
from app.platform.database import platform_audit_table
from app.platform.errors import PlatformError

from .schema import (
    backup_policy_table,
    backup_sets_table,
    ops_idempotency_commands_table,
    repair_targets_table,
    restore_sessions_table,
    restore_stages_table,
)
from .service import BackupRestoreService
from .write_gate import BackupWriteGateReader

ENDPOINT_CREATE_BACKUP = "create_backup"
ENDPOINT_START_RESTORE = "start_restore"
ENDPOINT_RETRY_REPAIR_TARGET = "retry_repair_target"
ENDPOINT_UPDATE_BACKUP_POLICY = "update_backup_policy"

_DEFAULT_POLICY: dict[str, Any] = {
    "enabled": 0,
    "frequency": "daily",
    "local_time": "02:00",
    "weekdays": [],
    "timezone": "UTC",
    "keep_last": 7,
    "retention_days": 30,
    "version": 1,
}

_LOCAL_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_ACTIVE_RESTORE_STATUSES: tuple[str, ...] = ("accepted", "running", "blocked")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(18)}"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return _utc(value).isoformat() if value is not None else None


def hash_idempotency_key(idempotency_key: str) -> str:
    """Stable storage hash; the plaintext key is never persisted (Q8)."""
    return hashlib.sha256(
        b"ops-backup-command-key-v1\0" + idempotency_key.encode("utf-8")
    ).hexdigest()


class BackupOpsService:
    def __init__(
        self,
        engine: Any,
        *,
        backup_service: BackupRestoreService,
        write_gate_reader: BackupWriteGateReader | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._engine = engine
        self._backup_service = backup_service
        self._write_gate_reader = write_gate_reader
        self._now = now or (lambda: datetime.now(UTC))

    def _current_time(self) -> datetime:
        return _utc(self._now())

    # ------------------------------------------------------------------
    # Policy
    # ------------------------------------------------------------------

    def get_policy(self) -> dict[str, Any]:
        now = self._current_time()
        with self._engine.begin() as connection:
            row = self._policy_row(connection, now)
            return self._policy_payload(row, now)

    def patch_policy(
        self,
        *,
        operator_user_id: str,
        expected_version: int,
        changes: Mapping[str, Any],
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[int, dict[str, Any]]:
        key_hash = hash_idempotency_key(idempotency_key)
        record = self._lookup_command(operator_user_id, ENDPOINT_UPDATE_BACKUP_POLICY, key_hash)
        if record is not None:
            return self._replay_or_conflict(
                record,
                request_hash=request_hash,
                action=ENDPOINT_UPDATE_BACKUP_POLICY,
                operator_user_id=operator_user_id,
            )
        now = self._current_time()
        try:
            with self._engine.begin() as connection:
                # Q9 whitelist: policy writes are rejected while a restore
                # holds the maintenance gate; replays above still return.
                if self._backup_service.reads_closed():
                    raise PlatformError(
                        "maintenance_mode",
                        "The instance is in maintenance mode during an active restore",
                        {},
                        503,
                    )
                row = self._policy_row(connection, now)
                if int(row["version"]) != expected_version:
                    raise PlatformError(
                        "version_conflict",
                        "Backup policy version does not match",
                        {"current_version": int(row["version"])},
                        409,
                    )
                merged = self._merge_policy(row, changes)
                self._validate_policy(merged)
                next_version = expected_version + 1
                connection.execute(
                    update(backup_policy_table)
                    .where(backup_policy_table.c.id == 1)
                    .values(
                        enabled=int(bool(merged["enabled"])),
                        frequency=str(merged["frequency"]),
                        local_time=str(merged["local_time"]),
                        weekdays=list(merged["weekdays"]),
                        timezone=str(merged["timezone"]),
                        keep_last=int(merged["keep_last"]),
                        retention_days=int(merged["retention_days"]),
                        version=next_version,
                        updated_by=operator_user_id,
                        updated_at_utc=now,
                    )
                )
                payload = self._policy_payload(
                    {
                        **merged,
                        "version": next_version,
                        "updated_by": operator_user_id,
                        "updated_at_utc": now,
                        "last_scheduled_for_utc": row["last_scheduled_for_utc"],
                        "last_outcome": row["last_outcome"],
                    },
                    now,
                )
                self._insert_command(
                    connection,
                    operator_user_id=operator_user_id,
                    endpoint=ENDPOINT_UPDATE_BACKUP_POLICY,
                    key_hash=key_hash,
                    request_hash=request_hash,
                    response_status=200,
                    response_json=payload,
                    occurred_at=now,
                )
                self._audit_row(
                    connection,
                    actor_id=operator_user_id,
                    action=ENDPOINT_UPDATE_BACKUP_POLICY,
                    resource_id="backup_policy",
                    result="succeeded",
                    details={"version": next_version},
                    occurred_at=now,
                )
                return 200, payload
        except PlatformError as exc:
            self._audit_rejection(
                action=ENDPOINT_UPDATE_BACKUP_POLICY,
                resource_id="backup_policy",
                operator_user_id=operator_user_id,
                error=exc,
            )
            raise
        except IntegrityError:
            # A concurrent same-key request committed between the lookup and
            # this insert: converge on the recorded command.
            record = self._lookup_command(operator_user_id, ENDPOINT_UPDATE_BACKUP_POLICY, key_hash)
            if record is None:  # pragma: no cover - the loser row always exists
                raise
            return self._replay_or_conflict(
                record,
                request_hash=request_hash,
                action=ENDPOINT_UPDATE_BACKUP_POLICY,
                operator_user_id=operator_user_id,
            )

    def _policy_row(self, connection: Connection, now: datetime) -> dict[str, Any]:
        row = (
            connection.execute(select(backup_policy_table).where(backup_policy_table.c.id == 1))
            .mappings()
            .one_or_none()
        )
        if row is None:
            try:
                connection.execute(
                    backup_policy_table.insert().values(
                        id=1,
                        enabled=_DEFAULT_POLICY["enabled"],
                        frequency=_DEFAULT_POLICY["frequency"],
                        local_time=_DEFAULT_POLICY["local_time"],
                        weekdays=list(_DEFAULT_POLICY["weekdays"]),
                        timezone=_DEFAULT_POLICY["timezone"],
                        keep_last=_DEFAULT_POLICY["keep_last"],
                        retention_days=_DEFAULT_POLICY["retention_days"],
                        version=_DEFAULT_POLICY["version"],
                        last_scheduled_for_utc=None,
                        last_outcome=None,
                        updated_by=None,
                        updated_at_utc=now,
                    )
                )
            except IntegrityError:
                # A concurrent reader materialized the default row first.
                pass
            row = (
                connection.execute(select(backup_policy_table).where(backup_policy_table.c.id == 1))
                .mappings()
                .one()
            )
        return dict(row)

    @staticmethod
    def _merge_policy(row: Mapping[str, Any], changes: Mapping[str, Any]) -> dict[str, Any]:
        merged: dict[str, Any] = {
            "enabled": bool(row["enabled"]),
            "frequency": str(row["frequency"]),
            "local_time": str(row["local_time"]),
            "weekdays": list(row["weekdays"] or []),
            "timezone": str(row["timezone"]),
            "keep_last": int(row["keep_last"]),
            "retention_days": int(row["retention_days"]),
        }
        for field in merged:
            if field in changes and changes[field] is not None:
                merged[field] = changes[field]
        if isinstance(merged["weekdays"], list):
            merged["weekdays"] = sorted({int(day) for day in merged["weekdays"]})
        return merged

    @staticmethod
    def _validate_policy(policy: Mapping[str, Any]) -> None:
        if policy["frequency"] not in ("daily", "weekly"):
            raise PlatformError(
                "validation_error",
                "Frequency must be daily or weekly",
                {"field": "frequency"},
                422,
            )
        if not _LOCAL_TIME_PATTERN.match(str(policy["local_time"])):
            raise PlatformError(
                "validation_error",
                "Local time must use HH:MM",
                {"field": "local_time"},
                422,
            )
        try:
            ZoneInfo(str(policy["timezone"]))
        except (TypeError, ValueError, ZoneInfoNotFoundError):
            raise PlatformError(
                "validation_error",
                "Timezone is not a valid IANA timezone",
                {"field": "timezone"},
                422,
            ) from None
        if int(policy["keep_last"]) < 1:
            raise PlatformError(
                "validation_error", "keep_last must be at least 1", {"field": "keep_last"}, 422
            )
        if int(policy["retention_days"]) < 1:
            raise PlatformError(
                "validation_error",
                "retention_days must be at least 1",
                {"field": "retention_days"},
                422,
            )
        weekdays = [int(day) for day in policy["weekdays"]]
        if any(day < 0 or day > 6 for day in weekdays):
            raise PlatformError(
                "validation_error",
                "Weekdays use 0=Monday through 6=Sunday",
                {"field": "weekdays"},
                422,
            )
        if policy["frequency"] == "weekly" and not weekdays:
            raise PlatformError(
                "validation_error",
                "Weekly frequency requires at least one weekday",
                {"field": "weekdays"},
                422,
            )

    def _next_run_at(self, policy: Mapping[str, Any], now: datetime) -> datetime | None:
        if not policy["enabled"]:
            return None
        timezone = ZoneInfo(str(policy["timezone"]))
        hour, minute = (int(part) for part in str(policy["local_time"]).split(":"))
        local_now = now.astimezone(timezone)
        if policy["frequency"] == "daily":
            candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= local_now:
                candidate += timedelta(days=1)
            return candidate.astimezone(UTC)
        weekdays = {int(day) for day in policy["weekdays"]}
        for offset in range(8):
            day = (local_now + timedelta(days=offset)).date()
            if day.weekday() not in weekdays:
                continue
            candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone)
            if candidate <= local_now:
                continue
            return candidate.astimezone(UTC)
        return None  # pragma: no cover - a non-empty weekday set always matches

    def _policy_payload(self, row: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        policy = {
            "enabled": bool(row["enabled"]),
            "frequency": str(row["frequency"]),
            "local_time": str(row["local_time"]),
            "weekdays": [int(day) for day in (row["weekdays"] or [])],
            "timezone": str(row["timezone"]),
            "keep_last": int(row["keep_last"]),
            "retention_days": int(row["retention_days"]),
        }
        return {
            **policy,
            "version": int(row["version"]),
            "last_scheduled_for_utc": _iso(row["last_scheduled_for_utc"]),
            "last_outcome": row["last_outcome"],
            "updated_by": row["updated_by"],
            "updated_at_utc": _iso(row["updated_at_utc"]),
            "next_run_at": _iso(self._next_run_at(policy, now)),
        }

    # ------------------------------------------------------------------
    # Backup commands and history
    # ------------------------------------------------------------------

    def create_backup(
        self,
        *,
        operator_user_id: str,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[int, dict[str, Any]]:
        return self._guarded_command(
            operator_user_id=operator_user_id,
            endpoint=ENDPOINT_CREATE_BACKUP,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            rejection_resource_id="backup_sets",
            execute=self._execute_create_backup,
        )

    def _execute_create_backup(self) -> tuple[int, dict[str, Any], str]:
        if self._backup_service.reads_closed():
            raise PlatformError(
                "maintenance_mode",
                "The instance is in maintenance mode during an active restore",
                {},
                503,
            )
        active = self._active_backup_id()
        if active is not None:
            raise PlatformError(
                "backup_in_progress",
                "Another backup is already in progress",
                {"active_backup_id": active},
                409,
            )
        if self._write_gate_reader is not None and not self._write_gate_reader.writes_open():
            raise PlatformError(
                "backup_in_progress",
                "Another backup is already in progress",
                {},
                409,
            )
        state = self._backup_service.create_backup_set()
        backup_id = str(state["backup_id"])
        return 202, {"backup_id": backup_id, "status": str(state["status"])}, backup_id

    def _active_backup_id(self) -> str | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(backup_sets_table.c.id)
                    .where(backup_sets_table.c.status == "creating")
                    .order_by(backup_sets_table.c.created_at_utc.desc())
                )
                .scalars()
                .first()
            )
        return str(row) if row is not None else None

    def create_scheduled_backup(self) -> tuple[int, dict[str, Any]]:
        """Internal schedule trigger entry (Q5/Q8).

        Runs the exact same persisted creation path (and the same guards) as
        the manual command. The schedule occurrence row claimed by the resident
        worker is the idempotency identity of the window, so no HTTP
        Idempotency-Key is fabricated; the worker owns the schedule audit
        records with the deployment identity.
        """
        status, payload, _resource_id = self._execute_create_backup()
        return status, payload

    def list_backups(self, *, page: int, page_size: int) -> dict[str, Any]:
        self._require_valid_pagination(page, page_size)
        with self._engine.connect() as connection:
            total = connection.execute(
                select(func.count()).select_from(backup_sets_table)
            ).scalar_one()
            rows = (
                connection.execute(
                    select(backup_sets_table)
                    .order_by(
                        backup_sets_table.c.created_at_utc.desc(), backup_sets_table.c.id.desc()
                    )
                    .limit(page_size)
                    .offset((page - 1) * page_size)
                )
                .mappings()
                .all()
            )
        return {
            "items": [self._backup_list_item(row) for row in rows],
            "total": int(total),
            "page": page,
            "page_size": page_size,
        }

    def get_backup(self, backup_id: str) -> dict[str, Any]:
        state = self._backup_service.get_backup_set(backup_id)
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(backup_sets_table).where(backup_sets_table.c.id == backup_id)
                )
                .mappings()
                .one()
            )
        return {
            **state,
            "created_at_utc": _iso(row["created_at_utc"]),
            "completed_at_utc": _iso(row["completed_at_utc"]),
            "purged_at_utc": _iso(row["purged_at_utc"]),
            "restorable": self._is_restorable(row),
        }

    @staticmethod
    def _is_restorable(row: Mapping[str, Any]) -> bool:
        return str(row["status"]) == "complete" and row["purged_at_utc"] is None

    @classmethod
    def _backup_list_item(cls, row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "backup_id": str(row["id"]),
            "status": str(row["status"]),
            "created_at_utc": _iso(row["created_at_utc"]),
            "completed_at_utc": _iso(row["completed_at_utc"]),
            "purged_at_utc": _iso(row["purged_at_utc"]),
            "restorable": cls._is_restorable(row),
        }

    # ------------------------------------------------------------------
    # Restore commands and history
    # ------------------------------------------------------------------

    def start_restore(
        self,
        *,
        operator_user_id: str,
        backup_id: str,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[int, dict[str, Any]]:
        return self._guarded_command(
            operator_user_id=operator_user_id,
            endpoint=ENDPOINT_START_RESTORE,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            rejection_resource_id=backup_id,
            execute=lambda: self._execute_start_restore(backup_id),
        )

    def _execute_start_restore(self, backup_id: str) -> tuple[int, dict[str, Any], str]:
        # The persisted unique index `uq_restore_sessions_active` also covers
        # 'blocked', which the internal pre-check predates; normalize the
        # user-visible conflict here instead of surfacing an integrity error.
        with self._engine.connect() as connection:
            active = (
                connection.execute(
                    select(restore_sessions_table.c.id).where(
                        restore_sessions_table.c.status.in_(_ACTIVE_RESTORE_STATUSES)
                    )
                )
                .scalars()
                .first()
            )
        if active is not None:
            raise PlatformError(
                "restore_in_progress",
                "Another restore is already active",
                {"active_restore_id": str(active)},
                409,
            )
        state = self._backup_service.start_restore(backup_id)
        restore_id = str(state["restore_id"])
        return (
            202,
            {
                "restore_id": restore_id,
                "backup_id": str(state["backup_id"]),
                "status": str(state["status"]),
            },
            restore_id,
        )

    def retry_repair_target(
        self,
        *,
        operator_user_id: str,
        restore_id: str,
        target_id: str,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[int, dict[str, Any]]:
        # Q9 whitelist: allowed while the maintenance gate is closed.
        return self._guarded_command(
            operator_user_id=operator_user_id,
            endpoint=ENDPOINT_RETRY_REPAIR_TARGET,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            rejection_resource_id=target_id,
            execute=lambda: self._execute_retry_repair_target(restore_id, target_id),
        )

    def _execute_retry_repair_target(
        self, restore_id: str, target_id: str
    ) -> tuple[int, dict[str, Any], str]:
        with self._engine.connect() as connection:
            target = (
                connection.execute(
                    select(repair_targets_table).where(repair_targets_table.c.id == target_id)
                )
                .mappings()
                .one_or_none()
            )
        if target is None or str(target["restore_id"]) != restore_id:
            raise PlatformError("repair_target_not_found", "Repair target was not found", {}, 404)
        self._backup_service.retry_repair_target(
            restore_id,
            stage=str(target["stage"]),
            resource_id=str(target["resource_id"]),
        )
        with self._engine.connect() as connection:
            refreshed = (
                connection.execute(
                    select(repair_targets_table.c.status).where(
                        repair_targets_table.c.id == target_id
                    )
                )
                .scalars()
                .one()
            )
        return (
            202,
            {"target_id": target_id, "restore_id": restore_id, "status": str(refreshed)},
            target_id,
        )

    def list_restores(self, *, page: int, page_size: int) -> dict[str, Any]:
        self._require_valid_pagination(page, page_size)
        with self._engine.connect() as connection:
            total = connection.execute(
                select(func.count()).select_from(restore_sessions_table)
            ).scalar_one()
            rows = (
                connection.execute(
                    select(restore_sessions_table)
                    .order_by(
                        restore_sessions_table.c.created_at_utc.desc(),
                        restore_sessions_table.c.id.desc(),
                    )
                    .limit(page_size)
                    .offset((page - 1) * page_size)
                )
                .mappings()
                .all()
            )
            current_stages = self._current_stages(connection, [str(row["id"]) for row in rows])
        items = [
            {
                "restore_id": str(row["id"]),
                "backup_id": str(row["backup_id"]),
                "status": str(row["status"]),
                "current_stage": current_stages.get(str(row["id"])),
                "created_at_utc": _iso(row["created_at_utc"]),
                "completed_at_utc": _iso(row["completed_at_utc"]),
            }
            for row in rows
        ]
        return {"items": items, "total": int(total), "page": page, "page_size": page_size}

    @staticmethod
    def _current_stages(connection: Connection, restore_ids: list[str]) -> dict[str, str | None]:
        if not restore_ids:
            return {}
        stages = (
            connection.execute(
                select(
                    restore_stages_table.c.restore_id,
                    restore_stages_table.c.stage,
                    restore_stages_table.c.status,
                )
                .where(restore_stages_table.c.restore_id.in_(restore_ids))
                .order_by(restore_stages_table.c.position)
            )
            .mappings()
            .all()
        )
        current: dict[str, str | None] = {}
        for stage in stages:
            restore_id = str(stage["restore_id"])
            if current.get(restore_id) is not None:
                continue
            if str(stage["status"]) != "succeeded":
                current[restore_id] = str(stage["stage"])
        return current

    def get_restore(self, restore_id: str) -> dict[str, Any]:
        state = self._backup_service.get_restore(restore_id)
        with self._engine.connect() as connection:
            session = (
                connection.execute(
                    select(restore_sessions_table).where(restore_sessions_table.c.id == restore_id)
                )
                .mappings()
                .one()
            )
            repairs = (
                connection.execute(
                    select(
                        repair_targets_table.c.id,
                        repair_targets_table.c.stage,
                        repair_targets_table.c.resource_id,
                    ).where(repair_targets_table.c.restore_id == restore_id)
                )
                .mappings()
                .all()
            )
        repair_ids = {(str(r["stage"]), str(r["resource_id"])): str(r["id"]) for r in repairs}
        return {
            **state,
            "created_at_utc": _iso(session["created_at_utc"]),
            "completed_at_utc": _iso(session["completed_at_utc"]),
            "repair_targets": [
                {
                    **repair,
                    "target_id": repair_ids.get((str(repair["stage"]), str(repair["resource_id"]))),
                }
                for repair in state["repair_targets"]
            ],
        }

    # ------------------------------------------------------------------
    # External idempotency command guard (Q8)
    # ------------------------------------------------------------------

    def _guarded_command(
        self,
        *,
        operator_user_id: str,
        endpoint: str,
        idempotency_key: str,
        request_hash: str,
        rejection_resource_id: str,
        execute: Callable[[], tuple[int, dict[str, Any], str]],
    ) -> tuple[int, dict[str, Any]]:
        key_hash = hash_idempotency_key(idempotency_key)
        record = self._lookup_command(operator_user_id, endpoint, key_hash)
        if record is not None:
            return self._replay_or_conflict(
                record,
                request_hash=request_hash,
                action=endpoint,
                operator_user_id=operator_user_id,
            )
        try:
            status, payload, resource_id = execute()
        except PlatformError as exc:
            self._audit_rejection(
                action=endpoint,
                resource_id=rejection_resource_id,
                operator_user_id=operator_user_id,
                error=exc,
            )
            raise
        now = self._current_time()
        try:
            with self._engine.begin() as connection:
                self._insert_command(
                    connection,
                    operator_user_id=operator_user_id,
                    endpoint=endpoint,
                    key_hash=key_hash,
                    request_hash=request_hash,
                    response_status=status,
                    response_json=payload,
                    occurred_at=now,
                )
                self._audit_row(
                    connection,
                    actor_id=operator_user_id,
                    action=endpoint,
                    resource_id=resource_id,
                    result="succeeded",
                    details={"endpoint": endpoint},
                    occurred_at=now,
                )
        except IntegrityError:
            # A concurrent same-key request committed first: converge on it.
            record = self._lookup_command(operator_user_id, endpoint, key_hash)
            if record is None:  # pragma: no cover - the winner row always exists
                raise
            return self._replay_or_conflict(
                record,
                request_hash=request_hash,
                action=endpoint,
                operator_user_id=operator_user_id,
            )
        return status, payload

    def _lookup_command(
        self, operator_user_id: str, endpoint: str, key_hash: str
    ) -> dict[str, Any] | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(ops_idempotency_commands_table).where(
                        ops_idempotency_commands_table.c.operator_user_id == operator_user_id,
                        ops_idempotency_commands_table.c.endpoint == endpoint,
                        ops_idempotency_commands_table.c.key_hash == key_hash,
                    )
                )
                .mappings()
                .one_or_none()
            )
        return dict(row) if row is not None else None

    def _insert_command(
        self,
        connection: Connection,
        *,
        operator_user_id: str,
        endpoint: str,
        key_hash: str,
        request_hash: str,
        response_status: int,
        response_json: dict[str, Any],
        occurred_at: datetime,
    ) -> None:
        connection.execute(
            ops_idempotency_commands_table.insert().values(
                id=_new_id("opscmd"),
                operator_user_id=operator_user_id,
                endpoint=endpoint,
                key_hash=key_hash,
                request_hash=request_hash,
                response_status=response_status,
                response_json=response_json,
                created_at_utc=occurred_at,
            )
        )

    def _replay_or_conflict(
        self,
        record: Mapping[str, Any],
        *,
        request_hash: str,
        action: str,
        operator_user_id: str,
    ) -> tuple[int, dict[str, Any]]:
        resource_id = self._record_resource_id(record)
        if str(record["request_hash"]) == request_hash:
            self._audit_standalone(
                actor_id=operator_user_id,
                action=action,
                resource_id=resource_id,
                result="replayed",
                details={"endpoint": str(record["endpoint"])},
            )
            return int(record["response_status"]), dict(record["response_json"])
        self._audit_standalone(
            actor_id=operator_user_id,
            action=action,
            resource_id=resource_id,
            result="rejected",
            details={"reason": "idempotency_key_conflict"},
        )
        raise PlatformError(
            "idempotency_key_conflict",
            "Idempotency-Key was reused with a different request",
            {},
            409,
        )

    @staticmethod
    def _record_resource_id(record: Mapping[str, Any]) -> str:
        payload = record["response_json"]
        if isinstance(payload, Mapping):
            for field in ("target_id", "restore_id", "backup_id"):
                value = payload.get(field)
                if value:
                    return str(value)
        return "backup_policy"

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    def record_authorization_denial(self, *, actor_id: str, action: str, resource_id: str) -> None:
        """API-layer 403s are observable in the same audit stream."""
        self._audit_standalone(
            actor_id=actor_id,
            action=action,
            resource_id=resource_id,
            result="denied",
            details={},
        )

    def _audit_rejection(
        self,
        *,
        action: str,
        resource_id: str,
        operator_user_id: str,
        error: PlatformError,
    ) -> None:
        self._audit_standalone(
            actor_id=operator_user_id,
            action=action,
            resource_id=resource_id,
            result="rejected",
            details={"error_code": error.code},
        )

    def _audit_standalone(
        self,
        *,
        actor_id: str,
        action: str,
        resource_id: str,
        result: str,
        details: dict[str, Any],
    ) -> None:
        # Rejection/replay audits must survive the rollback of the business
        # transaction, so they always commit in their own transaction.
        with self._engine.begin() as connection:
            self._audit_row(
                connection,
                actor_id=actor_id,
                action=action,
                resource_id=resource_id,
                result=result,
                details=details,
                occurred_at=self._current_time(),
            )

    @staticmethod
    def _audit_row(
        connection: Connection,
        *,
        actor_id: str,
        action: str,
        resource_id: str,
        result: str,
        occurred_at: datetime,
        details: dict[str, Any] | None = None,
    ) -> None:
        context = current_context()
        connection.execute(
            platform_audit_table.insert().values(
                actor_id=actor_id,
                resource_type=f"backup_ops.{action}",
                resource_id=resource_id,
                request_id=context.request_id if context is not None else "req_backup_ops",
                occurred_at_utc=occurred_at,
                result=result,
                details_json=details or {},
            )
        )

    @staticmethod
    def _require_valid_pagination(page: int, page_size: int) -> None:
        if page < 1 or page_size < 1 or page_size > 200:
            raise PlatformError("validation_error", "Pagination is invalid", {}, 422)
