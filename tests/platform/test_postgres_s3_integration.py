from __future__ import annotations

import os
import time
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    delete,
    inspect,
    select,
)

from alembic import command
from alembic.config import Config
from app.platform.app_factory import create_platform_app
from app.platform.config import load_platform_settings
from app.platform.database import CORE_TABLE_NAMES, SqlAlchemyLeaseStore, platform_lease_table
from app.platform.persistence import FenceViolation
from app.platform.runtime import build_runtime
from app.platform.storage import ObjectMetadata, build_object_store
from app.platform.worker import create_worker_runtime

pytestmark = pytest.mark.integration


def test_integration_environment_uses_non_runtime_prefix(monkeypatch) -> None:
    monkeypatch.setenv("RAGQS_TEST_POSTGRES_URL", "postgresql+psycopg://app:secret@db/rag")
    monkeypatch.setenv("RAGQS_TEST_S3_ENDPOINT", "http://localhost:9000")
    monkeypatch.setenv("RAGQS_TEST_S3_ACCESS_KEY", "minioadmin")
    monkeypatch.setenv("RAGQS_TEST_S3_SECRET_KEY", "minioadmin")
    monkeypatch.setenv("RAGQS_TEST_S3_BUCKET", "rag-test")

    environment = _integration_environment()

    assert environment is not None
    assert environment["RAG_DATABASE_URL"] == "postgresql+psycopg://app:secret@db/rag"


def _integration_environment() -> dict[str, str] | None:
    database_url = os.environ.get("RAGQS_TEST_POSTGRES_URL")
    endpoint = os.environ.get("RAGQS_TEST_S3_ENDPOINT")
    access_key = os.environ.get("RAGQS_TEST_S3_ACCESS_KEY")
    secret_key = os.environ.get("RAGQS_TEST_S3_SECRET_KEY")
    bucket = os.environ.get("RAGQS_TEST_S3_BUCKET")
    if not all((database_url, endpoint, access_key, secret_key, bucket)):
        return None
    return {
        "RAG_PLATFORM_PROFILE": "development",
        "RAG_DATABASE_URL": database_url,
        "RAG_OBJECT_STORAGE_ENDPOINT": endpoint,
        "RAG_OBJECT_STORAGE_ACCESS_KEY": access_key,
        "RAG_OBJECT_STORAGE_SECRET_KEY": secret_key,
        "RAG_OBJECT_STORAGE_BUCKET": bucket,
        "RAG_PROVIDER_NAME": "fake",
    }


def _alembic_config(database_url: str) -> Config:
    config = Config(str(Path("alembic.ini")))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_empty_postgres_and_s3_support_api_and_worker_startup() -> None:
    environment = _integration_environment()
    if environment is None:
        pytest.skip("PostgreSQL and S3 integration environment is not configured")
    pytest.importorskip("boto3")

    command.upgrade(_alembic_config(environment["RAG_DATABASE_URL"]), "head")
    engine = create_engine(environment["RAG_DATABASE_URL"])
    assert CORE_TABLE_NAMES <= set(inspect(engine).get_table_names())
    engine.dispose()

    settings = load_platform_settings(environment)
    runtime = build_runtime(settings)
    app = create_platform_app(settings, runtime=runtime)
    with TestClient(app) as client:
        assert client.get("/v1/health").status_code == 200

    worker = create_worker_runtime(settings, runtime=runtime)
    task_id = f"integration-job:{uuid4()}"
    assert (
        worker.run_task(task_id, "worker-1", lambda context, connection: context.task_id) == task_id
    )

    store = build_object_store(
        endpoint=environment["RAG_OBJECT_STORAGE_ENDPOINT"],
        bucket=environment["RAG_OBJECT_STORAGE_BUCKET"],
        access_key=environment["RAG_OBJECT_STORAGE_ACCESS_KEY"],
        secret_key=environment["RAG_OBJECT_STORAGE_SECRET_KEY"],
    )
    key = "integration/core-platform.txt"
    metadata = ObjectMetadata("text/plain", 4, "integration-digest")
    store.put(key, b"test", metadata)
    assert store.get(key) == (b"test", metadata)
    store.delete(key)
    store.close()
    runtime.close()


def test_postgres_initial_lease_acquisition_returns_fence_token() -> None:
    environment = _integration_environment()
    if environment is None:
        pytest.skip("PostgreSQL and S3 integration environment is not configured")

    command.upgrade(_alembic_config(environment["RAG_DATABASE_URL"]), "head")
    engine = create_engine(environment["RAG_DATABASE_URL"])
    leases = SqlAlchemyLeaseStore(engine)
    resource = f"integration:initial-lease:{uuid4()}"

    try:
        lease = leases.acquire(resource, "worker-1", timedelta(seconds=60))

        assert lease.fence_token == 1
    finally:
        with engine.begin() as connection:
            connection.execute(
                delete(platform_lease_table).where(platform_lease_table.c.resource == resource)
            )
        engine.dispose()


def test_postgres_fenced_transaction_rejects_writes_after_lease_expiry() -> None:
    environment = _integration_environment()
    if environment is None:
        pytest.skip("PostgreSQL and S3 integration environment is not configured")

    command.upgrade(_alembic_config(environment["RAG_DATABASE_URL"]), "head")
    engine = create_engine(environment["RAG_DATABASE_URL"])
    metadata = MetaData()
    documents = Table(
        "platform_fence_expiry_probe",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("status", String(32), nullable=False),
    )
    metadata.create_all(engine)
    leases = SqlAlchemyLeaseStore(engine)
    resource = f"integration:fence-expiry:{uuid4()}"
    lease = leases.acquire(resource, "worker-1", timedelta(seconds=1))

    def write_after_expiry(connection) -> None:
        connection.execute(documents.insert().values(id=1, status="indexed"))
        time.sleep(1.1)

    with pytest.raises(FenceViolation):
        leases.write_with_fence(lease, write_after_expiry)

    with engine.connect() as connection:
        assert connection.execute(select(documents)).all() == []
    metadata.drop_all(engine)
    engine.dispose()
