from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Connection
from sqlalchemy.pool import StaticPool

from app.documents.schema import (
    document_versions_table,
    documents_metadata,
    documents_table,
    ingestion_jobs_table,
)
from app.graph.schema import graph_metadata
from app.identity.schema import identity_metadata
from app.indexing.schema import indexing_metadata
from app.outbox.schema import outbox_metadata
from app.platform.app_factory import create_platform_app
from app.platform.config import load_platform_settings
from app.platform.database import core_metadata
from app.platform.runtime import build_runtime
from app.platform.storage import MemoryObjectStore
from app.usage.schema import usage_metadata

_MAX_UPLOAD_BYTES = 25 * 1024 * 1024


class _FixedClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def now_utc(self, connection: Connection | None = None) -> datetime:
        del connection
        return self.now

    def __call__(self) -> datetime:
        return self.now


class _AlwaysReleasedRetrieval:
    def is_released_for_generation(self, generation_id: str, connection: Connection) -> bool:
        del generation_id, connection
        return True

    def resolve(self, profile: object, generation_id: str) -> object:
        del generation_id
        return profile


class _RecordingUsage:
    def prepare_provider_call(self, **kwargs: object) -> str:
        del kwargs
        return "provider_call_1"

    def mark_dispatching(self, provider_call_id: str, **kwargs: object) -> bool:
        del provider_call_id, kwargs
        return True

    def complete_provider_call(self, **kwargs: object) -> str:
        return str(kwargs["provider_call_id"])

    def mark_not_sent(self, provider_call_id: str) -> None:
        del provider_call_id

    def mark_unknown(self, provider_call_id: str) -> None:
        del provider_call_id


def _make_client() -> tuple[TestClient, object, MemoryObjectStore]:
    settings = load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
        }
    )
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    documents_metadata.create_all(engine)
    indexing_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    graph_metadata.create_all(engine)

    from app.identity.service import IdentityAccessService

    identity = IdentityAccessService(engine, settings.auth)
    object_store = MemoryObjectStore()
    runtime = build_runtime(
        settings,
        adapters={
            "database_engine": engine,
            "database_clock": _FixedClock(datetime(2026, 8, 15, tzinfo=UTC)),
            "identity_access": identity,
            "object_store": object_store,
            "retrieval_release_service": _AlwaysReleasedRetrieval(),
            "graph_usage_submission": _RecordingUsage(),
        },
    )
    return TestClient(create_platform_app(settings, runtime=runtime)), runtime, object_store


def _seed_user(runtime: object) -> tuple[str, str]:
    identity = runtime.resolve("identity_access")  # type: ignore[attr-defined]
    user = identity.provision_user(
        username="upload-user",
        password="Password1",
        real_name="Upload User",
        display_name="Upload User",
        role="user",
        department_id=None,
    )
    login = identity.login(username="upload-user", password="Password1")
    return f"Bearer {login.access_token}", f"personal:{user['id']}"


def _document_state(runtime: object, object_store: MemoryObjectStore) -> tuple[int, int, int, int]:
    engine = runtime.resolve("database_engine")  # type: ignore[attr-defined]
    with engine.connect() as connection:
        return (
            int(connection.execute(select(func.count()).select_from(documents_table)).scalar_one()),
            int(connection.execute(select(func.count()).select_from(document_versions_table)).scalar_one()),
            int(connection.execute(select(func.count()).select_from(ingestion_jobs_table)).scalar_one()),
            len(object_store._objects),
        )


def test_upload_and_replacement_reject_oversized_files_before_persistence() -> None:
    client, runtime, object_store = _make_client()
    token, space_id = _seed_user(runtime)
    oversized = b"x" * (_MAX_UPLOAD_BYTES + 1)

    try:
        initial = client.post(
            f"/v1/spaces/{space_id}/documents",
            files=[("files", ("too-large.txt", oversized, "text/plain"))],
            headers={"Authorization": token, "Idempotency-Key": "oversized-initial"},
        )

        assert initial.status_code == 413
        assert initial.json()["error"]["code"] == "upload_too_large"
        assert _document_state(runtime, object_store) == (0, 0, 0, 0)

        created = client.post(
            f"/v1/spaces/{space_id}/documents",
            files=[("files", ("guide.txt", b"initial", "text/plain"))],
            headers={"Authorization": token, "Idempotency-Key": "initial"},
        )
        assert created.status_code == 202
        document_id = created.json()["items"][0]["document_id"]
        before_replace = _document_state(runtime, object_store)

        replacement = client.post(
            f"/v1/documents/{document_id}/versions",
            files={"file": ("too-large.txt", oversized, "text/plain")},
            data={"expected_version": "1"},
            headers={"Authorization": token, "Idempotency-Key": "oversized-replacement"},
        )

        assert replacement.status_code == 413
        assert replacement.json()["error"]["code"] == "upload_too_large"
        assert _document_state(runtime, object_store) == before_replace
    finally:
        client.close()
