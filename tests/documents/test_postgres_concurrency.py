from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier, Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import URL, make_url

from app.documents.schema import documents_metadata, documents_table, upload_dedup_claims_table
from app.documents.service import (
    DocumentsDepartmentWorkCheckPort,
    DocumentsService,
    DocumentUpload,
)
from app.identity.revocation import NoopGenerationRevocationPort
from app.identity.schema import identity_metadata
from app.identity.service import AuthPrincipal, IdentityAccessService
from app.platform.config import AuthSettings
from app.platform.database import core_metadata
from app.platform.errors import PlatformError
from app.platform.storage import MemoryObjectStore, ObjectMetadata

_PG_URL_ENV = "RAGQS_TEST_POSTGRES_URL"
_DESTRUCTIVE_OPTIN_ENV = "RAGQS_ALLOW_DESTRUCTIVE_POSTGRES_TESTS"


class _Identity:
    def authorize_space(self, *, principal, space_id: str, action: str, connection=None) -> str:
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
        pytest.skip("RAGQS_TEST_POSTGRES_URL database name must contain 'test' (NOT RUN/BLOCKED)")
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
        assert (
            connection.execute(select(func.count()).select_from(documents_table)).scalar_one() == 1
        )
        assert (
            connection.execute(
                select(func.count()).select_from(upload_dedup_claims_table)
            ).scalar_one()
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
        first = executor.submit(approve, first_submission["submission_id"], "concurrent-approval-1")
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
        assert (
            connection.execute(select(func.count()).select_from(documents_table)).scalar_one() == 1
        )
        assert (
            connection.execute(
                select(func.count()).select_from(upload_dedup_claims_table)
            ).scalar_one()
            == 1
        )


class _GatedSubmissionStore(MemoryObjectStore):
    """投稿对象写入后挂起事务：让部门行锁的持有窗口可被外部观测。"""

    def __init__(self) -> None:
        super().__init__()
        self.entered_txn = Event()
        self.release = Event()

    def put(self, key: str, content: bytes, metadata: ObjectMetadata) -> None:
        super().put(key, content, metadata)
        if key.startswith("submissions/"):
            self.entered_txn.set()
            self.release.wait(timeout=20)


@pytest.fixture()
def pg_acl_services():
    base_url = _postgres_test_url()
    schema = f"documents_acl_{uuid4().hex[:12]}"
    base_engine = create_engine(base_url)
    scoped_engine = None
    try:
        with base_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        scoped_engine = create_engine(_schema_url(base_url, schema))
        core_metadata.create_all(scoped_engine)
        identity_metadata.create_all(scoped_engine)
        documents_metadata.create_all(scoped_engine)
        identity = IdentityAccessService(
            scoped_engine,
            AuthSettings(secret_key="test-secret-that-is-long-enough"),
            revocation_port=NoopGenerationRevocationPort(),
            department_work_check=DocumentsDepartmentWorkCheckPort(scoped_engine),
        )
        store = _GatedSubmissionStore()
        documents = DocumentsService(
            scoped_engine,
            object_store=store,
            identity_access=identity,
        )
        yield SimpleNamespace(identity=identity, documents=documents, store=store)
    finally:
        if scoped_engine is not None:
            scoped_engine.dispose()
        with base_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        base_engine.dispose()


def _acl_setup(services):
    """admin + 部门 + 入部后的提交者（先入部再登录，避免授权变更撤会话）。"""

    identity = services.identity
    identity.provision_user(
        username="admin",
        password="Password1",
        real_name="Admin",
        display_name="Admin",
        role="admin",
        department_id=None,
    )
    admin = identity.authenticate_access_token(
        identity.login(username="admin", password="Password1").access_token
    )
    department = identity.create_department(
        actor=admin, name="Finance", idempotency_key="acl-dept-1"
    )
    user = identity.provision_user(
        username="bob",
        password="Password1",
        real_name="Bob",
        display_name="Bob",
        role="user",
        department_id=None,
    )
    identity.update_managed_user(
        actor=admin,
        user_id=user["id"],
        expected_version=1,
        role=None,
        department_id=department["id"],
        department_provided=True,
        idempotency_key="acl-assign-1",
    )
    bob = identity.authenticate_access_token(
        identity.login(username="bob", password="Password1").access_token
    )
    return SimpleNamespace(
        admin=admin,
        bob=bob,
        user=user,
        department=department,
        space_id=f"department:{department['id']}",
    )


def test_department_deactivation_serializes_against_inflight_submission(pg_acl_services) -> None:
    """C6-#24：投稿创建在写事务内锁定部门行，部门停用必须等其提交后才能评估。"""

    services = pg_acl_services
    env = _acl_setup(services)
    events: list[str] = []

    def submit() -> dict[str, object]:
        try:
            result = services.documents.create_submission(
                principal=env.bob,
                space_id=env.space_id,
                file=DocumentUpload("guide.txt", b"acl race content", "text/plain"),
                idempotency_key="acl-race-submission-1",
            )
            events.append("submission_committed")
            return result
        finally:
            services.store.release.set()

    def deactivate() -> dict[str, object]:
        result = services.identity.deactivate_department(
            actor=env.admin,
            department_id=env.department["id"],
            expected_version=1,
            idempotency_key="acl-deact-1",
        )
        events.append("deactivation_committed")
        return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        submission_future = executor.submit(submit)
        assert services.store.entered_txn.wait(timeout=20)
        deactivation_future = executor.submit(deactivate)
        # 给停用事务留出到达部门行锁的时间；正确实现下它被投稿事务阻塞、尚未完成。
        time.sleep(0.5)
        assert deactivation_future.running()
        services.store.release.set()
        submission = submission_future.result(timeout=20)
        # 投稿提交后停用才继续评估，随即被既有成员保护拒绝（成员检查在锁后执行）。
        with pytest.raises(PlatformError) as error:
            deactivation_future.result(timeout=20)

    assert submission["status"] == "pending"
    assert error.value.code == "department_has_members"
    assert events == ["submission_committed"]


def test_writes_to_a_deactivated_department_are_denied(pg_acl_services) -> None:
    """C6-#24：停用提交后，inactive 部门不再接受新的任务创建授权。"""

    services = pg_acl_services
    env = _acl_setup(services)
    identity = services.identity
    # 成员存在时停用被既有保护阻塞：先移出成员再停用。
    identity.update_managed_user(
        actor=env.admin,
        user_id=env.user["id"],
        expected_version=2,
        role=None,
        department_id=None,
        department_provided=True,
        idempotency_key="acl-remove-2",
    )
    deactivation = identity.deactivate_department(
        actor=env.admin,
        department_id=env.department["id"],
        expected_version=1,
        idempotency_key="acl-deact-first-1",
    )
    assert deactivation["status"] == "inactive"

    with pytest.raises(PlatformError) as error:
        services.documents.create_initial_upload(
            principal=env.admin,
            space_id=env.space_id,
            files=[DocumentUpload("guide.txt", b"after deactivation", "text/plain")],
            idempotency_key="acl-after-deact-1",
        )

    assert error.value.code == "space_action_forbidden"
