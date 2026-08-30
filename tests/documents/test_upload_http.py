from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Connection
from sqlalchemy.pool import StaticPool

from app.documents.schema import (
    document_versions_table,
    documents_metadata,
    documents_table,
    ingestion_jobs_table,
    knowledge_submissions_table,
    upload_batch_items_table,
)
from app.documents.service import DocumentUpload
from app.graph.schema import graph_metadata
from app.identity.schema import identity_metadata
from app.indexing.schema import indexing_metadata
from app.outbox.schema import outbox_metadata
from app.platform.app_factory import create_platform_app
from app.platform.config import load_platform_settings
from app.platform.database import core_metadata
from app.platform.errors import PlatformError
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
    app, runtime, object_store = _make_app()
    return TestClient(app), runtime, object_store


def _make_app(**extra_env: str) -> tuple[object, object, MemoryObjectStore]:
    settings = load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
            **extra_env,
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
    core_metadata.create_all(engine)

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
    return create_platform_app(settings, runtime=runtime), runtime, object_store


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
            int(
                connection.execute(
                    select(func.count()).select_from(document_versions_table)
                ).scalar_one()
            ),
            int(
                connection.execute(
                    select(func.count()).select_from(ingestion_jobs_table)
                ).scalar_one()
            ),
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

        # Per-file contract: an oversized file is a rejected item, not a
        # request-level failure.
        assert initial.status_code == 202
        item = initial.json()["items"][0]
        assert item["accepted"] is False
        assert item["name"] == "too-large.txt"
        assert item["error"]["code"] == "upload_too_large"
        assert item["document_id"] is None
        assert item["job_id"] is None
        assert item["submission_id"] is None
        assert _document_state(runtime, object_store) == (0, 0, 0, 0)

        batch = client.get(
            f"/v1/upload-batches/{initial.json()['upload_batch_id']}",
            headers={"Authorization": token},
        )
        assert batch.status_code == 200
        assert batch.json()["summary"]["rejected"] == 1
        engine = runtime.resolve("database_engine")  # type: ignore[attr-defined]
        with engine.connect() as connection:
            reason = connection.execute(
                select(upload_batch_items_table.c.rejection_reason)
            ).scalar_one()
        assert reason == "upload_too_large"

        created = client.post(
            f"/v1/spaces/{space_id}/documents",
            files=[("files", ("guide.txt", b"initial", "text/plain"))],
            headers={"Authorization": token, "Idempotency-Key": "initial"},
        )
        assert created.status_code == 202
        document_id = created.json()["items"][0]["document_id"]
        before_replace = _document_state(runtime, object_store)

        # Single-file replacement keeps the request-level 413 contract.
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


def test_mixed_batch_accepts_valid_file_and_rejects_oversized_per_item() -> None:
    client, runtime, object_store = _make_client()
    token, space_id = _seed_user(runtime)
    oversized = b"x" * (_MAX_UPLOAD_BYTES + 1)
    try:
        response = client.post(
            f"/v1/spaces/{space_id}/documents",
            files=[
                ("files", ("guide.txt", b"payload", "text/plain")),
                ("files", ("too-large.txt", oversized, "text/plain")),
            ],
            headers={"Authorization": token, "Idempotency-Key": "mixed-initial"},
        )
        assert response.status_code == 202
        items = response.json()["items"]
        assert [item["accepted"] for item in items] == [True, False]
        accepted = items[0]
        assert accepted["name"] == "guide.txt"
        assert accepted["space_id"] == space_id
        assert accepted["document_id"] and accepted["document_version_id"]
        assert accepted["job_id"] and accepted["publication_id"]
        assert accepted["deduplicated"] is False
        rejected = items[1]
        assert rejected["accepted"] is False
        assert rejected["name"] == "too-large.txt"
        assert rejected["error"]["code"] == "upload_too_large"
        assert rejected["document_id"] is None
        assert rejected["submission_id"] is None
        documents, versions, jobs, objects = _document_state(runtime, object_store)
        assert (documents, versions, jobs) == (1, 1, 1)
        assert objects == 1
    finally:
        client.close()


def test_mixed_batch_replay_is_idempotent_and_rejected_file_stays_side_effect_free() -> None:
    client, runtime, object_store = _make_client()
    token, space_id = _seed_user(runtime)
    oversized = b"x" * (_MAX_UPLOAD_BYTES + 1)
    payload = {
        "files": [
            ("files", ("guide.txt", b"payload", "text/plain")),
            ("files", ("too-large.txt", oversized, "text/plain")),
        ],
    }
    headers = {"Authorization": token, "Idempotency-Key": "mixed-replay"}
    try:
        first = client.post(f"/v1/spaces/{space_id}/documents", **payload, headers=headers)
        assert first.status_code == 202
        second = client.post(f"/v1/spaces/{space_id}/documents", **payload, headers=headers)
        assert second.status_code == 202
        assert second.json() == first.json()
        documents, versions, jobs, objects = _document_state(runtime, object_store)
        assert (documents, versions, jobs, objects) == (1, 1, 1, 1)
        engine = runtime.resolve("database_engine")  # type: ignore[attr-defined]
        with engine.connect() as connection:
            rejected_rows = connection.execute(
                select(func.count())
                .select_from(upload_batch_items_table)
                .where(upload_batch_items_table.c.result_state == "rejected")
            ).scalar_one()
        assert int(rejected_rows) == 1
    finally:
        client.close()


