from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Column, Integer, MetaData, String, Table, select, update

from app.chat.ports import ChatGenerationRevocationPort
from app.identity.schema import identity_metadata, identity_revocation_command_table
from app.platform.app_factory import create_platform_app
from app.platform.config import PlatformConfigurationError, load_platform_settings
from app.platform.context import TaskContext
from app.platform.database import (
    SqlAlchemyDatabaseClock,
    SqlAlchemyLeaseStore,
    SqlAlchemyTransactionManager,
    core_metadata,
)
from app.platform.maintenance import create_maintenance_runtime
from app.platform.observability import (
    InMemoryObservabilityMetrics,
    ObservabilityReadRequest,
    SqlAlchemyObservabilityMetrics,
)
from app.platform.persistence import FenceViolation
from app.platform.runtime import PlatformRuntime, build_runtime
from app.platform.storage import S3ObjectStore
from app.platform.worker import create_worker_runtime
from app.usage.schema import usage_metadata


def settings():
    return load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
        }
    )


def test_platform_app_registers_only_v1_health_and_request_header() -> None:
    app = create_platform_app(settings())
    engine = app.state.platform_runtime.resolve("database_engine")
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    paths = set(app.openapi()["paths"])

    assert "/v1/health" in paths
    assert not any(path.startswith("/api") or path == "/chat" for path in paths)

    with TestClient(app) as client:
        response = client.get("/v1/health", headers={"X-Request-Id": "req_client"})

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["service"] == "core-platform"
    assert response.headers["x-request-id"].startswith("req_")
    assert response.headers["x-request-id"] != "req_client"


def test_platform_app_records_only_bounded_http_telemetry() -> None:
    configured = settings()
    runtime = build_runtime(configured)
    engine = runtime.resolve("database_engine")
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    app = create_platform_app(configured, runtime=runtime)

    with TestClient(app) as client:
        response = client.get("/v1/health?account_id=secret")

    assert response.status_code == 200
    metrics = runtime.resolve("observability_metrics")
    assert isinstance(metrics, SqlAlchemyObservabilityMetrics)
    read = metrics.read(ObservabilityReadRequest("retention-ops", "ops", "today"))
    assert read.api.sampled_request_weight >= 0
    runtime.close()


def test_platform_app_keeps_registered_metric_routes_and_groups_unknown_paths() -> None:
    configured = settings()
    metrics = InMemoryObservabilityMetrics(
        now=lambda: datetime(2026, 8, 5, tzinfo=UTC),
        success_sample_rate=1,
    )
    runtime = build_runtime(configured, adapters={"observability_metrics": metrics})
    engine = runtime.resolve("database_engine")
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    app = create_platform_app(configured, runtime=runtime)

    with TestClient(app) as client:
        health = client.get("/v1/health")
        validation = client.post("/v1/auth/login", json={})
        unknown = client.get("/v1/not-registered")

    assert health.status_code == 200
    assert validation.status_code == 422
    assert unknown.status_code == 404
    recorded_routes = {sample.route_template for sample in metrics.samples}
    assert "/v1/auth/login" in recorded_routes
    assert "other" in recorded_routes
    runtime.close()


def test_worker_uses_task_context_and_fence_token() -> None:
    runtime = build_runtime(settings())
    core_metadata.create_all(runtime.resolve("database_engine"))
    worker = create_worker_runtime(
        settings(),
        runtime=runtime,
        now=lambda: datetime(2026, 8, 5, tzinfo=UTC),
    )
    observed: list[TaskContext] = []

    def task(context: TaskContext, connection) -> str:
        del connection
        observed.append(context)
        return context.task_id

    assert worker.run_task("job-1", "worker-1", task) == "job-1"
    assert observed[0].request_id.startswith("req_")
    assert observed[0].task_id == "job-1"
    assert observed[0].fence_token == 1
    worker.close()
    runtime.close()


