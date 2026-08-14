"""V1 metrics/ops-jobs API contract tests."""

from __future__ import annotations

from fastapi.testclient import TestClient
from retention_helpers import (
    build_engine,
    build_identity_service,
    fixed_now,
    make_settings,
    provision_user,
)

from app.chat.schema import chat_metadata
from app.documents.schema import documents_metadata, documents_table, ingestion_jobs_table
from app.identity.schema import identity_metadata
from app.indexing.schema import indexing_metadata
from app.outbox.schema import outbox_metadata
from app.platform.app_factory import create_platform_app
from app.platform.database import core_metadata
from app.platform.runtime import build_runtime
from app.retention.schema import retention_metadata
from app.usage.schema import usage_metadata


def _runtime_and_app():
    configured = make_settings()
    engine = build_engine()
    for metadata in (
        core_metadata,
        identity_metadata,
        documents_metadata,
        usage_metadata,
        outbox_metadata,
        indexing_metadata,
        chat_metadata,
        retention_metadata,
    ):
        metadata.create_all(engine)
    identity = build_identity_service(engine)
    admin = provision_user(identity, "admin", "admin")
    ops = provision_user(identity, "ops", "ops")
    user = provision_user(identity, "alice", "user")
    runtime = build_runtime(
        configured, adapters={"database_engine": engine, "identity_access": identity}
    )
    app = create_platform_app(configured, runtime=runtime)
    return app, admin, ops, user


def test_metrics_endpoints_reject_non_ops_admin_principals() -> None:
    app, _admin, _ops, user = _runtime_and_app()
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {user['token']}"}
        dashboard = client.get("/v1/metrics/dashboard?window=7d", headers=headers)
        assert dashboard.status_code == 403
        assert dashboard.json()["error"]["code"] == "metrics_forbidden"
        operations = client.get("/v1/metrics/operations?window=7d", headers=headers)
        assert operations.status_code == 403
        assert operations.json()["error"]["code"] == "metrics_forbidden"
        jobs = client.get("/v1/ops/jobs?view=all", headers=headers)
        assert jobs.status_code == 403
        assert jobs.json()["error"]["code"] == "ops_jobs_forbidden"


def test_window_and_view_validation_use_stable_envelope() -> None:
    app, _admin, ops, _user = _runtime_and_app()
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {ops['token']}"}
        bad_window = client.get("/v1/metrics/dashboard?window=bogus", headers=headers)
        assert bad_window.status_code == 422
        assert bad_window.json()["error"]["code"] == "validation_error"
        assert bad_window.json()["error"]["details"]["field"] == "window"
        bad_view = client.get("/v1/ops/jobs?view=bogus", headers=headers)
        assert bad_view.status_code == 422
        assert bad_view.json()["error"]["code"] == "validation_error"
        assert bad_view.json()["error"]["details"]["field"] == "view"


def test_ops_dashboard_and_operations_shapes() -> None:
    app, _admin, ops, _user = _runtime_and_app()
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {ops['token']}"}
        dashboard = client.get("/v1/metrics/dashboard?window=7d", headers=headers)
        assert dashboard.status_code == 200
        body = dashboard.json()
        assert body["window"] == "7d"
        assert [pack["key"] for pack in body["packs"]] == [
            "tasks_health",
            "cost_sentinel",
            "ingestion_quality",
            "todo",
        ]
        for pack in body["packs"]:
            assert pack["title"]
            assert isinstance(pack["cards"], list)
        operations = client.get("/v1/metrics/operations?window=today", headers=headers)
        assert operations.status_code == 200
        assert len(operations.json()["cards"]) == 3


def test_admin_dashboard_is_read_only_and_admin_jobs_have_no_actions() -> None:
    app, admin, _ops, _user = _runtime_and_app()
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {admin['token']}"}
        dashboard = client.get("/v1/metrics/dashboard", headers=headers)
        assert dashboard.status_code == 200
        body = dashboard.json()
        assert body["window"] == "7d"
        assert [pack["key"] for pack in body["packs"]] == [
            "usage_overview",
            "asset_usage",
            "cost_share",
            "quality_quota",
        ]
        for pack in body["packs"]:
            for card in pack["cards"]:
                assert card["threshold"] is None
                assert card["link"] is None
        jobs = client.get("/v1/ops/jobs?view=all", headers=headers)
        assert jobs.status_code == 200
        for item in jobs.json()["items"]:
            assert item["allowed_actions"] == []


def test_ops_jobs_views_and_item_shape() -> None:
    app, _admin, ops, _user = _runtime_and_app()
    configured = make_settings()
    engine = build_engine()
    for metadata in (
        core_metadata,
        identity_metadata,
        documents_metadata,
        usage_metadata,
        outbox_metadata,
        indexing_metadata,
        chat_metadata,
        retention_metadata,
    ):
        metadata.create_all(engine)
    identity = build_identity_service(engine)
    ops_principal = provision_user(identity, "ops", "ops")
    runtime = build_runtime(
        configured, adapters={"database_engine": engine, "identity_access": identity}
    )
    from app.identity.schema import identity_space_table

    with engine.begin() as connection:
        connection.execute(
            identity_space_table.insert().values(
                id="public",
                kind="public",
                name="Public",
                owner_user_id=None,
                department_id=None,
                created_at_utc=fixed_now(),
            )
        )
        connection.execute(
            documents_table.insert().values(
                id="doc_1",
                space_id="public",
                lifecycle_status="active",
                version=1,
                name="Broken",
                normalized_name="broken",
                uploaded_at_utc=fixed_now(),
                created_at_utc=fixed_now(),
                updated_at_utc=fixed_now(),
            )
        )
        connection.execute(
            ingestion_jobs_table.insert().values(
                id="job_1",
                document_id="doc_1",
                operation="initial",
                state="failed",
                version=1,
                replay_generation=0,
                failure_reason="parse",
                degradations_json=[],
                processing_summary_json={},
                ocr_low_confidence=False,
                notification_event_ids_json=[],
                created_by_user_id=str(ops_principal["record"]["id"]),
                quota_role_snapshot="ops",
                created_at_utc=fixed_now(),
                updated_at_utc=fixed_now(),
            )
        )
    app = create_platform_app(configured, runtime=runtime)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {ops_principal['token']}"}
        all_jobs = client.get("/v1/ops/jobs?view=all", headers=headers)
        assert all_jobs.status_code == 200
        items = all_jobs.json()["items"]
        assert len(items) >= 1
        failed = next(item for item in items if item["job_id"] == "job_1")
        assert failed["task_type"] == "ingestion"
        assert failed["document_name"] == "Broken"
        assert failed["state"] == "failed"
        assert isinstance(failed["stale"], bool)
        assert isinstance(failed["wait_seconds"], int)
        replayable = client.get("/v1/ops/jobs?view=replayable", headers=headers)
        assert replayable.status_code == 200
        assert any(item["job_id"] == "job_1" for item in replayable.json()["items"])
        active = client.get("/v1/ops/jobs?view=active", headers=headers)
        assert active.status_code == 200
        assert not any(item["job_id"] == "job_1" for item in active.json()["items"])
