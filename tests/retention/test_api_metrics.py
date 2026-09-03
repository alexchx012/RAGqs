"""V1 metrics/ops-jobs API contract tests."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from retention_helpers import (
    build_engine,
    build_identity_service,
    fixed_now,
    make_settings,
    provision_user,
)
from sqlalchemy import select

from app.chat.schema import chat_metadata
from app.documents.schema import documents_metadata, documents_table, ingestion_jobs_table
from app.identity.schema import identity_metadata
from app.indexing.schema import indexing_metadata
from app.outbox.schema import outbox_metadata
from app.platform.app_factory import create_platform_app
from app.platform.database import core_metadata, platform_audit_table
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
        bad_expand = client.get("/v1/metrics/dashboard?expand=unknown", headers=headers)
        assert bad_expand.status_code == 422
        assert bad_expand.json()["error"]["details"]["field"] == "expand"


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


def test_admin_document_drilldown_rejects_non_management_principals() -> None:
    app, admin, _ops, user = _runtime_and_app()
    with TestClient(app) as client:
        response = client.get(
            f"/v1/admin/users/{admin['record']['id']}/documents",
            headers={"Authorization": f"Bearer {user['token']}"},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden_target"


def test_admin_document_drilldown_exposes_paginated_read_models_to_ops_and_admin() -> None:
    app, admin, ops, _user = _runtime_and_app()
    admin_headers = {"Authorization": f"Bearer {admin['token']}"}
    ops_headers = {"Authorization": f"Bearer {ops['token']}"}
    with TestClient(app) as client:
        personal = client.get(
            f"/v1/admin/users/{admin['record']['id']}/documents?page=1&page_size=20",
            headers=admin_headers,
        )
        department = client.post(
            "/v1/admin/departments",
            headers={**admin_headers, "Idempotency-Key": "drilldown-department"},
            json={"name": "Drilldown"},
        )
        department_documents = client.get(
            f"/v1/admin/departments/{department.json()['id']}/documents",
            headers=ops_headers,
        )

    assert personal.status_code == 200
    assert personal.json() == {"items": [], "total": 0, "page": 1, "page_size": 20}
    assert department.status_code == 201
    assert department_documents.status_code == 200
    assert department_documents.json() == {"items": [], "total": 0, "page": 1, "page_size": 50}


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

    now = fixed_now()
    with engine.begin() as connection:
        connection.execute(
            identity_space_table.insert().values(
                id="public",
                kind="public",
                name="Public",
                owner_user_id=None,
                department_id=None,
                created_at_utc=now,
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
                uploaded_at_utc=now,
                created_at_utc=now,
                updated_at_utc=now,
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
                created_at_utc=now,
                updated_at_utc=now,
            )
        )
        connection.execute(
            ingestion_jobs_table.insert().values(
                id="job_2",
                document_id="doc_1",
                operation="initial",
                state="pending",
                version=1,
                replay_generation=0,
                degradations_json=[],
                processing_summary_json={},
                ocr_low_confidence=False,
                notification_event_ids_json=[],
                created_by_user_id=str(ops_principal["record"]["id"]),
                quota_role_snapshot="ops",
                created_at_utc=now - timedelta(minutes=3),
                updated_at_utc=now - timedelta(minutes=3),
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
        pending = next(item for item in active.json()["items"] if item["job_id"] == "job_2")
        assert isinstance(pending["wait_seconds"], int)


def _standalone_app():
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
    runtime = build_runtime(
        configured, adapters={"database_engine": engine, "identity_access": identity}
    )
    return configured, engine, runtime, identity


def test_operations_returns_exactly_three_designed_cards() -> None:
    configured, engine, runtime, identity = _standalone_app()
    ops = provision_user(identity, "ops", "ops")
    app = create_platform_app(configured, runtime=runtime)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {ops['token']}"}
        for window in ("today", "7d", "30d"):
            response = client.get(f"/v1/metrics/operations?window={window}", headers=headers)
            assert response.status_code == 200
            cards = response.json()["cards"]
            assert [card["key"] for card in cards] == [
                "cache_hit_rate",
                "ocr_confidence_dist",
                "graph_basic_split",
            ]
            assert {card["key"]: card["kind"] for card in cards} == {
                "cache_hit_rate": "stat",
                "ocr_confidence_dist": "distribution",
                "graph_basic_split": "distribution",
            }


def test_admin_document_drilldown_writes_audit_records() -> None:
    configured, engine, runtime, identity = _standalone_app()
    admin = provision_user(identity, "admin", "admin")
    ops = provision_user(identity, "ops", "ops")
    app = create_platform_app(configured, runtime=runtime)
    with TestClient(app) as client:
        admin_headers = {"Authorization": f"Bearer {admin['token']}"}
        ops_headers = {"Authorization": f"Bearer {ops['token']}"}
        department = client.post(
            "/v1/admin/departments",
            headers={**admin_headers, "Idempotency-Key": "audit-drilldown"},
            json={"name": "Audit"},
        )
        assert department.status_code == 201
        department_id = department.json()["id"]
        for _ in range(2):
            department_documents = client.get(
                f"/v1/admin/departments/{department_id}/documents", headers=ops_headers
            )
            assert department_documents.status_code == 200
            assert department_documents.json() == {
                "items": [],
                "total": 0,
                "page": 1,
                "page_size": 50,
            }
        personal = client.get(
            f"/v1/admin/users/{ops['record']['id']}/documents", headers=admin_headers
        )
        assert personal.status_code == 200
    with engine.connect() as connection:
        department_rows = connection.execute(
            select(platform_audit_table.c.actor_id, platform_audit_table.c.resource_id).where(
                platform_audit_table.c.resource_type == "documents.department_library_view"
            )
        ).all()
        personal_rows = connection.execute(
            select(platform_audit_table.c.actor_id, platform_audit_table.c.resource_id).where(
                platform_audit_table.c.resource_type == "documents.personal_library_view"
            )
        ).all()
    assert department_rows == [(str(ops["record"]["id"]), f"department:{department_id}")] * 2
    assert personal_rows == [(str(admin["record"]["id"]), f"personal:{ops['record']['id']}")]


def test_quota_consumption_dashboard_view_writes_audit_records() -> None:
    configured, engine, runtime, identity = _standalone_app()
    admin = provision_user(identity, "admin", "admin")
    ops = provision_user(identity, "ops", "ops")
    user = provision_user(identity, "alice", "user")
    app = create_platform_app(configured, runtime=runtime)
    with TestClient(app) as client:
        for token in (ops["token"], admin["token"]):
            response = client.get(
                "/v1/metrics/dashboard?window=7d",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 200
        # 普通用户被 403 拒绝，路径不产生查看审计。
        forbidden = client.get(
            "/v1/metrics/dashboard?window=7d", headers={"Authorization": f"Bearer {user['token']}"}
        )
        assert forbidden.status_code == 403
    with engine.connect() as connection:
        rows = connection.execute(
            select(
                platform_audit_table.c.actor_id,
                platform_audit_table.c.resource_type,
                platform_audit_table.c.resource_id,
                platform_audit_table.c.result,
            ).where(platform_audit_table.c.resource_type == "retention.quota_consumption_view")
        ).all()
    assert rows == [
        (str(ops["record"]["id"]), "retention.quota_consumption_view", "7d", "succeeded"),
        (str(admin["record"]["id"]), "retention.quota_consumption_view", "7d", "succeeded"),
    ]