def test_worker_rolls_back_writes_when_the_task_loses_its_fence() -> None:
    runtime = build_runtime(settings())
    engine = runtime.resolve("database_engine")
    core_metadata.create_all(engine)
    domain_metadata = MetaData()
    documents = Table(
        "worker_fenced_documents",
        domain_metadata,
        Column("id", Integer, primary_key=True),
        Column("status", String(32), nullable=False),
    )
    domain_metadata.create_all(engine)
    worker = create_worker_runtime(settings(), runtime=runtime)

    def stale_task(context: TaskContext, connection) -> None:
        connection.execute(documents.insert().values(id=1, status="indexed"))
        connection.execute(
            update(core_metadata.tables["platform_lease"])
            .where(core_metadata.tables["platform_lease"].c.resource == context.task_id)
            .values(expires_at_utc=datetime(2000, 1, 1, tzinfo=UTC))
        )

    with pytest.raises(FenceViolation):
        worker.run_task("job-1", "worker-1", stale_task, ttl=timedelta(seconds=30))

    with engine.connect() as connection:
        assert connection.execute(select(documents)).all() == []
    worker.close()
    runtime.close()


def test_runtime_assembles_sql_adapters_from_one_configuration() -> None:
    runtime = build_runtime(settings())

    assert isinstance(runtime.resolve("database_clock"), SqlAlchemyDatabaseClock)
    assert isinstance(runtime.resolve("transaction_manager"), SqlAlchemyTransactionManager)
    assert isinstance(runtime.resolve("lease_store"), SqlAlchemyLeaseStore)
    assert isinstance(runtime.resolve("observability_metrics"), SqlAlchemyObservabilityMetrics)
    assert isinstance(runtime.resolve("object_store"), S3ObjectStore)

    runtime.close()


def test_runtime_binds_usage_submission_to_configured_embedding_adapter() -> None:
    class UsageBindableEmbedding:
        def __init__(self) -> None:
            self.submission = None

        def set_usage_submission(self, submission) -> None:
            self.submission = submission

    embedding = UsageBindableEmbedding()
    runtime = build_runtime(settings(), adapters={"indexing_embedding": embedding})
    try:
        assert embedding.submission is runtime.resolve("indexing_usage_submission")
    finally:
        runtime.close()


def test_default_runtime_persists_generation_revocation_commands() -> None:
    runtime = build_runtime(settings())
    engine = runtime.resolve("database_engine")
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    service = runtime.resolve("identity_access")
    user = service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    login = service.login(username="alice", password="Password1")

    assert isinstance(runtime.resolve("generation_revocation_port"), ChatGenerationRevocationPort)
    assert service.revoke_session(
        user_id=user["id"],
        session_id=login.session_id,
        reason="user_logout",
    )

    with engine.connect() as connection:
        command = connection.execute(identity_revocation_command_table.select()).mappings().one()
    assert command["user_id"] == user["id"]
    assert command["auth_session_id"] == login.session_id
    assert command["receipt_state"] == "accepted"
    assert str(command["receipt_reference"]).startswith("generation-outbox:")
    runtime.close()


def test_maintenance_runtime_uses_the_same_platform_runtime() -> None:
    runtime = build_runtime(settings())
    engine = runtime.resolve("database_engine")
    core_metadata.create_all(engine)
    # Outbox-owned tables are required by the notification retention scan; identity-owned
    # tables are required by the directory search backfill.
    from app.identity.schema import identity_metadata
    from app.outbox.schema import outbox_metadata

    identity_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    maintenance = create_maintenance_runtime(settings(), runtime=runtime)

    maintenance.run_retention_once()

    runtime.close()


def test_invalid_runtime_configuration_fails_before_app_creation() -> None:
    with pytest.raises(PlatformConfigurationError, match="^platform configuration is invalid$"):
        invalid = load_platform_settings(
            {
                "RAG_PLATFORM_PROFILE": "development",
                "RAG_DATABASE_URL": "not-a-url",
                "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
                "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
                "RAG_PROVIDER_NAME": "fake",
            }
        )
        create_platform_app(invalid)


def test_api_lifespan_fails_before_accepting_requests_when_database_is_unavailable() -> None:
    class BrokenEngine:
        def connect(self):
            raise OSError("database unavailable")

    runtime = PlatformRuntime(settings(), adapters={"database_engine": BrokenEngine()})
    app = create_platform_app(settings(), runtime=runtime)

    with pytest.raises(OSError, match="database unavailable"):
        with TestClient(app):
            pass
