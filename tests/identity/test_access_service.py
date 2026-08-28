from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.chat.schema import chat_metadata
from app.documents.schema import documents_metadata
from app.identity.archive import IdentityArchiveProofIssuer
from app.identity.ports import (
    DepartmentWorkState,
    NoopAccountDeletionCleanupPort,
    NoopAccountRetirementGateway,
    NoopDepartmentWorkCheckPort,
    NoopPersonalDocumentDeletionPort,
)
from app.identity.revocation import GenerationRevocationReceipt, NoopGenerationRevocationPort
from app.identity.schema import identity_deletion_workflow_table, identity_metadata
from app.identity.service import IdentityAccessService
from app.outbox.schema import outbox_metadata
from app.platform.config import AuthSettings
from app.platform.database import core_metadata
from app.platform.errors import PlatformError


def make_service(**kwargs: object) -> IdentityAccessService:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    kwargs.setdefault("revocation_port", NoopGenerationRevocationPort())
    kwargs.setdefault("department_work_check", NoopDepartmentWorkCheckPort())
    kwargs.setdefault("deletion_cleanup_port", NoopAccountDeletionCleanupPort())
    kwargs.setdefault("account_retirement_gateway", NoopAccountRetirementGateway())
    kwargs.setdefault(
        "archive_issuer",
        IdentityArchiveProofIssuer(secret=b"test-secret-archive-key"),
    )
    return IdentityAccessService(
        engine,
        AuthSettings(secret_key="test-secret-that-is-long-enough"),
        **kwargs,
    )


def test_admin_department_creation_provisions_acl_backed_spaces() -> None:
    service = make_service()
    service.provision_user(
        username="admin",
        password="Password1",
        real_name="Admin",
        display_name="Admin",
        role="admin",
        department_id=None,
    )
    admin_principal = service.authenticate_access_token(
        service.login(username="admin", password="Password1").access_token
    )

    department = service.create_department(
        actor=admin_principal,
        name="Finance",
        idempotency_key="department-create-1",
    )
    minister = service.create_managed_user(
        actor=admin_principal,
        username="minister",
        password="Password1",
        real_name="Minister",
        display_name="Minister",
        role="minister",
        department_id=department["id"],
        idempotency_key="user-create-1",
    )
    minister_principal = service.authenticate_access_token(
        service.login(username="minister", password="Password1").access_token
    )

    spaces = service.list_spaces(principal=minister_principal, usage="retrieval")

    assert department["status"] == "active"
    assert minister["department"]["id"] == department["id"]
    assert {item["id"]: item["permission"] for item in spaces} == {
        f"personal:{minister['id']}": "manage",
        f"department:{department['id']}": "manage",
        "public": "contribute",
    }


def test_admin_role_change_revokes_the_target_users_active_sessions() -> None:
    service = make_service()
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
    user = service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    old_login = service.login(username="alice", password="Password1")

    updated = service.update_managed_user(
        actor=admin,
        user_id=user["id"],
        expected_version=1,
        role="ops",
        department_id=None,
        department_provided=False,
        idempotency_key="user-update-1",
    )

    assert updated["role"] == "ops"
    assert updated["version"] == 2
    with pytest.raises(PlatformError, match="revoked"):
        service.authenticate_access_token(old_login.access_token)


