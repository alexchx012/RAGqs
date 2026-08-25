"""Resident backup maintenance worker (Q5/Q6/Q7).

One resident loop owns four leased tasks, all recovering from Postgres
persistent state rather than from in-process queues:

* ``backup.schedule`` — claims due schedule windows from the versioned policy;
  only the most recent missed window is re-run, earlier missed windows are
  recorded as ``skipped_missed`` occurrences (Q5). The occurrence row is the
  idempotency identity of a window; no HTTP Idempotency-Key is fabricated (Q8).
* ``backup.execute`` — runs the write-gate quiesce protocol (Q7) and the
  provider snapshot sequence for every ``creating`` backup set, then releases
  the gate whatever the outcome.
* ``backup.restore`` — drives accepted/running restore sessions until they
  complete, fail or block on repair targets (a blocked session waits for the
  operator's repair retry through the ops API).
* ``backup.retention`` — protective-AND expiry (Q6) with durable cleanup
  targets, so provider deletion retries survive process restarts.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, delete, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.platform.config import PlatformSettings
from app.platform.context import current_context
from app.platform.database import platform_audit_table
from app.platform.errors import PlatformError
from app.platform.persistence import FenceViolation, LeaseUnavailable
from app.platform.worker import WorkerRuntime, create_worker_runtime

from .ops_service import BackupOpsService
from .ports import ObjectManifestPort, ObjectSnapshotPort, PostgresBackupPort
from .schema import (
    backup_cleanup_targets_table,
    backup_components_table,
    backup_policy_table,
    backup_schedule_occurrences_table,
    backup_sets_table,
    backup_write_gate_table,
    repair_targets_table,
    restore_sessions_table,
)
from .service import BackupRestoreService
from .write_gate import (
    WRITE_GATE_CLOSED,
    WRITE_GATE_CLOSING,
    WRITE_GATE_OPEN,
    InFlightWriteTracker,
)

_logger = logging.getLogger(__name__)

TASK_SCHEDULE = "backup.schedule"
TASK_EXECUTE = "backup.execute"
TASK_RESTORE = "backup.restore"
TASK_RETENTION = "backup.retention"

SCHEDULER_ACTOR = "backup_scheduler"
EXECUTOR_ACTOR = "backup_executor"
RETENTION_ACTOR = "backup_retention"

_ACTIVE_RESTORE_STATUSES: tuple[str, ...] = ("accepted", "running", "blocked")
_DRIVABLE_RESTORE_STATUSES: tuple[str, ...] = ("accepted", "running")
_UNFINISHED_CLEANUP_STATUSES: tuple[str, ...] = ("pending", "deleting", "failed")
# Simple linear backoff for provider deletion retries (Q6).
_RETRY_BASE_DELAY = timedelta(minutes=5)
# advance_restore executes at most one stage per call; the bound only guards
# against a pathological session that never leaves an active status.
_MAX_RESTORE_ADVANCES_PER_TICK = 32
# Mirrors BackupOpsService's materialized defaults when no policy row exists.
_DEFAULT_KEEP_LAST = 7
_DEFAULT_RETENTION_DAYS = 30


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(18)}"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class _DrainTimeout(Exception):
    """The write gate could not quiesce within the configured drain timeout."""


class _SnapshotStepFailed(Exception):
    """A provider snapshot step failed; carries the component kind to fail."""

    def __init__(self, kind: str, cause: Exception) -> None:
        super().__init__(f"{kind}: {cause}")
        self.kind = kind
        self.cause = cause


@dataclass(frozen=True, slots=True)
class BackupMaintenanceWorkerStats:
    completed: int
    deferred: int


class BackupMaintenanceWorker:
    """Leased worker driving schedule, backup execution, restore advancement
    and retention expiry around the internal `BackupRestoreService`."""

    def __init__(
        self,
        worker_runtime: WorkerRuntime,
        *,
        ops_service: BackupOpsService,
        backup_service: BackupRestoreService,
        engine: Any,
        postgres_backup: PostgresBackupPort,
        object_snapshot: ObjectSnapshotPort,
        object_manifest: ObjectManifestPort,
        write_tracker: InFlightWriteTracker,
        gate_settle_seconds: float = 2.0,
        gate_drain_timeout_seconds: float = 30.0,
        retention_batch_limit: int = 50,
        drain_poll_seconds: float = 0.05,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._worker_runtime = worker_runtime
        self._ops_service = ops_service
        self._backup_service = backup_service
        self._engine = engine
        self._postgres_backup = postgres_backup
        self._object_snapshot = object_snapshot
        self._object_manifest = object_manifest
        self._write_tracker = write_tracker
        self._gate_settle_seconds = float(gate_settle_seconds)
        self._gate_drain_timeout_seconds = float(gate_drain_timeout_seconds)
        self._retention_batch_limit = int(retention_batch_limit)
        self._drain_poll_seconds = float(drain_poll_seconds)
        self._now = now or (lambda: datetime.now(UTC))

    def _current_time(self) -> datetime:
        return _utc(self._now())

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------

    def run_once(self, *, owner: str) -> BackupMaintenanceWorkerStats:
        normalized_owner = owner.strip()
        if not normalized_owner:
            raise ValueError("worker owner must not be empty")
        # Crash recovery first: a gate left closing/closed for a backup that is
        # already terminal (or gone) belongs to a dead executor; the guarded
        # update makes the release single-writer across instances.
        self._recover_stale_gate()
        completed = 0
        deferred = 0
        for task_id, task in (
            (TASK_SCHEDULE, self._schedule_task),
            (TASK_EXECUTE, self._execute_task),
            (TASK_RESTORE, self._restore_task),
            (TASK_RETENTION, self._retention_task),
        ):
            try:
                task(normalized_owner)
            except (FenceViolation, LeaseUnavailable, PlatformError, SQLAlchemyError) as exc:
                _logger.warning("backup task %s deferred: %s", task_id, exc)
                deferred += 1
            except Exception:
                # A single failing task must not take the other tasks down.
                _logger.exception("backup task %s failed", task_id)
                deferred += 1
            else:
                completed += 1
        return BackupMaintenanceWorkerStats(completed=completed, deferred=deferred)

    def run_forever(
        self,
        *,
        owner: str,
        interval_seconds: int = 60,
        stop: Callable[[], bool] | None = None,
    ) -> None:
        while True:
            if stop is not None and stop():
                return
            try:
                self.run_once(owner=owner)
            except Exception:
                _logger.exception("backup maintenance loop iteration failed")
            time.sleep(interval_seconds)

    # ------------------------------------------------------------------
    # Schedule (Q5): due-window claims from the versioned policy
    # ------------------------------------------------------------------

    def _schedule_task(self, owner: str) -> None:
        self._worker_runtime.run_task(TASK_SCHEDULE, owner, partial(self._schedule_tick))

    def _schedule_tick(self, _context: Any, _connection: Any) -> None:
        now = self._current_time()
        policy = self._policy_row()
        if policy is None or not int(policy["enabled"]):
            return
        if self._backup_service.reads_closed():
            # A restore holds the maintenance gate (Q9): do not consume windows
            # while it runs; afterwards the catch-up rule re-runs the latest
            # missed window and marks the earlier ones skipped_missed.
            return
        start = policy["last_scheduled_for_utc"] or policy["updated_at_utc"]
        windows = self._due_windows(policy, _utc(start), now)
        if not windows:
            return
        for missed in windows[:-1]:
            self._mark_window_missed(missed, now)
        self._claim_latest_window(windows[-1], now)

    @staticmethod
    def _due_windows(policy: Mapping[str, Any], start: datetime, now: datetime) -> list[datetime]:
        """Scheduled instants in (start, now] under the policy timezone."""
        timezone = ZoneInfo(str(policy["timezone"]))
        hour, minute = (int(part) for part in str(policy["local_time"]).split(":"))
        weekly = str(policy["frequency"]) == "weekly"
        weekdays = {int(day) for day in (policy["weekdays"] or [])}
        windows: list[datetime] = []
        day = start.astimezone(timezone).date()
        last_day = now.astimezone(timezone).date()
        while day <= last_day:
            if not weekly or day.weekday() in weekdays:
                candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone)
                candidate_utc = candidate.astimezone(UTC)
                if start < candidate_utc <= now:
                    windows.append(candidate_utc)
            day += timedelta(days=1)
        return windows

    def _mark_window_missed(self, scheduled_for: datetime, now: datetime) -> None:
        occurrence_id = _new_id("occ")
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    backup_schedule_occurrences_table.insert().values(
                        id=occurrence_id,
                        scheduled_for_utc=scheduled_for,
                        backup_id=None,
                        outcome="skipped_missed",
                        created_at_utc=now,
                    )
                )
                self._audit_row(
                    connection,
                    actor_id=SCHEDULER_ACTOR,
                    resource_type="backup_schedule.skipped_missed",
                    resource_id=occurrence_id,
                    result="skipped",
                    occurred_at=now,
                    details={"scheduled_for_utc": scheduled_for.isoformat()},
                )
        except IntegrityError:
            # Another instance already recorded this window.
            return

    def _claim_latest_window(self, scheduled_for: datetime, now: datetime) -> None:
        occurrence_id = _new_id("occ")
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    backup_schedule_occurrences_table.insert().values(
                        id=occurrence_id,
                        scheduled_for_utc=scheduled_for,
                        backup_id=None,
                        outcome="executed",
                        created_at_utc=now,
                    )
                )
        except IntegrityError:
            # The unique scheduled_for_utc claim means a peer owns this window.
            return
        try:
            _status, payload = self._ops_service.create_scheduled_backup()
        except PlatformError as exc:
            if exc.code == "backup_in_progress":
                # A non-terminal backup already exists: skip this trigger (Q5).
                self._finalize_occurrence(
                    occurrence_id,
                    scheduled_for,
                    outcome="skipped_active_backup",
                    backup_id=None,
                    now=now,
                    audit_details={
                        key: str(value)
                        for key, value in exc.details.items()
                        if key == "active_backup_id"
                    },
                )
                return
            # maintenance_mode (or anything unexpected): the window was not
            # consumed; release the claim so a later tick can re-claim it.
            self._release_claim(occurrence_id)
            if exc.code == "maintenance_mode":
                return
            raise
        except Exception:
            self._release_claim(occurrence_id)
            raise
        self._finalize_occurrence(
            occurrence_id,
            scheduled_for,
            outcome="executed",
            backup_id=str(payload["backup_id"]),
            now=now,
            audit_details={},
        )

    def _finalize_occurrence(
        self,
        occurrence_id: str,
        scheduled_for: datetime,
        *,
        outcome: str,
        backup_id: str | None,
        now: datetime,
        audit_details: dict[str, Any],
    ) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                update(backup_schedule_occurrences_table)
                .where(backup_schedule_occurrences_table.c.id == occurrence_id)
                .values(outcome=outcome, backup_id=backup_id)
            )
            connection.execute(
                update(backup_policy_table)
                .where(backup_policy_table.c.id == 1)
                .values(last_scheduled_for_utc=scheduled_for, last_outcome=outcome)
            )
            details = {"scheduled_for_utc": scheduled_for.isoformat(), **audit_details}
            if outcome == "executed":
                self._audit_row(
                    connection,
                    actor_id=SCHEDULER_ACTOR,
                    resource_type="backup_schedule.triggered",
                    resource_id=str(backup_id),
                    result="succeeded",
                    occurred_at=now,
                    details=details,
                )
            else:
                self._audit_row(
                    connection,
                    actor_id=SCHEDULER_ACTOR,
                    resource_type=f"backup_schedule.{outcome}",
                    resource_id=occurrence_id,
                    result="skipped",
                    occurred_at=now,
                    details=details,
                )

    def _release_claim(self, occurrence_id: str) -> None:
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    delete(backup_schedule_occurrences_table).where(
                        backup_schedule_occurrences_table.c.id == occurrence_id
                    )
                )
        except SQLAlchemyError:
            _logger.warning("could not release schedule claim %s", occurrence_id)

    # ------------------------------------------------------------------
    # Execute (Q7): write-gate quiesce + provider snapshot sequence
    # ------------------------------------------------------------------

    def _execute_task(self, owner: str) -> None:
        for backup_id in self._creating_backup_ids():
            try:
                self._worker_runtime.run_task(
                    TASK_EXECUTE, owner, partial(self._execute_backup, backup_id)
                )
            except (FenceViolation, LeaseUnavailable):
                continue
            except (PlatformError, SQLAlchemyError) as exc:
                _logger.warning("backup %s execution deferred: %s", backup_id, exc)

    def _creating_backup_ids(self) -> list[str]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(backup_sets_table.c.id)
                    .where(backup_sets_table.c.status == "creating")
                    .order_by(backup_sets_table.c.created_at_utc)
                )
                .scalars()
                .all()
            )
        return [str(row) for row in rows]

    def _execute_backup(self, backup_id: str, _context: Any, _connection: Any) -> None:
        now = self._current_time()
        if self._backup_status(backup_id) != "creating":
            return
        gate = self._ensure_gate_row(now)
        if str(gate["state"]) == WRITE_GATE_OPEN:
            if not self._try_acquire_gate(backup_id, now):
                return  # a peer executor won the gate between read and write
            settle_elapsed = 0.0
        elif gate["backup_id"] == backup_id:
            # Adopt our own crashed execution: holding the execute lease
            # guarantees no live peer is executing this backup. The persisted
            # gate update time measures how long the gate has been quiescing.
            settle_elapsed = max(0.0, (now - _utc(gate["updated_at_utc"])).total_seconds())
        else:
            return  # the gate is held by another backup's execution
        try:
            self._await_write_drain(settle_elapsed)
            self._close_gate(backup_id)
            self._run_snapshot_sequence(backup_id)
        except _DrainTimeout as exc:
            # No snapshot reference exists yet: fail the first component.
            self._fail_backup_execution(backup_id, kind="postgres_snapshot", reason=str(exc)[:500])
        except _SnapshotStepFailed as exc:
            self._fail_backup_execution(backup_id, kind=exc.kind, reason=str(exc)[:500])
        except Exception as exc:
            kind = self._first_unsucceeded_component_kind(backup_id) or "postgres_snapshot"
            self._fail_backup_execution(
                backup_id, kind=kind, reason=f"snapshot sequence failed: {exc}"[:500]
            )
        else:
            self._audit_standalone(
                actor_id=EXECUTOR_ACTOR,
                resource_type="backup_execution.completed",
                resource_id=backup_id,
                result="succeeded",
                details={},
            )
        finally:
            # Whatever happened, the write gate must be released (Q7).
            self._release_gate(backup_id)

    def _run_snapshot_sequence(self, backup_id: str) -> None:
        # Components already succeeded by a crashed attempt are not re-run, so
        # resuming never overwrites a recorded snapshot reference.
        succeeded = self._succeeded_component_kinds(backup_id)
        if "postgres_snapshot" not in succeeded:
            try:
                reference = self._postgres_backup.snapshot()
                self._backup_service.complete_snapshot_component(
                    backup_id, kind="postgres_snapshot", reference=reference
                )
            except Exception as exc:
                raise _SnapshotStepFailed("postgres_snapshot", exc) from exc
        if "object_store_snapshot" not in succeeded:
            try:
                reference = self._object_snapshot.snapshot()
                self._backup_service.complete_snapshot_component(
                    backup_id, kind="object_store_snapshot", reference=reference
                )
            except Exception as exc:
                raise _SnapshotStepFailed("object_store_snapshot", exc) from exc
        if "object_manifest" not in succeeded:
            try:
                facts = self._object_manifest.collect_object_facts()
                self._backup_service.record_manifest_objects(backup_id, facts)
            except Exception as exc:
                raise _SnapshotStepFailed("object_manifest", exc) from exc

    def _fail_backup_execution(self, backup_id: str, *, kind: str, reason: str) -> None:
        now = self._current_time()
        try:
            self._backup_service.fail_component(backup_id, kind=kind, reason=reason)
        except (PlatformError, SQLAlchemyError):
            _logger.warning("could not mark backup %s failed", backup_id)
        # Provider artifacts produced before the failure are purged through a
        # durable cleanup target by the retention sweep (Q6/Q7).
        self._ensure_cleanup_target(backup_id, now=now, error=reason)
        self._audit_standalone(
            actor_id=EXECUTOR_ACTOR,
            resource_type="backup_execution.failed",
            resource_id=backup_id,
            result="failed",
            details={"reason": reason},
        )

    def _await_write_drain(self, settle_elapsed: float) -> None:
        """Wait until admitted writes drained and the gate has settled.

        The in-process tracker only counts writes admitted by THIS process; in
        a multi-process deployment the settle delay measured from when the gate
        entered `closing` is the protocol boundary that gives writes admitted
        by other processes time to finish before the gate becomes `closed`.
        """
        settle_deadline = time.monotonic() + max(0.0, self._gate_settle_seconds - settle_elapsed)
        drain_deadline = time.monotonic() + self._gate_drain_timeout_seconds
        while True:
            if self._write_tracker.in_flight == 0 and time.monotonic() >= settle_deadline:
                return
            if time.monotonic() >= drain_deadline:
                raise _DrainTimeout(
                    f"write gate drain timed out after {self._gate_drain_timeout_seconds}s"
                )
            time.sleep(self._drain_poll_seconds)

    # -- gate persistence helpers --------------------------------------

    @staticmethod
    def _gate_row(connection: Connection) -> dict[str, Any] | None:
        row = (
            connection.execute(
                select(backup_write_gate_table).where(backup_write_gate_table.c.id == 1)
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row is not None else None

    def _ensure_gate_row(self, now: datetime) -> dict[str, Any]:
        with self._engine.begin() as connection:
            row = self._gate_row(connection)
            if row is None:
                try:
                    connection.execute(
                        backup_write_gate_table.insert().values(
                            id=1, state=WRITE_GATE_OPEN, backup_id=None, updated_at_utc=now
                        )
                    )
                except IntegrityError:
                    pass  # a peer materialized the row first
                row = self._gate_row(connection)
        assert row is not None  # the materialized row always reads back
        return row

    def _try_acquire_gate(self, backup_id: str, now: datetime) -> bool:
        with self._engine.begin() as connection:
            updated = connection.execute(
                update(backup_write_gate_table)
                .where(
                    backup_write_gate_table.c.id == 1,
                    backup_write_gate_table.c.state == WRITE_GATE_OPEN,
                )
                .values(state=WRITE_GATE_CLOSING, backup_id=backup_id, updated_at_utc=now)
            ).rowcount
        return updated == 1

    def _close_gate(self, backup_id: str) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                update(backup_write_gate_table)
                .where(
                    backup_write_gate_table.c.id == 1,
                    backup_write_gate_table.c.backup_id == backup_id,
                    backup_write_gate_table.c.state == WRITE_GATE_CLOSING,
                )
                .values(state=WRITE_GATE_CLOSED, updated_at_utc=self._current_time())
            )

    def _release_gate(self, backup_id: str) -> None:
        now = self._current_time()
        with self._engine.begin() as connection:
            released = connection.execute(
                update(backup_write_gate_table)
                .where(
                    backup_write_gate_table.c.id == 1,
                    backup_write_gate_table.c.backup_id == backup_id,
                    backup_write_gate_table.c.state.in_((WRITE_GATE_CLOSING, WRITE_GATE_CLOSED)),
                )
                .values(state=WRITE_GATE_OPEN, backup_id=None, updated_at_utc=now)
            ).rowcount
            if released == 1:
                self._audit_row(
                    connection,
                    actor_id=EXECUTOR_ACTOR,
                    resource_type="backup_execution.gate_released",
                    resource_id=backup_id,
                    result="succeeded",
                    occurred_at=now,
                    details={},
                )

    def _recover_stale_gate(self) -> None:
        now = self._current_time()
        with self._engine.begin() as connection:
            gate = self._gate_row(connection)
            if gate is None or str(gate["state"]) == WRITE_GATE_OPEN:
                return
            backup_id = gate["backup_id"]
            stale = True
            if backup_id is not None:
                status = connection.execute(
                    select(backup_sets_table.c.status).where(backup_sets_table.c.id == backup_id)
                ).scalar_one_or_none()
                stale = status is None or str(status) != "creating"
            if not stale:
                return
            conditions = [
                backup_write_gate_table.c.id == 1,
                backup_write_gate_table.c.state.in_((WRITE_GATE_CLOSING, WRITE_GATE_CLOSED)),
            ]
            if backup_id is None:
                conditions.append(backup_write_gate_table.c.backup_id.is_(None))
            else:
                conditions.append(backup_write_gate_table.c.backup_id == backup_id)
            released = connection.execute(
                update(backup_write_gate_table)
                .where(*conditions)
                .values(state=WRITE_GATE_OPEN, backup_id=None, updated_at_utc=now)
            ).rowcount
            if released == 1:
                self._audit_row(
                    connection,
                    actor_id=EXECUTOR_ACTOR,
                    resource_type="backup_execution.gate_recovered",
                    resource_id=str(backup_id) if backup_id is not None else "backup_write_gate",
                    result="succeeded",
                    occurred_at=now,
                    details={"previous_state": str(gate["state"])},
                )

    # ------------------------------------------------------------------
    # Restore driving: advance until completed/failed/blocked
    # ------------------------------------------------------------------

    def _restore_task(self, owner: str) -> None:
        for restore_id in self._drivable_restore_ids():
            try:
                self._worker_runtime.run_task(
                    TASK_RESTORE, owner, partial(self._drive_restore, restore_id)
                )
            except (FenceViolation, LeaseUnavailable):
                continue
            except (PlatformError, SQLAlchemyError) as exc:
                _logger.warning("restore %s advance deferred: %s", restore_id, exc)

    def _drivable_restore_ids(self) -> list[str]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(restore_sessions_table.c.id)
                    .where(restore_sessions_table.c.status.in_(_DRIVABLE_RESTORE_STATUSES))
                    .order_by(restore_sessions_table.c.created_at_utc)
                )
                .scalars()
                .all()
            )
        return [str(row) for row in rows]

    def _drive_restore(self, restore_id: str, _context: Any, _connection: Any) -> None:
        # The persisted session is the progress record; a stop mid-loop (or a
        # crash) is resumed by the next tick. A blocked session keeps waiting
        # for the operator's repair retry instead of being driven further.
        for _ in range(_MAX_RESTORE_ADVANCES_PER_TICK):
            state = self._backup_service.advance_restore(restore_id)
            if str(state["status"]) not in _DRIVABLE_RESTORE_STATUSES:
                return

    # ------------------------------------------------------------------
    # Retention (Q6): protective-AND expiry with durable cleanup targets
    # ------------------------------------------------------------------

    def _retention_task(self, owner: str) -> None:
        self._worker_runtime.run_task(TASK_RETENTION, owner, partial(self._retention_sweep))

    def _retention_sweep(self, _context: Any, _connection: Any) -> None:
        now = self._current_time()
        counts = {"retried": 0, "candidates": 0, "purged": 0, "failed": 0, "skipped": 0}
        budget = self._retention_batch_limit
        # Only unfinished deletions are retried; their rows survive restarts.
        for target in self._due_cleanup_targets(now):
            if budget <= 0:
                break
            budget -= 1
            counts["retried"] += 1
            counts[self._process_cleanup_target(target, now=now)] += 1
        policy = self._policy_row()
        keep_last = int(policy["keep_last"]) if policy is not None else _DEFAULT_KEEP_LAST
        retention_days = (
            int(policy["retention_days"]) if policy is not None else _DEFAULT_RETENTION_DAYS
        )
        cutoff = now - timedelta(days=retention_days)
        restorable = self._restorable_backups()
        keep_ids = {str(row["id"]) for row in restorable[:keep_last]}
        remaining = len(restorable)
        candidates = [
            row
            for row in restorable
            if str(row["id"]) not in keep_ids and _utc(row["completed_at_utc"]) < cutoff
        ]
        for row in reversed(candidates):  # oldest first
            if budget <= 0:
                break
            counts["candidates"] += 1
            backup_id = str(row["id"])
            skip_reason = self._reference_protection(backup_id)
            if skip_reason is None and remaining <= 1:
                # Whatever the policy says, one complete restorable backup
                # always survives (Q6).
                skip_reason = "last_restorable"
            if skip_reason is not None:
                counts["skipped"] += 1
                self._audit_standalone(
                    actor_id=RETENTION_ACTOR,
                    resource_type="backup_retention.skipped",
                    resource_id=backup_id,
                    result="skipped",
                    details={"reason": skip_reason},
                )
                continue
            if self._has_unfinished_cleanup_target(backup_id):
                continue  # a previous attempt is waiting out its backoff
            budget -= 1
            target = self._create_cleanup_target(backup_id, now)
            outcome = self._process_cleanup_target(target, now=now)
            counts[outcome] += 1
            if outcome == "purged":
                remaining -= 1
        if counts["candidates"] or counts["retried"]:
            self._audit_standalone(
                actor_id=RETENTION_ACTOR,
                resource_type="backup_retention.evaluated",
                resource_id="backup_policy",
                result="succeeded",
                details={
                    "policy_version": int(policy["version"]) if policy is not None else None,
                    "keep_last": keep_last,
                    "retention_days": retention_days,
                    **counts,
                },
            )

    def _restorable_backups(self) -> list[Mapping[str, Any]]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(backup_sets_table.c.id, backup_sets_table.c.completed_at_utc)
                    .where(
                        backup_sets_table.c.status == "complete",
                        backup_sets_table.c.purged_at_utc.is_(None),
                    )
                    .order_by(
                        backup_sets_table.c.completed_at_utc.desc(),
                        backup_sets_table.c.id.desc(),
                    )
                )
                .mappings()
                .all()
            )
        return list(rows)

    def _reference_protection(self, backup_id: str) -> str | None:
        """Why this backup must be kept, or None when nothing references it."""
        with self._engine.connect() as connection:
            active_restore = connection.execute(
                select(restore_sessions_table.c.id).where(
                    restore_sessions_table.c.backup_id == backup_id,
                    restore_sessions_table.c.status.in_(_ACTIVE_RESTORE_STATUSES),
                )
            ).scalar_one_or_none()
            if active_restore is not None:
                return "active_restore"
            open_repair = connection.execute(
                select(repair_targets_table.c.id)
                .select_from(
                    repair_targets_table.join(
                        restore_sessions_table,
                        repair_targets_table.c.restore_id == restore_sessions_table.c.id,
                    )
                )
                .where(
                    restore_sessions_table.c.backup_id == backup_id,
                    repair_targets_table.c.status == "open",
                )
            ).scalar_one_or_none()
            if open_repair is not None:
                return "open_repair_target"
        return None

    def _due_cleanup_targets(self, now: datetime) -> list[dict[str, Any]]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(backup_cleanup_targets_table)
                    .where(
                        backup_cleanup_targets_table.c.status.in_(_UNFINISHED_CLEANUP_STATUSES),
                        or_(
                            backup_cleanup_targets_table.c.next_retry_at_utc.is_(None),
                            backup_cleanup_targets_table.c.next_retry_at_utc <= now,
                        ),
                    )
                    .order_by(backup_cleanup_targets_table.c.created_at_utc)
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    def _has_unfinished_cleanup_target(self, backup_id: str) -> bool:
        with self._engine.connect() as connection:
            existing = connection.execute(
                select(backup_cleanup_targets_table.c.id).where(
                    backup_cleanup_targets_table.c.backup_id == backup_id,
                    backup_cleanup_targets_table.c.status.in_(_UNFINISHED_CLEANUP_STATUSES),
                )
            ).scalar_one_or_none()
        return existing is not None

    def _create_cleanup_target(self, backup_id: str, now: datetime) -> dict[str, Any]:
        target_id = _new_id("cleanup")
        with self._engine.begin() as connection:
            connection.execute(
                backup_cleanup_targets_table.insert().values(
                    id=target_id,
                    backup_id=backup_id,
                    status="pending",
                    attempts=0,
                    created_at_utc=now,
                    updated_at_utc=now,
                )
            )
        return {"id": target_id, "backup_id": backup_id, "attempts": 0}

    def _ensure_cleanup_target(self, backup_id: str, *, now: datetime, error: str) -> None:
        with self._engine.begin() as connection:
            references = connection.execute(
                select(backup_components_table.c.id)
                .where(
                    backup_components_table.c.backup_id == backup_id,
                    backup_components_table.c.reference.is_not(None),
                )
                .limit(1)
            ).first()
            if references is None:
                return  # no provider artifact was produced; nothing to delete
            existing = connection.execute(
                select(backup_cleanup_targets_table.c.id).where(
                    backup_cleanup_targets_table.c.backup_id == backup_id,
                    backup_cleanup_targets_table.c.status.in_(_UNFINISHED_CLEANUP_STATUSES),
                )
            ).scalar_one_or_none()
            if existing is not None:
                return
            connection.execute(
                backup_cleanup_targets_table.insert().values(
                    id=_new_id("cleanup"),
                    backup_id=backup_id,
                    status="pending",
                    attempts=0,
                    last_error=error,
                    created_at_utc=now,
                    updated_at_utc=now,
                )
            )

    def _process_cleanup_target(self, target: Mapping[str, Any], *, now: datetime) -> str:
        """Run the provider delete protocol; returns 'purged' or 'failed'."""
        target_id = str(target["id"])
        backup_id = str(target["backup_id"])
        attempts = int(target["attempts"]) + 1
        with self._engine.begin() as connection:
            connection.execute(
                update(backup_cleanup_targets_table)
                .where(backup_cleanup_targets_table.c.id == target_id)
                .values(status="deleting", attempts=attempts, updated_at_utc=now)
            )
        try:
            for kind, reference in self._component_references(backup_id):
                if kind == "postgres_snapshot":
                    self._postgres_backup.delete(reference)
                elif kind == "object_store_snapshot":
                    self._object_snapshot.delete(reference)
                # object_manifest references are bookkeeping, not provider
                # artifacts; there is nothing to delete for them.
        except Exception as exc:
            error = str(exc)[:500]
            retry_at = now + _RETRY_BASE_DELAY * attempts
            with self._engine.begin() as connection:
                connection.execute(
                    update(backup_cleanup_targets_table)
                    .where(backup_cleanup_targets_table.c.id == target_id)
                    .values(
                        status="failed",
                        last_error=error,
                        next_retry_at_utc=retry_at,
                        updated_at_utc=now,
                    )
                )
                self._audit_row(
                    connection,
                    actor_id=RETENTION_ACTOR,
                    resource_type="backup_retention.failed",
                    resource_id=backup_id,
                    result="failed",
                    occurred_at=now,
                    details={
                        "error": error,
                        "attempts": attempts,
                        "next_retry_at_utc": retry_at.isoformat(),
                    },
                )
            return "failed"
        # The provider delete protocol finished first; only now mark the set.
        with self._engine.begin() as connection:
            connection.execute(
                update(backup_sets_table)
                .where(backup_sets_table.c.id == backup_id)
                .values(purged_at_utc=now)
            )
            connection.execute(
                update(backup_cleanup_targets_table)
                .where(backup_cleanup_targets_table.c.id == target_id)
                .values(status="done", updated_at_utc=now)
            )
            self._audit_row(
                connection,
                actor_id=RETENTION_ACTOR,
                resource_type="backup_retention.purged",
                resource_id=backup_id,
                result="succeeded",
                occurred_at=now,
                details={"attempts": attempts},
            )
        return "purged"

    def _component_references(self, backup_id: str) -> list[tuple[str, str]]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(
                        backup_components_table.c.kind, backup_components_table.c.reference
                    ).where(
                        backup_components_table.c.backup_id == backup_id,
                        backup_components_table.c.reference.is_not(None),
                    )
                )
                .mappings()
                .all()
            )
        # Deterministic deletion order: fact source first, then the object
        # snapshot; the manifest reference is bookkeeping and is skipped by the
        # caller. (Without an explicit order SQLite may return index order.)
        order = {"postgres_snapshot": 0, "object_store_snapshot": 1, "object_manifest": 2}
        pairs = [(str(row["kind"]), str(row["reference"])) for row in rows]
        pairs.sort(key=lambda pair: order.get(pair[0], 99))
        return pairs

    # ------------------------------------------------------------------
    # Shared lookups and audit
    # ------------------------------------------------------------------

    def _policy_row(self) -> dict[str, Any] | None:
        with self._engine.connect() as connection:
            row = (
                connection.execute(select(backup_policy_table).where(backup_policy_table.c.id == 1))
                .mappings()
                .one_or_none()
            )
        return dict(row) if row is not None else None

    def _backup_status(self, backup_id: str) -> str | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(backup_sets_table.c.status).where(backup_sets_table.c.id == backup_id)
            ).scalar_one_or_none()
        return str(row) if row is not None else None

    def _succeeded_component_kinds(self, backup_id: str) -> set[str]:
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(backup_components_table.c.kind).where(
                        backup_components_table.c.backup_id == backup_id,
                        backup_components_table.c.status == "succeeded",
                    )
                )
                .scalars()
                .all()
            )
        return {str(row) for row in rows}

    def _first_unsucceeded_component_kind(self, backup_id: str) -> str | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(backup_components_table.c.kind)
                .where(
                    backup_components_table.c.backup_id == backup_id,
                    backup_components_table.c.status != "succeeded",
                )
                .order_by(backup_components_table.c.created_at_utc, backup_components_table.c.kind)
            ).scalar_one_or_none()
        return str(row) if row is not None else None

    def _audit_standalone(
        self,
        *,
        actor_id: str,
        resource_type: str,
        resource_id: str,
        result: str,
        details: dict[str, Any],
    ) -> None:
        with self._engine.begin() as connection:
            self._audit_row(
                connection,
                actor_id=actor_id,
                resource_type=resource_type,
                resource_id=resource_id,
                result=result,
                occurred_at=self._current_time(),
                details=details,
            )

    @staticmethod
    def _audit_row(
        connection: Connection,
        *,
        actor_id: str,
        resource_type: str,
        resource_id: str,
        result: str,
        occurred_at: datetime,
        details: dict[str, Any] | None = None,
    ) -> None:
        context = current_context()
        connection.execute(
            platform_audit_table.insert().values(
                actor_id=actor_id,
                resource_type=resource_type,
                resource_id=resource_id,
                request_id=context.request_id if context is not None else "req_backup_worker",
                occurred_at_utc=occurred_at,
                result=result,
                details_json=details or {},
            )
        )


def main() -> None:
    """Resident entry point: run the backup maintenance loop."""
    settings = load_platform_settings_for_main()
    worker_runtime = create_worker_runtime(settings)
    try:
        worker = worker_runtime.runtime.resolve("backup_maintenance_worker")
        if not isinstance(worker, BackupMaintenanceWorker):
            raise RuntimeError("backup maintenance worker is not configured")
        worker.run_forever(
            owner=f"backup-worker:{_hostname()}",
            interval_seconds=settings.backup.schedule_interval_seconds,
        )
    finally:
        worker_runtime.close()


def load_platform_settings_for_main() -> PlatformSettings:
    from app.platform.config import load_platform_settings

    return load_platform_settings()


def _hostname() -> str:
    import socket

    return socket.gethostname() or "local"