def test_contribute_mixed_batch_creates_submissions_per_item() -> None:
    client, runtime, object_store = _make_client()
    token, _ = _seed_user(runtime)
    oversized = b"x" * (_MAX_UPLOAD_BYTES + 1)
    try:
        response = client.post(
            "/v1/spaces/public/documents",
            files=[
                ("files", ("note.txt", b"submission", "text/plain")),
                ("files", ("too-large.txt", oversized, "text/plain")),
            ],
            headers={"Authorization": token, "Idempotency-Key": "mixed-contribute"},
        )
        assert response.status_code == 202
        items = response.json()["items"]
        assert [item["accepted"] for item in items] == [True, False]
        accepted = items[0]
        assert accepted["name"] == "note.txt"
        assert accepted["submission_id"]
        assert accepted["status"] == "pending"
        assert accepted["space_id"] == "public"
        assert accepted["quota_exempt"] is True
        assert accepted["document_id"] is None
        rejected = items[1]
        assert rejected["accepted"] is False
        assert rejected["name"] == "too-large.txt"
        assert rejected["error"]["code"] == "upload_too_large"
        assert rejected["submission_id"] is None
        engine = runtime.resolve("database_engine")  # type: ignore[attr-defined]
        with engine.connect() as connection:
            submissions = connection.execute(
                select(func.count()).select_from(knowledge_submissions_table)
            ).scalar_one()
        assert int(submissions) == 1
        assert _document_state(runtime, object_store) == (0, 0, 0, 1)
    finally:
        client.close()


def test_malware_upload_rejected_at_http_layer_without_persistence() -> None:
    client, runtime, object_store = _make_client()
    token, space_id = _seed_user(runtime)
    eicar = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
    try:
        rejected = client.post(
            f"/v1/spaces/{space_id}/documents",
            files=[("files", ("eicar.txt", eicar, "text/plain"))],
            headers={"Authorization": token, "Idempotency-Key": "malware-initial"},
        )
        assert rejected.status_code == 202
        item = rejected.json()["items"][0]
        assert item["accepted"] is False
        error = item["error"]
        assert error["code"] == "malware_detected"
        # No scan detail, object key or storage location in the error object.
        serialized = str(error)
        assert "documents/" not in serialized and "object" not in serialized
        assert _document_state(runtime, object_store) == (0, 0, 0, 0)
    finally:
        client.close()


def test_upload_rejects_too_many_files_before_reading_any_bytes() -> None:
    app, runtime, object_store = _make_app(RAG_UPLOAD_MAX_FILES_PER_REQUEST="2")
    client = TestClient(app)
    token, space_id = _seed_user(runtime)
    try:
        response = client.post(
            f"/v1/spaces/{space_id}/documents",
            files=[
                ("files", ("a.txt", b"a", "text/plain")),
                ("files", ("b.txt", b"b", "text/plain")),
                ("files", ("c.txt", b"c", "text/plain")),
            ],
            headers={"Authorization": token, "Idempotency-Key": "too-many-files"},
        )
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "validation_error"
        assert error["details"]["max_files_per_request"] == 2
        assert _document_state(runtime, object_store) == (0, 0, 0, 0)
    finally:
        client.close()


def test_upload_rejects_aggregate_bytes_over_budget() -> None:
    app, runtime, object_store = _make_app(RAG_UPLOAD_MAX_REQUEST_BYTES="16")
    client = TestClient(app)
    token, space_id = _seed_user(runtime)
    try:
        response = client.post(
            f"/v1/spaces/{space_id}/documents",
            files=[
                ("files", ("a.txt", b"a" * 8, "text/plain")),
                ("files", ("b.txt", b"b" * 9, "text/plain")),
            ],
            headers={"Authorization": token, "Idempotency-Key": "aggregate-over-budget"},
        )
        assert response.status_code == 413
        error = response.json()["error"]
        assert error["code"] == "upload_too_large"
        assert error["details"]["max_request_bytes"] == 16
        assert _document_state(runtime, object_store) == (0, 0, 0, 0)
    finally:
        client.close()