def test_department_deactivation_blocks_members_then_leaves_ops_read_only_access() -> None:
    service = make_service()
    service.provision_user(
        username="admin",
        password="Password1",
        real_name="Admin",
        display_name="Admin",
        role="admin",
        department_id=None,
    )
    service.provision_user(
        username="ops",
        password="Password1",
        real_name="Ops",
        display_name="Ops",
        role="ops",
        department_id=None,
    )
    admin = service.authenticate_access_token(
        service.login(username="admin", password="Password1").access_token
    )
    ops = service.authenticate_access_token(
        service.login(username="ops", password="Password1").access_token
    )
    department = service.create_department(
        actor=admin, name="Finance", idempotency_key="department-create-1"
    )
    minister = service.create_managed_user(
        actor=admin,
        username="minister",
        password="Password1",
        real_name="Minister",
        display_name="Minister",
        role="minister",
        department_id=department["id"],
        idempotency_key="user-create-1",
    )

    with pytest.raises(PlatformError) as exc_info:
        service.deactivate_department(
            actor=admin,
            department_id=department["id"],
            expected_version=1,
            idempotency_key="department-deactivate-1",
        )
    assert exc_info.value.code == "department_has_members"

    service.update_managed_user(
        actor=admin,
        user_id=minister["id"],
        expected_version=1,
        role="user",
        department_id=None,
        department_provided=True,
        idempotency_key="user-update-1",
    )
    deactivated = service.deactivate_department(
        actor=admin,
        department_id=department["id"],
        expected_version=1,
        idempotency_key="department-deactivate-2",
    )

    assert deactivated["status"] == "inactive"
    ops_spaces = {item["id"]: item["permission"] for item in service.list_spaces(principal=ops)}
    assert ops_spaces[f"department:{department['id']}"] == "read"


def test_admin_delete_starts_irreversible_pending_delete_lifecycle() -> None:
    service = make_service()
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
    user = service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    login = service.login(username="alice", password="Password1")

    deleted = service.delete_managed_user(
        actor=admin,
        user_id=user["id"],
        expected_version=1,
        idempotency_key="user-delete-1",
    )

    assert deleted["lifecycle_status"] == "pending_delete"
    assert deleted["version"] == 2
    assert deleted["deletion_requested_at"] is not None
    assert deleted["purge_after_at"] is not None
    with pytest.raises(PlatformError, match="revoked"):
        service.authenticate_access_token(login.access_token)
    with pytest.raises(PlatformError) as exc_info:
        service.login(username="alice", password="Password1")
    assert exc_info.value.code == "invalid_credentials"


def test_pending_delete_emits_account_revocation_without_an_active_session() -> None:
    class RecordingRevocationPort:
        def __init__(self) -> None:
            self.commands = []

        def revoke(self, command, *, connection):
            del connection
            self.commands.append(command)
            return GenerationRevocationReceipt(reference="chat-receipt-delete", state="accepted")

    port = RecordingRevocationPort()
    service = make_service(revocation_port=port)
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
    user = service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )

    service.delete_managed_user(
        actor=admin,
        user_id=user["id"],
        expected_version=1,
        idempotency_key="user-delete-without-session",
    )

    assert len(port.commands) == 1
    command = port.commands[0]
    assert command.user_id == user["id"]
    assert command.auth_session_id is None
    assert command.reason == "account_pending_delete"
    assert command.identity_transition_version == 2


def _prepare_account_deletion(service: IdentityAccessService, *, user_id: str) -> None:
    """§9.2.1: create remaining-domain tables, the Noop personal-document
    port, and the physical archive package before finalizing a deletion."""
    for metadata in (chat_metadata, documents_metadata, outbox_metadata):
        metadata.create_all(service._engine)
    service._personal_document_deletion = NoopPersonalDocumentDeletionPort()
    service.build_deletion_archive(user_id=user_id)


def test_pending_account_deletion_can_be_finalized_to_a_tombstone() -> None:
    current = datetime(2026, 8, 6, tzinfo=UTC)
    service = make_service(now=lambda: current)
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
    user = service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    service.delete_managed_user(
        actor=admin,
        user_id=user["id"],
        expected_version=1,
        idempotency_key="user-delete-1",
    )

    with pytest.raises(PlatformError) as early_finalization:
        service.finalize_pending_deletion(user_id=user["id"])
    assert early_finalization.value.code == "deletion_not_ready"

    current += timedelta(days=30)
    _prepare_account_deletion(service, user_id=user["id"])
    finalized = service.finalize_pending_deletion(user_id=user["id"])

    assert finalized == {"id": user["id"], "lifecycle_status": "deleted"}
    assert service.finalize_pending_deletion(user_id=user["id"]) == finalized
    with service._engine.connect() as connection:
        workflow = (
            connection.execute(
                identity_deletion_workflow_table.select().where(
                    identity_deletion_workflow_table.c.user_id == user["id"]
                )
            )
            .mappings()
            .one()
        )
    assert workflow["status"] == "completed"
    assert workflow["cleanup_reference"] is not None
    assert workflow["cleanup_completed_at_utc"] is not None


