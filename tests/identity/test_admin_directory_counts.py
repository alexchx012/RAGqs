from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.documents.schema import documents_metadata
from app.documents.service import DocumentsDepartmentWorkCheckPort, DocumentsService, DocumentUpload
from app.identity.revocation import NoopGenerationRevocationPort
from app.identity.schema import identity_metadata
from app.identity.service import AuthPrincipal, IdentityAccessService
from app.platform.config import AuthSettings, load_platform_settings
from app.platform.database import core_metadata
from app.platform.storage import MemoryObjectStore


def _engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    documents_metadata.create_all(engine)
    return engine


class _AllAccess:
    def authorize_space(self, *, principal, space_id: str, action: str) -> str:
        return "manage"


def _admin_and_user(
    engine, *, retention_days: int = 30
) -> tuple[IdentityAccessService, AuthPrincipal, AuthPrincipal, str]:
    service = IdentityAccessService(
        engine,
        AuthSettings(
            secret_key="test-secret-that-is-long-enough",
            user_deletion_retention_days=retention_days,
        ),
        department_work_check=DocumentsDepartmentWorkCheckPort(engine),
        revocation_port=NoopGenerationRevocationPort(),
    )
    service.provision_user(
        username="admin",
        password="Password1",
        real_name="Admin",
        display_name="Admin",
        role="admin",
        department_id=None,
    )
    admin = service.authenticate_access_token(
        service.login(username="admin", password="Password1").access_token
    )
    department = service.create_department(actor=admin, name="Finance", idempotency_key="dep-1")
    service.create_managed_user(
        actor=admin,
        username="member",
        password="Password1",
        real_name="Member",
        display_name="Member",
        role="user",
        department_id=department["id"],
        idempotency_key="user-1",
    )
    member = service.authenticate_access_token(
        service.login(username="member", password="Password1").access_token
    )
    return service, admin, member, department["id"]


def test_deletion_retention_days_is_configurable() -> None:
    settings = load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
            "USER_DELETION_RETENTION_DAYS": "7",
        }
    )
    assert settings.auth.user_deletion_retention_days == 7


def test_deletion_retention_days_defaults_to_thirty() -> None:
    settings = load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
        }
    )
    assert settings.auth.user_deletion_retention_days == 30


def test_managed_deletion_uses_configured_retention() -> None:
    engine = _engine()
    service, admin, member, _ = _admin_and_user(engine, retention_days=7)
    before = datetime.now(UTC)
    response = service.delete_managed_user(
        actor=admin,
        user_id=member.user_id,
        expected_version=1,
        idempotency_key="delete-1",
    )
    purge_after = datetime.fromisoformat(str(response["purge_after_at"]))
    delta = purge_after - before
    assert timedelta(days=6) < delta <= timedelta(days=7, hours=1)


def test_admin_roster_removal_uses_configured_retention() -> None:
    engine = _engine()
    secret = "test-secret-that-is-long-enough"
    first = IdentityAccessService(
        engine,
        AuthSettings(secret_key=secret, admin_roster=("retained", "removed")),
        revocation_port=NoopGenerationRevocationPort(),
    )
    first.provision_user(
        username="retained",
        password="Password1",
        real_name="Retained",
        display_name="Retained",
        role="admin",
        department_id=None,
    )
    first.provision_user(
        username="removed",
        password="Password1",
        real_name="Removed",
        display_name="Removed",
        role="admin",
        department_id=None,
    )
    service = IdentityAccessService(
        engine,
        AuthSettings(
            secret_key=secret,
            admin_roster=("retained",),
            user_deletion_retention_days=5,
        ),
        revocation_port=NoopGenerationRevocationPort(),
    )
    before = datetime.now(UTC)
    reconciled = service.reconcile_admin_roster()
    assert len(reconciled) == 1
    admin = service.authenticate_access_token(
        service.login(username="retained", password="Password1").access_token
    )
    listed = service.list_managed_users(actor=admin)
    spare = next(item for item in listed["items"] if item["username"] == "removed")
    purge_after = datetime.fromisoformat(spare["purge_after_at"])
    assert timedelta(days=4) < purge_after - before <= timedelta(days=5, hours=1)


def test_directory_counts_use_real_queries_by_user_id() -> None:
    engine = _engine()
    identity, admin, member, department_id = _admin_and_user(engine)
    documents = DocumentsService(
        engine,
        now=lambda: datetime(2026, 8, 22, tzinfo=UTC),
        object_store=MemoryObjectStore(),
        identity_access=_AllAccess(),
    )
    documents.create_upload(
        principal=member,
        space_id=f"department:{department_id}",
        files=[DocumentUpload(filename="guide.txt", content=b"hello", media_kind="text/plain")],
        idempotency_key="upload-1",
    )

    roster = identity.list_managed_users(actor=admin, q="member")
    member_row = next(item for item in roster["items"] if item["username"] == "member")
    assert member_row["id"] == member.user_id
    assert member_row["document_count"] == 1

    departments = identity.list_departments(actor=admin)
    finance = next(item for item in departments if item["name"] == "Finance")
    assert finance["document_count"] == 1
    assert finance["nonterminal_job_count"] == 1
    assert finance["member_count"] == 1