def test_direct_upload_quota_exhaustion_fails_whole_batch() -> None:
    from app.documents.indexing import NoopIndexingHandoff

    class _RejectingQuota:
        def check(self, connection: object, **values: object) -> None:
            del connection, values
            raise PlatformError("quota_exceeded", "Quota is exhausted", {}, 409)

    class _ManageIdentity:
        def authorize_space(
            self, *, principal: object, space_id: str, action: str, connection: object = None
        ) -> str:
            del principal, space_id, action, connection
            return "manage"

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    documents_metadata.create_all(engine)
    from app.documents.service import DocumentsService

    service = DocumentsService(
        engine,
        now=lambda: datetime(2026, 8, 15, tzinfo=UTC),
        object_store=MemoryObjectStore(),
        identity_access=_ManageIdentity(),
        indexing_handoff_port=NoopIndexingHandoff(),
        quota_service=_RejectingQuota(),
    )
    from app.identity.service import AuthPrincipal

    principal = AuthPrincipal(
        user_id="user_1",
        auth_session_id="session_1",
        username="alice",
        role="user",
        department_id=None,
    )
    with pytest.raises(PlatformError) as exc_info:
        service.create_initial_upload(
            principal=principal,
            space_id="space_1",
            files=[DocumentUpload(filename="a.txt", content=b"x", media_kind="text/plain")],
            idempotency_key="quota-key",
        )
    assert exc_info.value.code == "quota_exceeded"
    assert exc_info.value.status_code == 409


def test_delete_document_uses_query_parameter_without_body() -> None:
    client, runtime, _ = _make_client()
    token, space_id = _seed_user(runtime)
    try:
        created = client.post(
            f"/v1/spaces/{space_id}/documents",
            files=[("files", ("guide.txt", b"initial", "text/plain"))],
            headers={"Authorization": token, "Idempotency-Key": "delete-setup"},
        )
        assert created.status_code == 202
        document_id = created.json()["items"][0]["document_id"]

        conflict = client.delete(
            f"/v1/documents/{document_id}?expected_version=99",
            headers={"Authorization": token, "Idempotency-Key": "delete-conflict"},
        )
        assert conflict.status_code == 409

        deleted = client.delete(
            f"/v1/documents/{document_id}?expected_version=1",
            headers={"Authorization": token, "Idempotency-Key": "delete-ok"},
        )
        assert deleted.status_code == 202
        listing = client.get(f"/v1/spaces/{space_id}/documents", headers={"Authorization": token})
        items = listing.json()["items"]
        assert all(item["document_id"] != document_id for item in items)
    finally:
        client.close()


def test_delete_submission_uses_query_parameter_without_body() -> None:
    from app.identity.service import AuthPrincipal

    client, runtime, _ = _make_client()
    token, space_id = _seed_user(runtime)
    identity = runtime.resolve("identity_access")  # type: ignore[attr-defined]
    login = identity.login(username="upload-user", password="Password1")
    principal = AuthPrincipal(
        user_id=str(login.user["id"]),
        auth_session_id=login.session_id,
        username="upload-user",
        role="user",
        department_id=None,
    )
    service = runtime.resolve("documents_service")  # type: ignore[attr-defined]
    try:
        created = service.create_submission(
            principal=principal,
            space_id=space_id,
            file=DocumentUpload(
                filename="note.txt", content=b"submission", media_kind="text/plain"
            ),
            idempotency_key="submission-setup",
        )
        submission_id = created["submission_id"]

        pending = client.delete(
            f"/v1/submissions/{submission_id}?expected_version=1",
            headers={"Authorization": token, "Idempotency-Key": "submission-delete"},
        )
        assert pending.status_code == 409
        assert pending.json()["error"]["code"] == "submission_state_conflict"
    finally:
        client.close()


async def test_upload_does_not_block_concurrent_health_checks() -> None:
    import asyncio
    import time

    import httpx

    app, runtime, _ = _make_app()
    token, space_id = _seed_user(runtime)
    service = runtime.resolve("documents_service")  # type: ignore[attr-defined]
    original_create_upload = service.create_upload

    def slow_create_upload(**kwargs: object) -> object:
        time.sleep(0.5)
        return original_create_upload(**kwargs)  # type: ignore[arg-type]

    service.create_upload = slow_create_upload  # type: ignore[method-assign, assignment]
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        upload_task = asyncio.create_task(
            client.post(
                f"/v1/spaces/{space_id}/documents",
                files=[("files", ("guide.txt", b"payload", "text/plain"))],
                headers={"Authorization": token, "Idempotency-Key": "concurrent-upload"},
            )
        )
        try:
            await asyncio.sleep(0.15)
            started = time.monotonic()
            health = await client.get("/v1/health")
            health_latency = time.monotonic() - started
            uploaded = await upload_task
        except BaseException:
            upload_task.cancel()
            raise
    assert health.status_code == 200
    assert uploaded.status_code == 202
    assert health_latency < 0.4