def test_deletion_finalization_fails_closed_without_cleanup_confirmation() -> None:
    current = datetime(2026, 8, 6, tzinfo=UTC)
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    service = IdentityAccessService(
        engine,
        AuthSettings(secret_key="test-secret-that-is-long-enough"),
        now=lambda: current,
        revocation_port=NoopGenerationRevocationPort(),
        department_work_check=NoopDepartmentWorkCheckPort(),
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
    user = service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    service.delete_managed_user(
        actor=admin,
        user_id=user["id"],
        expected_version=1,
        idempotency_key="user-delete-1",
    )
    current += timedelta(days=30)
    _prepare_account_deletion(service, user_id=user["id"])

    with pytest.raises(PlatformError) as exc_info:
        service.finalize_pending_deletion(user_id=user["id"])

    assert exc_info.value.code == "deletion_cleanup_unverified"
    assert exc_info.value.status_code == 503


def test_admin_user_idempotency_key_rejects_a_different_initial_password() -> None:
    service = make_service()
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
    service.create_managed_user(
        actor=admin,
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
        idempotency_key="user-create-1",
    )

    with pytest.raises(PlatformError) as exc_info:
        service.create_managed_user(
            actor=admin,
            username="alice",
            password="Different2",
            real_name="Alice",
            display_name="Alice",
            role="user",
            department_id=None,
            idempotency_key="user-create-1",
        )

    assert exc_info.value.code == "idempotency_key_conflict"


def test_idempotency_replays_the_first_version_error_after_state_changes() -> None:
    service = make_service()
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
    user = service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )

    with pytest.raises(PlatformError) as first_error:
        service.update_managed_user(
            actor=admin,
            user_id=user["id"],
            expected_version=2,
            role=None,
            department_id=None,
            department_provided=False,
            idempotency_key="stale-update",
        )
    assert first_error.value.code == "version_conflict"

    service.update_managed_user(
        actor=admin,
        user_id=user["id"],
        expected_version=1,
        role=None,
        department_id=None,
        department_provided=False,
        idempotency_key="advance-version",
    )

    with pytest.raises(PlatformError) as replayed_error:
        service.update_managed_user(
            actor=admin,
            user_id=user["id"],
            expected_version=2,
            role=None,
            department_id=None,
            department_provided=False,
            idempotency_key="stale-update",
        )
    assert replayed_error.value.code == "version_conflict"


