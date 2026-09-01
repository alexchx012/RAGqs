from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.engine import Connection
from sqlalchemy.pool import StaticPool

from app.documents.schema import documents_metadata, documents_table
from app.identity.ports import NoopDepartmentWorkCheckPort
from app.identity.revocation import GenerationRevocationReceipt, NoopGenerationRevocationPort
from app.identity.schema import (
    auth_session_table,
    identity_department_table,
    identity_login_throttle_table,
    identity_metadata,
    identity_space_table,
    identity_user_table,
)
from app.identity.service import IdentityAccessService
from app.outbox.schema import outbox_metadata
from app.platform.config import AuthSettings
from app.platform.database import core_metadata
from app.platform.errors import PlatformError


def make_service(*, settings: AuthSettings | None = None) -> IdentityAccessService:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    documents_metadata.create_all(engine)
    return IdentityAccessService(
        engine,
        settings or AuthSettings(secret_key="test-secret-that-is-long-enough"),
        revocation_port=NoopGenerationRevocationPort(),
        department_work_check=NoopDepartmentWorkCheckPort(),
    )


def create_admin(service: IdentityAccessService):
    service.provision_user(
        username="admin",
        password="Password1",
        real_name="Admin",
        display_name="Admin",
        role="admin",
        department_id=None,
    )
    return service.authenticate_access_token(
        service.login(username="admin", password="Password1").access_token
    )


