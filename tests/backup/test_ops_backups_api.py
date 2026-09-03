"""V1 ops backup/restore API contract (Q1/Q2/Q8/Q9)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.backup.schema import backup_metadata, repair_targets_table
from app.identity.service import AuthPrincipal
from app.platform.app_factory import create_platform_app
from app.platform.database import platform_audit_table
from app.platform.runtime import build_runtime
from tests._support import build_engine, build_identity_service, make_settings, provision_user

REPAIR_NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)


def make_app(*, role: str):
    configured = make_settings()
    engine = build_engine()
    backup_metadata.create_all(engine)
    identity = build_identity_service(engine)
    department_id = None
    if role == "minister":
        # A minister must belong to an active department.
        admin = AuthPrincipal(
            user_id="root",
            auth_session_id="s-admin",
            username="root",
            role="admin",
            department_id=None,
        )
        department = identity.create_department(
            actor=admin, name="Finance", idempotency_key="dept-1"
        )
        department_id = str(department["id"])
    if department_id is None:
        actor = provision_user(identity, username="caller", role=role)
    else:
        user = identity.provision_user(
            username="caller",
            password="Password1",
            real_name="Caller",
            display_name="Caller",
            role=role,  # type: ignore[arg-type]
            department_id=department_id,
        )
        actor = str(user["id"])
    runtime = build_runtime(
        configured,
        adapters={"database_engine": engine, "identity_access": identity},
    )
    app = create_platform_app(configured, runtime=runtime)
    login = identity.login(username="caller", password="Password1")
    return app, login.access_token, engine, actor, runtime


def _complete_backup(runtime, backup_id: str) -> None:
    service = runtime.resolve("backup_restore_service")
    service.complete_snapshot_component(backup_id, kind="postgres_snapshot", reference="pg")
    service.complete_snapshot_component(backup_id, kind="object_store_snapshot", reference="obj")
    service.record_manifest_objects(backup_id, [])


def _insert_repair_target(engine, *, target_id: str, restore_id: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            repair_targets_table.insert().values(
                id=target_id,
                restore_id=restore_id,
                stage="object_store",
                resource_id="documents/d1/original",
                failure_classification="object_missing",
                detail="object missing from object storage",
                status="open",
                attempts=0,
                next_retry_at_utc=None,
                resolved_at_utc=None,
                created_at_utc=REPAIR_NOW,
                updated_at_utc=REPAIR_NOW,
            )
        )


def _audit_denials(engine, action: str) -> list[tuple[str, str]]:
    with engine.connect() as connection:
        return [
            (str(r[0]), str(r[1]))
            for r in connection.execute(
                select(
                    platform_audit_table.c.actor_id,
                    platform_audit_table.c.result,
                ).where(platform_audit_table.c.resource_type == f"backup_ops.{action}")
            ).all()
        ]


def _create_backup(client, token: str, key: str = "kb-1"):
    return client.post(
        "/v1/ops/backups",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
    )


# ---------------------------------------------------------------------------
# Nine-endpoint contract
# ---------------------------------------------------------------------------


def test_backup_policy_get_and_patch_contract() -> None:
    app, token, _, _, _ = make_app(role="ops")
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {token}"}
        read = client.get("/v1/ops/backup-policy", headers=headers)
        assert read.status_code == 200
        assert read.json()["version"] == 1
        assert read.json()["enabled"] is False
        assert read.json()["next_run_at"] is None

        patch_body = {
            "expected_version": 1,
            "enabled": True,
            "frequency": "daily",
            "local_time": "04:00",
            "timezone": "UTC",
            "keep_last": 3,
            "retention_days": 10,
        }
        patched = client.patch(
            "/v1/ops/backup-policy",
            headers={**headers, "Idempotency-Key": "kp-1"},
            json=patch_body,
        )
        assert patched.status_code == 200
        assert patched.json()["version"] == 2
        assert patched.json()["keep_last"] == 3
        assert patched.json()["next_run_at"] is not None

        # Same key + same request replays the first response (version stays 2).
        replayed = client.patch(
            "/v1/ops/backup-policy",
            headers={**headers, "Idempotency-Key": "kp-1"},
            json=patch_body,
        )
        assert replayed.status_code == 200
        assert replayed.json() == patched.json()

        # Same key + different request conflicts.
        conflict = client.patch(
            "/v1/ops/backup-policy",
            headers={**headers, "Idempotency-Key": "kp-1"},
            json={**patch_body, "keep_last": 5},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_key_conflict"

        # A stale version with a new key conflicts on the version.
        stale = client.patch(
            "/v1/ops/backup-policy",
            headers={**headers, "Idempotency-Key": "kp-2"},
            json={**patch_body, "expected_version": 1},
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "version_conflict"
        assert client.get("/v1/ops/backup-policy", headers=headers).json()["version"] == 2


def test_backup_create_list_get_contract() -> None:
    app, token, _, _, _ = make_app(role="ops")
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {token}"}
        created = _create_backup(client, token)
        assert created.status_code == 202
        assert set(created.json()) == {"backup_id", "status"}
        assert created.json()["status"] == "creating"
        backup_id = created.json()["backup_id"]

        replayed = _create_backup(client, token)
        assert replayed.status_code == 202
        assert replayed.json() == created.json()

        listing = client.get("/v1/ops/backups", headers=headers, params={"page_size": 10})
        assert listing.status_code == 200
        assert listing.json()["total"] == 1
        item = listing.json()["items"][0]
        assert item["backup_id"] == backup_id
        assert item["restorable"] is False

        detail = client.get(f"/v1/ops/backups/{backup_id}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["backup_id"] == backup_id
        assert len(detail.json()["components"]) == 3

        missing = client.get("/v1/ops/backups/backup_missing", headers=headers)
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "backup_not_found"


def test_restore_flow_and_repair_retry_contract() -> None:
    app, token, engine, _, runtime = make_app(role="ops")
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {token}"}
        backup_id = _create_backup(client, token).json()["backup_id"]
        _complete_backup(runtime, backup_id)

        started = client.post(
            "/v1/ops/restores",
            headers={**headers, "Idempotency-Key": "kr-1"},
            json={"backup_id": backup_id},
        )
        assert started.status_code == 202
        assert set(started.json()) == {"restore_id", "backup_id", "status"}
        assert started.json()["status"] == "accepted"
        restore_id = started.json()["restore_id"]

        replayed = client.post(
            "/v1/ops/restores",
            headers={**headers, "Idempotency-Key": "kr-1"},
            json={"backup_id": backup_id},
        )
        assert replayed.status_code == 202
        assert replayed.json() == started.json()

        listing = client.get("/v1/ops/restores", headers=headers)
        assert listing.status_code == 200
        assert listing.json()["total"] == 1
        assert listing.json()["items"][0]["current_stage"] == "postgres"

        detail = client.get(f"/v1/ops/restores/{restore_id}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["reads_closed"] is True
        assert len(detail.json()["stages"]) == 7

        _insert_repair_target(engine, target_id="repair_1", restore_id=restore_id)
        detail = client.get(f"/v1/ops/restores/{restore_id}", headers=headers)
        assert detail.json()["repair_targets"][0]["target_id"] == "repair_1"

        retried = client.post(
            f"/v1/ops/restores/{restore_id}/repair-targets/repair_1/retry",
            headers={**headers, "Idempotency-Key": "kt-1"},
        )
        assert retried.status_code == 202
        assert retried.json() == {
            "target_id": "repair_1",
            "restore_id": restore_id,
            "status": "open",
        }

        missing = client.post(
            f"/v1/ops/restores/{restore_id}/repair-targets/repair_missing/retry",
            headers={**headers, "Idempotency-Key": "kt-2"},
        )
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "repair_target_not_found"


# ---------------------------------------------------------------------------
# Q2: strict ops-only, audited denials
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["admin", "user", "minister"])
def test_non_ops_roles_are_denied_and_audited(role: str) -> None:
    app, token, engine, actor, _ = make_app(role=role)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {token}"}
        write = _create_backup(client, token)
        read = client.get("/v1/ops/backups", headers=headers)
        policy = client.get("/v1/ops/backup-policy", headers=headers)
        patch = client.patch(
            "/v1/ops/backup-policy",
            headers={**headers, "Idempotency-Key": "kp-1"},
            json={"expected_version": 1, "keep_last": 3},
        )

    assert write.status_code == 403
    assert read.status_code == 403
    assert policy.status_code == 403
    assert patch.status_code == 403
    assert write.json()["error"]["code"] == "forbidden"
    # No partial state was created by the denied write.
    with engine.connect() as connection:
        backups = connection.execute(select(backup_metadata.tables["backup_sets"])).all()
    assert backups == []
    assert (actor, "denied") in _audit_denials(engine, "create_backup")
    assert (actor, "denied") in _audit_denials(engine, "list_backups")
    assert (actor, "denied") in _audit_denials(engine, "update_backup_policy")


# ---------------------------------------------------------------------------
# Q8: Idempotency-Key required on all four write commands
# ---------------------------------------------------------------------------


def test_write_commands_require_idempotency_key() -> None:
    app, token, engine, _, runtime = make_app(role="ops")
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {token}"}
        backup_id = _create_backup(client, token).json()["backup_id"]
        _complete_backup(runtime, backup_id)
        restore_id = client.post(
            "/v1/ops/restores",
            headers={**headers, "Idempotency-Key": "kr-1"},
            json={"backup_id": backup_id},
        ).json()["restore_id"]
        _insert_repair_target(engine, target_id="repair_1", restore_id=restore_id)

        responses = [
            client.post("/v1/ops/backups", headers=headers),
            client.post("/v1/ops/restores", headers=headers, json={"backup_id": backup_id}),
            client.post(
                f"/v1/ops/restores/{restore_id}/repair-targets/repair_1/retry",
                headers=headers,
            ),
            client.patch(
                "/v1/ops/backup-policy",
                headers=headers,
                json={"expected_version": 1, "keep_last": 3},
            ),
        ]

    assert [r.status_code for r in responses] == [422, 422, 422, 422]
    assert all(r.json()["error"]["code"] == "validation_error" for r in responses)


def test_pagination_query_validation() -> None:
    app, token, _, _, _ = make_app(role="ops")
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {token}"}
        bad_page = client.get("/v1/ops/backups", headers=headers, params={"page": 0})
        bad_size = client.get("/v1/ops/restores", headers=headers, params={"page_size": 201})
        ok = client.get("/v1/ops/backups", headers=headers, params={"page": 1, "page_size": 200})

    assert bad_page.status_code == 422
    assert bad_size.status_code == 422
    assert ok.status_code == 200
    assert ok.json() == {"items": [], "total": 0, "page": 1, "page_size": 200}


# ---------------------------------------------------------------------------
# Q9: restore-period whitelist
# ---------------------------------------------------------------------------


def test_restore_period_whitelist() -> None:
    app, token, engine, _, runtime = make_app(role="ops")
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {token}"}
        backup_id = _create_backup(client, token).json()["backup_id"]
        _complete_backup(runtime, backup_id)
        restore_id = client.post(
            "/v1/ops/restores",
            headers={**headers, "Idempotency-Key": "kr-1"},
            json={"backup_id": backup_id},
        ).json()["restore_id"]
        _insert_repair_target(engine, target_id="repair_1", restore_id=restore_id)

        # Allowed: every backup/restore GET and the repair-target retry.
        assert client.get("/v1/ops/backups", headers=headers).status_code == 200
        assert client.get(f"/v1/ops/backups/{backup_id}", headers=headers).status_code == 200
        assert client.get("/v1/ops/restores", headers=headers).status_code == 200
        assert client.get(f"/v1/ops/restores/{restore_id}", headers=headers).status_code == 200
        assert client.get("/v1/ops/backup-policy", headers=headers).status_code == 200
        retry = client.post(
            f"/v1/ops/restores/{restore_id}/repair-targets/repair_1/retry",
            headers={**headers, "Idempotency-Key": "kt-1"},
        )
        assert retry.status_code == 202

        # Rejected: creating a backup and patching the policy are 503
        # maintenance_mode; a second restore is 409 restore_in_progress.
        create = _create_backup(client, token, key="kb-2")
        assert create.status_code == 503
        assert create.json()["error"]["code"] == "maintenance_mode"
        patch = client.patch(
            "/v1/ops/backup-policy",
            headers={**headers, "Idempotency-Key": "kp-9"},
            json={"expected_version": 1, "keep_last": 3},
        )
        assert patch.status_code == 503
        assert patch.json()["error"]["code"] == "maintenance_mode"
        second = client.post(
            "/v1/ops/restores",
            headers={**headers, "Idempotency-Key": "kr-2"},
            json={"backup_id": backup_id},
        )
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "restore_in_progress"
