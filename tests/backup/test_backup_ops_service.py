"""Contract tests for the backup operations layer service."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select, update

from app.backup.ops_service import BackupOpsService
from app.backup.ports import ObjectFact
from app.backup.schema import (
    backup_metadata,
    backup_sets_table,
    backup_write_gate_table,
    maintenance_gate_table,
    ops_idempotency_commands_table,
    repair_targets_table,
    restore_sessions_table,
)
from app.backup.service import BackupRestoreService
from app.backup.write_gate import BackupWriteGateReader
from app.platform.database import core_metadata, platform_audit_table
from app.platform.errors import PlatformError

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)  # a Tuesday


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class _FakePostgresBackup:
    def snapshot(self) -> str:
        return "pgsnap-1"

    def restore(self, reference: str) -> None:
        del reference

    def delete(self, reference: str) -> None:
        del reference


class _FakeObjectSnapshot:
    def snapshot(self) -> str:
        return "objsnap-1"

    def restore(self, reference: str) -> None:
        del reference

    def delete(self, reference: str) -> None:
        del reference


class _Manifest:
    def collect_object_facts(self) -> list[ObjectFact]:
        return []


class _FactValidation:
    def __init__(self) -> None:
        self.expected: list[ObjectFact] = []
        self.actual: list[ObjectFact] = []

    def expected_object_facts(self) -> list[ObjectFact]:
        return list(self.expected)

    def actual_object_facts(self) -> list[ObjectFact]:
        return list(self.actual)


@pytest.fixture()
def env():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    core_metadata.create_all(engine)
    backup_metadata.create_all(engine)
    clock = _Clock(NOW)
    facts = _FactValidation()
    backup_service = BackupRestoreService(
        engine,
        postgres_backup=_FakePostgresBackup(),
        object_snapshot=_FakeObjectSnapshot(),
        object_manifest=_Manifest(),
        fact_validation=facts,
        now=clock,
    )
    gate_reader = BackupWriteGateReader(engine, cache_ttl_seconds=0)
    ops = BackupOpsService(
        engine,
        backup_service=backup_service,
        write_gate_reader=gate_reader,
        now=clock,
    )
    return {
        "engine": engine,
        "backup_service": backup_service,
        "ops": ops,
        "gate_reader": gate_reader,
        "clock": clock,
        "facts": facts,
    }


def _audit_rows(env) -> list[tuple[str, str, str, str]]:
    with env["engine"].connect() as connection:
        return [
            (str(r[0]), str(r[1]), str(r[2]), str(r[3]))
            for r in connection.execute(
                select(
                    platform_audit_table.c.actor_id,
                    platform_audit_table.c.resource_type,
                    platform_audit_table.c.resource_id,
                    platform_audit_table.c.result,
                )
            ).all()
        ]


def _complete_backup(env, backup_id: str) -> None:
    service = env["backup_service"]
    service.complete_snapshot_component(backup_id, kind="postgres_snapshot", reference="pg")
    service.complete_snapshot_component(backup_id, kind="object_store_snapshot", reference="obj")
    service.record_manifest_objects(backup_id, [])


def _set_write_gate(env, state: str) -> None:
    engine = env["engine"]
    with engine.begin() as connection:
        updated = connection.execute(
            update(backup_write_gate_table)
            .where(backup_write_gate_table.c.id == 1)
            .values(state=state, updated_at_utc=env["clock"]())
        ).rowcount
        if not updated:
            connection.execute(
                backup_write_gate_table.insert().values(
                    id=1, state=state, backup_id=None, updated_at_utc=env["clock"]()
                )
            )


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def test_policy_defaults_materialized_on_first_read(env):
    policy = env["ops"].get_policy()
    assert policy == {
        "enabled": False,
        "frequency": "daily",
        "local_time": "02:00",
        "weekdays": [],
        "timezone": "UTC",
        "keep_last": 7,
        "retention_days": 30,
        "version": 1,
        "last_scheduled_for_utc": None,
        "last_outcome": None,
        "updated_by": None,
        "updated_at_utc": NOW.isoformat(),
        "next_run_at": None,
    }
    # The materialized row is persisted and stable across reads.
    assert env["ops"].get_policy() == policy


def test_policy_patch_bumps_version_and_computes_next_run(env):
    ops = env["ops"]
    status, payload = ops.patch_policy(
        operator_user_id="op-1",
        expected_version=1,
        changes={"enabled": True, "timezone": "UTC", "local_time": "02:00"},
        idempotency_key="k1",
        request_hash="h1",
    )
    assert status == 200
    assert payload["version"] == 2
    assert payload["updated_by"] == "op-1"
    # now is Tuesday 12:00 UTC; daily 02:00 UTC already passed -> tomorrow.
    assert payload["next_run_at"] == "2026-08-26T02:00:00+00:00"
    assert ops.get_policy()["version"] == 2


def test_policy_patch_weekly_computes_next_matching_weekday(env):
    status, payload = env["ops"].patch_policy(
        operator_user_id="op-1",
        expected_version=1,
        changes={"enabled": True, "frequency": "weekly", "weekdays": [0], "timezone": "UTC"},
        idempotency_key="k1",
        request_hash="h1",
    )
    assert status == 200
    # Weekday 0 = Monday; from Tuesday 2026-08-25 the next Monday is 2026-08-31.
    assert payload["next_run_at"] == "2026-08-31T02:00:00+00:00"


def test_policy_patch_timezone_shifts_next_run(env):
    status, payload = env["ops"].patch_policy(
        operator_user_id="op-1",
        expected_version=1,
        changes={"enabled": True, "timezone": "Asia/Shanghai", "local_time": "02:00"},
        idempotency_key="k1",
        request_hash="h1",
    )
    assert status == 200
    # 02:00 at +08:00 the next day is 18:00 UTC of the current day.
    assert payload["next_run_at"] == "2026-08-25T18:00:00+00:00"


def test_policy_patch_rejects_invalid_values(env):
    ops = env["ops"]
    attempts = [
        ({"keep_last": 0}, "keep_last"),
        ({"retention_days": 0}, "retention_days"),
        ({"frequency": "weekly"}, "weekdays"),
        ({"weekdays": [7]}, "weekdays"),
        ({"local_time": "25:00"}, "local_time"),
        ({"timezone": "Not/AZone"}, "timezone"),
    ]
    for changes, field in attempts:
        with pytest.raises(PlatformError) as captured:
            ops.patch_policy(
                operator_user_id="op-1",
                expected_version=1,
                changes=changes,
                idempotency_key=f"k-{field}",
                request_hash=f"h-{field}",
            )
        assert captured.value.status_code == 422
        assert captured.value.code == "validation_error"
        assert captured.value.details["field"] == field
    # Nothing was persisted.
    assert env["ops"].get_policy()["version"] == 1


def test_policy_patch_version_conflict(env):
    with pytest.raises(PlatformError) as captured:
        env["ops"].patch_policy(
            operator_user_id="op-1",
            expected_version=99,
            changes={"keep_last": 3},
            idempotency_key="k1",
            request_hash="h1",
        )
    assert captured.value.code == "version_conflict"
    assert captured.value.status_code == 409
    assert captured.value.details["current_version"] == 1
    assert (
        "op-1",
        "backup_ops.update_backup_policy",
        "backup_policy",
        "rejected",
    ) in _audit_rows(env)


def test_policy_patch_replays_first_response_and_conflicts_on_mismatch(env):
    ops = env["ops"]
    first = ops.patch_policy(
        operator_user_id="op-1",
        expected_version=1,
        changes={"keep_last": 3},
        idempotency_key="k1",
        request_hash="h1",
    )
    replayed = ops.patch_policy(
        operator_user_id="op-1",
        expected_version=1,
        changes={"keep_last": 3},
        idempotency_key="k1",
        request_hash="h1",
    )
    assert replayed == first
    assert ops.get_policy()["version"] == 2
    with pytest.raises(PlatformError) as captured:
        ops.patch_policy(
            operator_user_id="op-1",
            expected_version=1,
            changes={"keep_last": 5},
            idempotency_key="k1",
            request_hash="h-other",
        )
    assert captured.value.code == "idempotency_key_conflict"
    assert captured.value.status_code == 409
    rows = _audit_rows(env)
    assert ("op-1", "backup_ops.update_backup_policy", "backup_policy", "replayed") in rows
    assert ("op-1", "backup_ops.update_backup_policy", "backup_policy", "rejected") in rows


# ---------------------------------------------------------------------------
# create_backup
# ---------------------------------------------------------------------------


def test_create_backup_accepts_and_replays(env):
    ops = env["ops"]
    status, payload = ops.create_backup(
        operator_user_id="op-1", idempotency_key="k1", request_hash="h1"
    )
    assert status == 202
    assert payload["status"] == "creating"
    replay_status, replay_payload = ops.create_backup(
        operator_user_id="op-1", idempotency_key="k1", request_hash="h1"
    )
    assert (replay_status, replay_payload) == (status, payload)
    # The replay did not create a second backup set.
    assert ops.list_backups(page=1, page_size=10)["total"] == 1
    rows = _audit_rows(env)
    assert ("op-1", "backup_ops.create_backup", payload["backup_id"], "succeeded") in rows
    assert ("op-1", "backup_ops.create_backup", payload["backup_id"], "replayed") in rows


def test_create_backup_rejects_concurrent_backup(env):
    ops = env["ops"]
    _, payload = ops.create_backup(operator_user_id="op-1", idempotency_key="k1", request_hash="h1")
    with pytest.raises(PlatformError) as captured:
        ops.create_backup(operator_user_id="op-1", idempotency_key="k2", request_hash="h2")
    assert captured.value.code == "backup_in_progress"
    assert captured.value.status_code == 409
    assert captured.value.details["active_backup_id"] == payload["backup_id"]
    assert ("op-1", "backup_ops.create_backup", "backup_sets", "rejected") in _audit_rows(env)


def test_create_backup_rejects_during_maintenance(env):
    _set_write_gate(env, "open")
    with env["engine"].begin() as connection:
        connection.execute(
            maintenance_gate_table.insert().values(
                id=1, reads_closed=1, restore_id="restore_x", updated_at_utc=NOW
            )
        )
    with pytest.raises(PlatformError) as captured:
        env["ops"].create_backup(operator_user_id="op-1", idempotency_key="k1", request_hash="h1")
    assert captured.value.code == "maintenance_mode"
    assert captured.value.status_code == 503


def test_create_backup_rejects_while_write_gate_closing(env):
    _set_write_gate(env, "closing")
    with pytest.raises(PlatformError) as captured:
        env["ops"].create_backup(operator_user_id="op-1", idempotency_key="k1", request_hash="h1")
    assert captured.value.code == "backup_in_progress"
    assert captured.value.status_code == 409


# ---------------------------------------------------------------------------
# start_restore
# ---------------------------------------------------------------------------


def test_start_restore_not_found_is_audited(env):
    with pytest.raises(PlatformError) as captured:
        env["ops"].start_restore(
            operator_user_id="op-1",
            backup_id="backup_missing",
            idempotency_key="k1",
            request_hash="h1",
        )
    assert captured.value.code == "backup_not_found"
    assert captured.value.status_code == 404
    assert (
        "op-1",
        "backup_ops.start_restore",
        "backup_missing",
        "rejected",
    ) in _audit_rows(env)


def test_start_restore_conflict_replay_and_blocked_guard(env):
    ops = env["ops"]
    _, backup = ops.create_backup(operator_user_id="op-1", idempotency_key="kb", request_hash="hb")
    backup_id = backup["backup_id"]
    _complete_backup(env, backup_id)

    status, payload = ops.start_restore(
        operator_user_id="op-1", backup_id=backup_id, idempotency_key="k1", request_hash="h1"
    )
    assert status == 202
    assert payload["backup_id"] == backup_id
    assert payload["status"] == "accepted"

    # Same key + fingerprint replays the first response without a new session.
    replay = ops.start_restore(
        operator_user_id="op-1", backup_id=backup_id, idempotency_key="k1", request_hash="h1"
    )
    assert replay == (status, payload)

    # A different key while the restore is active conflicts.
    with pytest.raises(PlatformError) as captured:
        ops.start_restore(
            operator_user_id="op-1", backup_id=backup_id, idempotency_key="k2", request_hash="h2"
        )
    assert captured.value.code == "restore_in_progress"
    assert captured.value.status_code == 409
    assert captured.value.details["active_restore_id"] == payload["restore_id"]

    # A blocked restore still holds the persisted mutex (unique index covers
    # blocked; the conflict must surface as 409, never as an integrity error).
    with env["engine"].begin() as connection:
        connection.execute(
            update(restore_sessions_table)
            .where(restore_sessions_table.c.id == payload["restore_id"])
            .values(status="blocked", updated_at_utc=env["clock"]())
        )
    with pytest.raises(PlatformError) as captured:
        ops.start_restore(
            operator_user_id="op-1", backup_id=backup_id, idempotency_key="k3", request_hash="h3"
        )
    assert captured.value.code == "restore_in_progress"
    assert captured.value.status_code == 409

    rows = _audit_rows(env)
    restore_id = payload["restore_id"]
    assert ("op-1", "backup_ops.start_restore", restore_id, "succeeded") in rows
    assert ("op-1", "backup_ops.start_restore", restore_id, "replayed") in rows
    assert ("op-1", "backup_ops.start_restore", backup_id, "rejected") in rows


# ---------------------------------------------------------------------------
# retry_repair_target
# ---------------------------------------------------------------------------


def _blocked_restore_with_repair(env) -> tuple[str, str]:
    """Drive a restore into blocked with one open repair target."""
    ops = env["ops"]
    service = env["backup_service"]
    _, backup = ops.create_backup(operator_user_id="op-1", idempotency_key="kb", request_hash="hb")
    backup_id = backup["backup_id"]
    _complete_backup(env, backup_id)
    env["facts"].expected = [ObjectFact("documents/d1/original", 100, "a" * 64)]
    env["facts"].actual = []
    _, restore = ops.start_restore(
        operator_user_id="op-1", backup_id=backup_id, idempotency_key="kr", request_hash="hr"
    )
    restore_id = restore["restore_id"]
    service.advance_restore(restore_id)  # postgres stage
    service.advance_restore(restore_id)  # object_store stage fails fact validation
    state = service.advance_restore(restore_id)  # session becomes blocked
    assert state["status"] == "blocked"
    repair = state["repair_targets"][0]
    with env["engine"].connect() as connection:
        target_id = connection.execute(
            select(repair_targets_table.c.id).where(repair_targets_table.c.restore_id == restore_id)
        ).scalar_one()
    assert repair["status"] == "open"
    return restore_id, str(target_id)


def test_retry_repair_target_resolves_after_facts_agree(env):
    ops = env["ops"]
    restore_id, target_id = _blocked_restore_with_repair(env)

    # Still mismatching: retried target stays open.
    status, payload = ops.retry_repair_target(
        operator_user_id="op-1",
        restore_id=restore_id,
        target_id=target_id,
        idempotency_key="k1",
        request_hash="h1",
    )
    assert (status, payload["status"]) == (202, "open")
    assert payload["target_id"] == target_id
    assert payload["restore_id"] == restore_id

    # Facts now agree: the retry resolves the target.
    env["facts"].actual = list(env["facts"].expected)
    status, payload = ops.retry_repair_target(
        operator_user_id="op-1",
        restore_id=restore_id,
        target_id=target_id,
        idempotency_key="k2",
        request_hash="h2",
    )
    assert (status, payload["status"]) == (202, "succeeded")
    assert (
        "op-1",
        "backup_ops.retry_repair_target",
        target_id,
        "succeeded",
    ) in _audit_rows(env)


def test_retry_repair_target_unknown_ids(env):
    with pytest.raises(PlatformError) as captured:
        env["ops"].retry_repair_target(
            operator_user_id="op-1",
            restore_id="restore_x",
            target_id="repair_x",
            idempotency_key="k1",
            request_hash="h1",
        )
    assert captured.value.code == "repair_target_not_found"
    assert captured.value.status_code == 404


# ---------------------------------------------------------------------------
# History reads
# ---------------------------------------------------------------------------


def test_list_backups_orders_descending_paginates_and_marks_purged(env):
    ops = env["ops"]
    clock = env["clock"]
    created = []
    for index in range(3):
        clock.value = NOW + timedelta(minutes=index)
        _, payload = ops.create_backup(
            operator_user_id="op-1",
            idempotency_key=f"kb-{index}",
            request_hash=f"hb-{index}",
        )
        created.append(payload["backup_id"])
        if index < 2:
            env["backup_service"].fail_component(
                payload["backup_id"], kind="postgres_snapshot", reason="boom"
            )
    listing = ops.list_backups(page=1, page_size=2)
    assert listing["total"] == 3
    assert [item["backup_id"] for item in listing["items"]] == [created[2], created[1]]
    page_two = ops.list_backups(page=2, page_size=2)
    assert [item["backup_id"] for item in page_two["items"]] == [created[0]]
    assert all(item["restorable"] is False for item in listing["items"])

    # Terminate the still-open set, then a complete backup is restorable until
    # purged.
    env["backup_service"].fail_component(created[2], kind="postgres_snapshot", reason="boom")
    clock.value = NOW + timedelta(minutes=5)
    _, payload = ops.create_backup(operator_user_id="op-1", idempotency_key="kc", request_hash="hc")
    _complete_backup(env, payload["backup_id"])
    detail = ops.get_backup(payload["backup_id"])
    assert detail["status"] == "complete"
    assert detail["restorable"] is True
    assert detail["purged_at_utc"] is None
    with env["engine"].begin() as connection:
        connection.execute(
            update(backup_sets_table)
            .where(backup_sets_table.c.id == payload["backup_id"])
            .values(purged_at_utc=clock())
        )
    detail = ops.get_backup(payload["backup_id"])
    assert detail["status"] == "complete"
    assert detail["restorable"] is False
    assert detail["purged_at_utc"] is not None


def test_get_backup_not_found(env):
    with pytest.raises(PlatformError) as captured:
        env["ops"].get_backup("backup_missing")
    assert captured.value.code == "backup_not_found"
    assert captured.value.status_code == 404


def test_list_restores_paginates_and_reports_current_stage(env):
    ops = env["ops"]
    _, backup = ops.create_backup(operator_user_id="op-1", idempotency_key="kb", request_hash="hb")
    backup_id = backup["backup_id"]
    _complete_backup(env, backup_id)
    _, first = ops.start_restore(
        operator_user_id="op-1", backup_id=backup_id, idempotency_key="k1", request_hash="h1"
    )
    listing = ops.list_restores(page=1, page_size=10)
    assert listing["total"] == 1
    item = listing["items"][0]
    assert item["restore_id"] == first["restore_id"]
    assert item["status"] == "accepted"
    assert item["current_stage"] == "postgres"

    detail = ops.get_restore(first["restore_id"])
    assert detail["reads_closed"] is True
    assert len(detail["stages"]) == 7
    assert detail["created_at_utc"] is not None


def test_get_restore_not_found(env):
    with pytest.raises(PlatformError) as captured:
        env["ops"].get_restore("restore_missing")
    assert captured.value.code == "restore_not_found"
    assert captured.value.status_code == 404


def test_get_restore_enriches_repair_target_ids(env):
    restore_id, target_id = _blocked_restore_with_repair(env)
    detail = env["ops"].get_restore(restore_id)
    assert detail["repair_targets"][0]["target_id"] == target_id


def test_pagination_validation(env):
    with pytest.raises(PlatformError):
        env["ops"].list_backups(page=0, page_size=10)
    with pytest.raises(PlatformError):
        env["ops"].list_restores(page=1, page_size=201)


def test_idempotency_records_never_store_plaintext_key(env):
    env["ops"].create_backup(
        operator_user_id="op-1", idempotency_key="secret-key", request_hash="h"
    )
    with env["engine"].connect() as connection:
        rows = connection.execute(select(ops_idempotency_commands_table)).mappings().all()
    assert len(rows) == 1
    row = rows[0]
    assert row["key_hash"] != "secret-key"
    assert len(str(row["key_hash"])) == 64
    assert "secret-key" not in str(dict(row))