def test_list_spaces_ignores_a_concurrent_public_space_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = make_service()
    principal = create_admin(service)
    original_execute = Connection.execute
    raced = False

    def execute_after_racing_request(
        connection: Connection,
        statement: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        nonlocal raced
        if not raced and getattr(statement, "table", None) is identity_space_table:
            raced = True
            original_execute(
                connection,
                identity_space_table.insert().values(
                    id="public",
                    kind="public",
                    name="Public knowledge space",
                    owner_user_id=None,
                    department_id=None,
                    created_at_utc=datetime.now(UTC),
                ),
            )
        return original_execute(connection, statement, *args, **kwargs)

    now = datetime.now(UTC)
    with service._engine.begin() as connection:
        for document_id, lifecycle_status in (
            ("doc_public_active", "active"),
            ("doc_public_deleted", "deleted"),
        ):
            connection.execute(
                documents_table.insert().values(
                    id=document_id,
                    space_id="public",
                    lifecycle_status=lifecycle_status,
                    active_version_id=None,
                    pending_version_id=None,
                    active_operation_job_id=None,
                    deletion_id=None,
                    version=1,
                    name="Document",
                    normalized_name="document",
                    media_kind="text/plain",
                    created_by_user_id=str(principal.user_id),
                    uploaded_at_utc=now,
                    created_at_utc=now,
                    updated_at_utc=now,
                )
            )

    monkeypatch.setattr(Connection, "execute", execute_after_racing_request)

    spaces = service.list_spaces(principal=principal, with_document_counts=True)

    assert {
        "id": "public",
        "kind": "public",
        "name": "Public knowledge space",
        "permission": "manage",
        "document_count": 1,
    } in spaces


def test_session_transition_fence_rejects_stale_access_and_refresh_tokens() -> None:
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

    with service._engine.begin() as connection:
        connection.execute(
            update(identity_user_table)
            .where(identity_user_table.c.id == user["id"])
            .values(transition_version=2)
        )

    with pytest.raises(PlatformError) as access_error:
        service.authenticate_access_token(login.access_token)
    assert access_error.value.code == "session_revoked"
    with pytest.raises(PlatformError) as refresh_error:
        service.refresh(
            refresh_token=login.refresh_token,
            csrf_cookie=login.csrf_token,
            csrf_header=login.csrf_token,
            origin=None,
        )
    assert refresh_error.value.code == "invalid_refresh"
    assert service.authenticate_session_action_token(login.access_token).session_revoked is True
    with pytest.raises(PlatformError) as acl_error:
        service.list_spaces(principal=principal)
    assert acl_error.value.code == "session_revoked"


def test_managed_user_update_rejects_a_version_changed_between_read_and_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = make_service()
    admin = create_admin(service)
    user = service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    original_execute = Connection.execute
    injected = False

    def execute_with_competing_update(
        connection: Connection,
        statement: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        nonlocal injected
        if not injected and getattr(statement, "table", None) is identity_user_table:
            injected = True
            original_execute(
                connection,
                update(identity_user_table)
                .where(identity_user_table.c.id == user["id"])
                .values(version=2),
            )
        return original_execute(connection, statement, *args, **kwargs)

    monkeypatch.setattr(Connection, "execute", execute_with_competing_update)

    with pytest.raises(PlatformError) as exc_info:
        service.update_managed_user(
            actor=admin,
            user_id=user["id"],
            expected_version=1,
            role=None,
            department_id=None,
            department_provided=False,
            idempotency_key="concurrent-user-update",
        )

    assert injected
    assert exc_info.value.code == "version_conflict"


def test_department_assignment_rechecks_that_the_department_is_still_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = make_service()
    admin = create_admin(service)
    department = service.create_department(
        actor=admin,
        name="Finance",
        idempotency_key="department-create",
    )
    user = service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    original_execute = Connection.execute
    injected = False

    def execute_with_department_deactivation(
        connection: Connection,
        statement: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        nonlocal injected
        if not injected and getattr(statement, "table", None) is identity_user_table:
            injected = True
            original_execute(
                connection,
                update(identity_department_table)
                .where(identity_department_table.c.id == department["id"])
                .values(status="inactive"),
            )
        return original_execute(connection, statement, *args, **kwargs)

    monkeypatch.setattr(Connection, "execute", execute_with_department_deactivation)

    with pytest.raises(PlatformError) as exc_info:
        service.update_managed_user(
            actor=admin,
            user_id=user["id"],
            expected_version=1,
            role=None,
            department_id=department["id"],
            department_provided=True,
            idempotency_key="assign-department",
        )

    assert injected
    assert exc_info.value.code == "version_conflict"


def test_session_revocation_claim_prevents_duplicate_delivery_during_reentry() -> None:
    class ReentrantRevocationPort:
        def __init__(self) -> None:
            self.service: IdentityAccessService | None = None
            self.calls = 0
            self.reentered = False

        def revoke(self, command: Any, *, connection: Connection) -> GenerationRevocationReceipt:
            self.calls += 1
            if not self.reentered:
                self.reentered = True
                assert self.service is not None
                self.service._revoke_session_in_transaction(
                    connection,
                    session={
                        "id": command.auth_session_id,
                        "user_id": command.user_id,
                        "revoked_at_utc": None,
                        "identity_transition_version": command.identity_transition_version,
                    },
                    reason=command.reason,
                    revoked_at=command.revoked_at,
                )
            return GenerationRevocationReceipt(reference="chat-receipt", state="accepted")

    port = ReentrantRevocationPort()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    service = IdentityAccessService(
        engine,
        AuthSettings(secret_key="test-secret-that-is-long-enough"),
        revocation_port=port,
        department_work_check=NoopDepartmentWorkCheckPort(),
    )
    port.service = service
    user = service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    login = service.login(username="alice", password="Password1")

    assert service.revoke_session(
        user_id=user["id"], session_id=login.session_id, reason="user_logout"
    )
    assert port.calls == 1
    with engine.connect() as connection:
        assert (
            connection.execute(
                auth_session_table.select()
                .with_only_columns(auth_session_table.c.revoked_at_utc)
                .where(auth_session_table.c.id == login.session_id)
            ).scalar_one()
            is not None
        )


def test_failed_login_count_retries_after_an_interleaved_increment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = make_service(
        settings=AuthSettings(
            secret_key="test-secret-that-is-long-enough",
            login_max_attempts=5,
        )
    )
    service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    with pytest.raises(PlatformError):
        service.login(username="alice", password="WrongPassword1")

    original_execute = Connection.execute
    injected = False

    def execute_with_competing_increment(
        connection: Connection,
        statement: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        nonlocal injected
        if not injected and getattr(statement, "table", None) is identity_login_throttle_table:
            injected = True
            original_execute(
                connection,
                update(identity_login_throttle_table)
                .where(identity_login_throttle_table.c.normalized_username == "alice")
                .values(failed_attempts=2),
            )
        return original_execute(connection, statement, *args, **kwargs)

    monkeypatch.setattr(Connection, "execute", execute_with_competing_increment)

    with pytest.raises(PlatformError) as exc_info:
        service.login(username="alice", password="WrongPassword1")

    assert injected
    assert exc_info.value.code == "invalid_credentials"
    with service._engine.connect() as connection:
        attempts = connection.execute(
            identity_login_throttle_table.select()
            .with_only_columns(identity_login_throttle_table.c.failed_attempts)
            .where(identity_login_throttle_table.c.normalized_username == "alice")
        ).scalar_one()
    assert attempts == 3


def test_department_write_race_reports_the_reloaded_current_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = make_service()
    admin = create_admin(service)
    department = service.create_department(
        actor=admin, name="Finance", idempotency_key="department-create"
    )
    original_execute = Connection.execute
    injected = False

    def execute_with_competing_version_bump(
        connection: Connection,
        statement: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        nonlocal injected
        # 初始版本检查之后、最终乐观更新执行前，竞争事务先推进了部门版本。
        if not injected and getattr(statement, "table", None) is identity_department_table:
            injected = True
            original_execute(
                connection,
                update(identity_department_table)
                .where(identity_department_table.c.id == department["id"])
                .values(version=2),
            )
        return original_execute(connection, statement, *args, **kwargs)

    monkeypatch.setattr(Connection, "execute", execute_with_competing_version_bump)

    with pytest.raises(PlatformError) as exc_info:
        service.rename_department(
            actor=admin,
            department_id=str(department["id"]),
            expected_version=1,
            name="Finance Renamed",
            idempotency_key="department-rename-raced",
        )

    assert injected
    assert exc_info.value.code == "version_conflict"
    assert exc_info.value.details == {"current_version": 2}
