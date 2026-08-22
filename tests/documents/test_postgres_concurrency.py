from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import URL, make_url

from app.documents.schema import documents_metadata, documents_table, upload_dedup_claims_table
from app.documents.service import DocumentsService, DocumentUpload
from app.identity.service import AuthPrincipal
from app.platform.database import core_metadata
from app.platform.errors import PlatformError
from app.platform.storage import MemoryObjectStore, ObjectMetadata

_PG_URL_ENV = "RAGQS_TEST_POSTGRES_URL"
_DESTRUCTIVE_OPTIN_ENV = "RAGQS_ALLOW_DESTRUCTIVE_POSTGRES_TESTS"


class _Identity:
    def authorize_space(self, *, principal, space_id: str, action: str) -> str:
        del principal, space_id, action
        return "manage"


class _ConcurrentUploadStore(MemoryObjectStore):
    def __init__(self) -> None:
        super().__init__()
        self._barrier = Barrier(2)

    def put(self, key: str, content: bytes, metadata: ObjectMetadata) -> None:
        super().put(key, content, metadata)
        if key.startswith("documents/"):
            self._barrier.wait(timeout=20)


def _postgres_test_url() -> URL:
    url = os.environ.get(_PG_URL_ENV)
    if not url:
        pytest.skip(
            "PostgreSQL concurrency acceptance requires RAGQS_TEST_POSTGRES_URL "
            "(NOT RUN/BLOCKED)"
        )
    if os.environ.get(_DESTRUCTIVE_OPTIN_ENV) != "1":
        pytest.skip(
            "PostgreSQL concurrency acceptance requires "
            "RAGQS_ALLOW_DESTRUCTIVE_POSTGRES_TESTS=1 (NOT RUN/BLOCKED)"
        )
    try:
        parsed = make_url(url)
    except Exception:  # noqa: BLE001 - an invalid external test URL must skip
        pytest.skip("RAGQS_TEST_POSTGRES_URL is malformed (NOT RUN/BLOCKED)")
    if parsed.get_backend_name() != "postgresql":
        pytest.skip("RAGQS_TEST_POSTGRES_URL must use a postgresql backend (NOT RUN/BLOCKED)")
    if parsed.database is None or "test" not in parsed.database.lower():
        pytest.skip(
            "RAGQS_TEST_POSTGRES_URL database name must contain 'test' (NOT RUN/BLOCKED)"
        )
    return parsed


def _schema_url(url: URL, schema: str) -> URL:
    query = dict(url.query)
    existing_options = str(query.get("options", "")).strip()
    query["options"] = f"{existing_options} -csearch_path={schema}".strip()
    return url.set(query=query)


@pytest.fixture()
def pg_documents_service():
    base_url = _postgres_test_url()
    schema = f"documents_test_{uuid4().hex[:12]}"
    base_engine = create_engine(base_url)
    scoped_engine = None
    try:
        with base_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        scoped_engine = create_engine(_schema_url(base_url, schema))
        core_metadata.create_all(scoped_engine)

        documents_metadata.create_all(scoped_engine)
        service = DocumentsService(
            scoped_engine,
            now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
            object_store=_ConcurrentUploadStore(),
            identity_access=_Identity(),
        )
        yield service
    finally:
        if scoped_engine is not None:
            scoped_engine.dispose()
        with base_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        base_engine.dispose()


def test_concurrent_initial_upload_deduplicates_the_losing_claim(pg_documents_service) -> None:
    principal = AuthPrincipal(
        user_id="user_1",
        auth_session_id="session_1",
        username="alice",
        role="user",
        department_id=None,
    )

    def upload(idempotency_key: str) -> dict[str, object]:
        return pg_documents_service.create_initial_upload(
            principal=principal,
            space_id="space_1",
            files=[DocumentUpload("guide.txt", b"concurrent content", "text/plain")],
            idempotency_key=idempotency_key,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(upload, "concurrent-upload-1")
        second = executor.submit(upload, "concurrent-upload-2")
        results = [first.result(), second.result()]

    items = [result["items"][0] for result in results]
    created = next(item for item in items if item["deduplicated"] is False)
    duplicate = next(item for item in items if item["deduplicated"] is True)
    assert duplicate["document_id"] == created["document_id"]
    with pg_documents_service._engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(documents_table)).scalar_one() == 1
        assert (
            connection.execute(select(func.count()).select_from(upload_dedup_claims_table)).scalar_one()
            == 1
        )


def test_concurrent_submission_approvals_return_a_duplicate_document_conflict(
    pg_documents_service,
) -> None:
    submitter = AuthPrincipal(
        user_id="user_1",
        auth_session_id="session_1",
        username="alice",
        role="user",
        department_id=None,
    )
    reviewer = AuthPrincipal(
        user_id="admin_1",
        auth_session_id="session_admin",
        username="admin",
        role="admin",
        department_id=None,
    )
    first_submission = pg_documents_service.create_submission(
        principal=submitter,
        space_id="space_1",
        file=DocumentUpload("guide.txt", b"concurrent content", "text/plain"),
        idempotency_key="concurrent-submission-1",
    )
    second_submission = pg_documents_service.create_submission(
        principal=submitter,
        space_id="space_1",
        file=DocumentUpload("guide.txt", b"concurrent content", "text/plain"),
        idempotency_key="concurrent-submission-2",
    )

    def approve(submission_id: str, idempotency_key: str) -> dict[str, object] | PlatformError:
        try:
            return pg_documents_service.approve_submission(
                principal=reviewer,
                submission_id=submission_id,
                expected_version=1,
                idempotency_key=idempotency_key,
            )
        except PlatformError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            approve, first_submission["submission_id"], "concurrent-approval-1"
        )
        second = executor.submit(
            approve, second_submission["submission_id"], "concurrent-approval-2"
        )
        results = [first.result(), second.result()]

    approved = [result for result in results if isinstance(result, dict)]
    conflicts = [result for result in results if isinstance(result, PlatformError)]
    assert len(approved) == 1
    assert len(conflicts) == 1
    assert conflicts[0].code == "duplicate_document"
    assert conflicts[0].status_code == 409
    with pg_documents_service._engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(documents_table)).scalar_one() == 1
        assert (
            connection.execute(select(func.count()).select_from(upload_dedup_claims_table)).scalar_one()
            == 1
        )