def test_failed_revocation_rolls_back_the_managed_user_transition() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    service = IdentityAccessService(
        engine,
        AuthSettings(secret_key="test-secret-that-is-long-enough"),
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
    user = service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    service.login(username="alice", password="Password1")

    with pytest.raises(PlatformError) as exc_info:
        service.update_managed_user(
            actor=admin,
            user_id=user["id"],
            expected_version=1,
            role="ops",
            department_id=None,
            department_provided=False,
            idempotency_key="user-update-1",
        )

    assert exc_info.value.code == "generation_revocation_unavailable"
    assert service.user_response(user["id"])["role"] == "user"


def test_identity_changes_and_pending_delete_invalidate_pending_submissions() -> None:
    class RecordingInvalidationPort:
        def __init__(self) -> None:
            self.commands: list[object] = []

        def invalidate_pending_submissions(self, command: object, *, connection: object) -> int:
            del connection
            self.commands.append(command)
            return 0

    port = RecordingInvalidationPort()
    service = make_service(submission_invalidation_port=port)
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
    first = service.create_department(actor=admin, name="First", idempotency_key="department-first")
    second = service.create_department(
        actor=admin, name="Second", idempotency_key="department-second"
    )
    user = service.create_managed_user(
        actor=admin,
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=first["id"],
        idempotency_key="user-create",
    )

    updated = service.update_managed_user(
        actor=admin,
        user_id=user["id"],
        expected_version=1,
        role=None,
        department_id=second["id"],
        department_provided=True,
        idempotency_key="user-move",
    )
    service.delete_managed_user(
        actor=admin,
        user_id=user["id"],
        expected_version=updated["version"],
        idempotency_key="user-delete",
    )

    assert [(command.lifecycle_status, command.department_id) for command in port.commands] == [
        ("active", second["id"]),
        ("pending_delete", second["id"]),
    ]


def test_space_authorization_distinguishes_unreadable_from_insufficient_access() -> None:
    service = make_service()
    alice = service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    bob = service.provision_user(
        username="bob",
        password="Password1",
        real_name="Bob",
        display_name="Bob",
        role="user",
        department_id=None,
    )
    alice_principal = service.authenticate_access_token(
        service.login(username="alice", password="Password1").access_token
    )

    assert (
        service.authorize_space(
            principal=alice_principal,
            space_id=f"personal:{alice['id']}",
            action="manage",
        )
        == "manage"
    )
    assert (
        service.authorize_space(
            principal=alice_principal,
            space_id="public",
            action="contribute",
        )
        == "contribute"
    )
    with pytest.raises(PlatformError) as insufficient:
        service.authorize_space(principal=alice_principal, space_id="public", action="manage")
    assert insufficient.value.code == "space_action_forbidden"

    with pytest.raises(PlatformError) as unreadable:
        service.authorize_space(
            principal=alice_principal,
            space_id=f"personal:{bob['id']}",
            action="read",
        )
    assert unreadable.value.code == "space_not_found"


def test_space_authorization_rechecks_the_principal_session() -> None:
    service = make_service()
    user = service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    login = service.login(username="alice", password="Password1")
    principal = service.authenticate_access_token(login.access_token)
    assert service.revoke_session(
        user_id=user["id"], session_id=login.session_id, reason="user_logout"
    )

    with pytest.raises(PlatformError) as exc_info:
        service.authorize_space(
            principal=principal,
            space_id=f"personal:{user['id']}",
            action="read",
        )

    assert exc_info.value.code == "session_revoked"


def test_department_deactivation_fails_closed_when_work_state_cannot_be_verified() -> None:
    class UnavailableWorkCheck:
        def inspect(self, department_id: str, *, connection: object) -> object:
            del department_id, connection
            raise RuntimeError("document service is unavailable")

    service = make_service(department_work_check=UnavailableWorkCheck())
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
    department = service.create_department(
        actor=admin,
        name="Finance",
        idempotency_key="department-create-1",
    )

    with pytest.raises(PlatformError) as exc_info:
        service.deactivate_department(
            actor=admin,
            department_id=department["id"],
            expected_version=1,
            idempotency_key="department-deactivate-1",
        )

    assert exc_info.value.code == "department_deactivation_unverified"
    assert exc_info.value.status_code == 503
    assert exc_info.value.details == {"retryable": True}


def test_department_deactivation_requires_an_explicit_work_check_adapter() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    service = IdentityAccessService(
        engine,
        AuthSettings(secret_key="test-secret-that-is-long-enough"),
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
    department = service.create_department(
        actor=admin,
        name="Finance",
        idempotency_key="department-create-1",
    )

    with pytest.raises(PlatformError) as exc_info:
        service.deactivate_department(
            actor=admin,
            department_id=department["id"],
            expected_version=1,
            idempotency_key="department-deactivate-1",
        )

    assert exc_info.value.code == "department_deactivation_unverified"


def test_department_deactivation_blocks_nonterminal_work() -> None:
    class ActiveWorkCheck:
        def inspect(self, department_id: str, *, connection: object) -> DepartmentWorkState:
            del department_id, connection
            return DepartmentWorkState(nonterminal_job_count=1, pending_submission_count=0)

    service = make_service(department_work_check=ActiveWorkCheck())
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
    department = service.create_department(
        actor=admin,
        name="Finance",
        idempotency_key="department-create-1",
    )

    with pytest.raises(PlatformError) as exc_info:
        service.deactivate_department(
            actor=admin,
            department_id=department["id"],
            expected_version=1,
            idempotency_key="department-deactivate-1",
        )

    assert exc_info.value.code == "department_has_active_work"
