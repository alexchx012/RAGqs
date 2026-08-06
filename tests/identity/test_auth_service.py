from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection
from sqlalchemy.pool import StaticPool

from app.identity.ports import NoopDepartmentWorkCheckPort
from app.identity.revocation import GenerationRevocationReceipt, NoopGenerationRevocationPort
from app.identity.schema import (
    auth_refresh_token_table,
    identity_idempotency_table,
    identity_metadata,
    identity_user_table,
)
from app.identity.service import IdentityAccessService
from app.platform.config import AuthSettings
from app.platform.database import core_metadata, platform_audit_table
from app.platform.errors import PlatformError
from app.platform.storage import MemoryObjectStore, StorageKeyError


def make_service() -> IdentityAccessService:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    return IdentityAccessService(
        engine,
        AuthSettings(secret_key="test-secret-that-is-long-enough"),
        revocation_port=NoopGenerationRevocationPort(),
        department_work_check=NoopDepartmentWorkCheckPort(),
    )


def test_login_issues_session_bound_access_and_refresh_tokens() -> None:
    service = make_service()
    user = service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )

    result = service.login(username="alice", password="Password1", device="Browser")
    principal = service.authenticate_access_token(result.access_token)

    assert result.user == user
    assert result.refresh_token
    assert principal.user_id == user["id"]
    assert principal.auth_session_id == result.session_id


def test_refresh_rotates_once_and_replays_the_same_successor_within_grace() -> None:
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
    )
    service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    login = service.login(username="alice", password="Password1", device="Browser")

    rotated = service.refresh(
        refresh_token=login.refresh_token,
        csrf_cookie=login.csrf_token,
        csrf_header=login.csrf_token,
        origin=None,
    )
    replayed = service.refresh(
        refresh_token=login.refresh_token,
        csrf_cookie=login.csrf_token,
        csrf_header=login.csrf_token,
        origin=None,
    )

    assert rotated.access_token == replayed.access_token
    assert rotated.refresh_token == replayed.refresh_token
    assert rotated.csrf_token == replayed.csrf_token
    assert (
        service.authenticate_access_token(rotated.access_token).auth_session_id == login.session_id
    )

    current += timedelta(seconds=6)
    with pytest.raises(PlatformError) as exc_info:
        service.refresh(
            refresh_token=login.refresh_token,
            csrf_cookie=login.csrf_token,
            csrf_header=login.csrf_token,
            origin=None,
        )

    assert exc_info.value.code == "refresh_reuse_detected"
    with pytest.raises(PlatformError, match="revoked"):
        service.authenticate_access_token(rotated.access_token)
    with engine.connect() as connection:
        replay_payload = (
            connection.execute(
                auth_refresh_token_table.select().where(
                    auth_refresh_token_table.c.auth_session_id == login.session_id,
                    auth_refresh_token_table.c.sequence == 1,
                )
            )
            .mappings()
            .one()["replay_payload"]
        )
    assert replay_payload is None


def test_session_revocation_calls_chat_port_once_and_invalidates_access_token() -> None:
    class RecordingRevocationPort:
        def __init__(self) -> None:
            self.commands = []

        def revoke(self, command, *, connection):
            del connection
            self.commands.append(command)
            return GenerationRevocationReceipt(reference="chat-receipt-1", state="accepted")

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    port = RecordingRevocationPort()
    service = IdentityAccessService(
        engine,
        AuthSettings(secret_key="test-secret-that-is-long-enough"),
        revocation_port=port,
    )
    user = service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    login = service.login(username="alice", password="Password1", device="Browser")

    assert service.revoke_session(
        user_id=user["id"],
        session_id=login.session_id,
        reason="user_logout",
    )
    assert not service.revoke_session(
        user_id=user["id"],
        session_id=login.session_id,
        reason="user_logout",
    )

    assert len(port.commands) == 1
    assert port.commands[0].user_id == user["id"]
    assert port.commands[0].auth_session_id == login.session_id
    assert port.commands[0].reason == "user_logout"
    with pytest.raises(PlatformError, match="revoked"):
        service.authenticate_access_token(login.access_token)


def test_all_session_revocation_emits_one_account_scoped_generation_command() -> None:
    class RecordingRevocationPort:
        def __init__(self) -> None:
            self.commands = []

        def revoke(self, command, *, connection):
            del connection
            self.commands.append(command)
            return GenerationRevocationReceipt(reference="chat-receipt-account", state="accepted")

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    port = RecordingRevocationPort()
    service = IdentityAccessService(
        engine,
        AuthSettings(secret_key="test-secret-that-is-long-enough"),
        revocation_port=port,
    )
    user = service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    login = service.login(username="alice", password="Password1", device="Browser")

    assert service.revoke_all_sessions(user_id=user["id"], reason="all_devices_revoked") == 1

    assert len(port.commands) == 1
    command = port.commands[0]
    assert command.user_id == user["id"]
    assert command.auth_session_id is None
    assert command.reason == "all_devices_revoked"
    assert command.identity_transition_version == 2
    with pytest.raises(PlatformError, match="revoked"):
        service.authenticate_access_token(login.access_token)


def test_session_revocation_fails_closed_without_a_generation_port() -> None:
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
    user = service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    login = service.login(username="alice", password="Password1")

    with pytest.raises(PlatformError) as exc_info:
        service.revoke_session(
            user_id=user["id"],
            session_id=login.session_id,
            reason="user_logout",
        )

    assert exc_info.value.code == "generation_revocation_unavailable"
    assert service.authenticate_access_token(login.access_token).user_id == user["id"]


def test_session_revocation_rejects_an_unverifiable_generation_receipt() -> None:
    class InvalidReceiptPort:
        def revoke(self, command, *, connection):
            del command, connection
            return GenerationRevocationReceipt(reference="", state="accepted")

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
        revocation_port=InvalidReceiptPort(),
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

    with pytest.raises(PlatformError) as exc_info:
        service.revoke_session(
            user_id=user["id"],
            session_id=login.session_id,
            reason="user_logout",
        )

    assert exc_info.value.code == "generation_revocation_unverified"
    assert service.authenticate_access_token(login.access_token).user_id == user["id"]


def test_refresh_reuse_of_a_non_predecessor_revokes_the_session_family() -> None:
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
    )
    service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    login = service.login(username="alice", password="Password1")
    first_rotation = service.refresh(
        refresh_token=login.refresh_token,
        csrf_cookie=login.csrf_token,
        csrf_header=login.csrf_token,
        origin=None,
    )
    service.refresh(
        refresh_token=first_rotation.refresh_token,
        csrf_cookie=first_rotation.csrf_token,
        csrf_header=first_rotation.csrf_token,
        origin=None,
    )

    with pytest.raises(PlatformError) as exc_info:
        service.refresh(
            refresh_token=login.refresh_token,
            csrf_cookie=login.csrf_token,
            csrf_header=login.csrf_token,
            origin=None,
        )

    assert exc_info.value.code == "refresh_reuse_detected"


def test_later_refresh_rotation_clears_expired_predecessor_replay_payload() -> None:
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
    )
    service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    login = service.login(username="alice", password="Password1")
    first_rotation = service.refresh(
        refresh_token=login.refresh_token,
        csrf_cookie=login.csrf_token,
        csrf_header=login.csrf_token,
        origin=None,
    )
    current += timedelta(seconds=6)
    service.refresh(
        refresh_token=first_rotation.refresh_token,
        csrf_cookie=first_rotation.csrf_token,
        csrf_header=first_rotation.csrf_token,
        origin=None,
    )

    with engine.connect() as connection:
        replay_payload = connection.execute(
            auth_refresh_token_table.select()
            .with_only_columns(auth_refresh_token_table.c.replay_payload)
            .where(
                auth_refresh_token_table.c.auth_session_id == login.session_id,
                auth_refresh_token_table.c.sequence == 1,
            )
        ).scalar_one_or_none()

    assert replay_payload is None


def test_session_list_marks_current_device_and_can_revoke_another_device() -> None:
    service = make_service()
    user = service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    current = service.login(username="alice", password="Password1", device="Desktop")
    other = service.login(username="alice", password="Password1", device="Phone")

    sessions = service.list_sessions(user_id=user["id"], current_session_id=current.session_id)

    assert {item["id"] for item in sessions} == {current.session_id, other.session_id}
    assert next(item for item in sessions if item["id"] == current.session_id)["current"] is True
    assert next(item for item in sessions if item["id"] == other.session_id)["current"] is False
    assert service.revoke_session(
        user_id=user["id"], session_id=other.session_id, reason="device_revoked"
    )
    with pytest.raises(PlatformError, match="revoked"):
        service.authenticate_access_token(other.access_token)
    assert service.authenticate_access_token(current.access_token).user_id == user["id"]


def test_profile_and_preferences_are_desired_state_replacements() -> None:
    service = make_service()
    user = service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )

    updated = service.update_profile(user_id=user["id"], display_name="Alice Smith")
    preferences = service.replace_preferences(
        user_id=user["id"],
        preferences={"theme": "dark", "chat_font_size": "large", "ab_opt_out": True},
    )

    assert updated["display_name"] == "Alice Smith"
    assert preferences == {"theme": "dark", "chat_font_size": "large", "ab_opt_out": True}
    assert service.get_preferences(user_id=user["id"]) == preferences


def test_password_change_revokes_all_existing_sessions() -> None:
    service = make_service()
    user = service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    first = service.login(username="alice", password="Password1", device="Desktop")
    second = service.login(username="alice", password="Password1", device="Phone")

    service.change_password(user_id=user["id"], old_password="Password1", new_password="Different2")

    with pytest.raises(PlatformError, match="revoked"):
        service.authenticate_access_token(first.access_token)
    with pytest.raises(PlatformError, match="revoked"):
        service.authenticate_access_token(second.access_token)
    with pytest.raises(PlatformError) as exc_info:
        service.login(username="alice", password="Password1")
    assert exc_info.value.code == "invalid_credentials"
    assert service.login(username="alice", password="Different2").user["id"] == user["id"]


def test_login_throttles_after_repeated_invalid_credentials() -> None:
    service = make_service()
    service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )

    for _ in range(4):
        with pytest.raises(PlatformError) as exc_info:
            service.login(username="alice", password="WrongPassword1")
        assert exc_info.value.code == "invalid_credentials"

    with pytest.raises(PlatformError) as exc_info:
        service.login(username="alice", password="WrongPassword1")

    assert exc_info.value.code == "too_many_attempts"
    assert exc_info.value.details["retry_after_seconds"] > 0


def test_avatar_replacement_uses_the_shared_object_store() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    object_store = MemoryObjectStore()
    service = IdentityAccessService(
        engine,
        AuthSettings(secret_key="test-secret-that-is-long-enough"),
        object_store=object_store,
    )
    user = service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )

    avatar = service.replace_avatar(
        user_id=user["id"], content=b"fake-png", content_type="image/png"
    )

    assert avatar["avatar_url"].startswith("object://avatars/")
    assert service.user_response(user["id"])["avatar_url"] == avatar["avatar_url"]


def test_avatar_replacement_compensates_when_deletion_wins_the_write_race(monkeypatch) -> None:
    class RecordingObjectStore(MemoryObjectStore):
        last_key: str | None = None

        def put(self, key, content, metadata) -> None:
            self.last_key = key
            super().put(key, content, metadata)

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    object_store = RecordingObjectStore()
    service = IdentityAccessService(
        engine,
        AuthSettings(secret_key="test-secret-that-is-long-enough"),
        object_store=object_store,
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
    captured_update = None

    class NoRowsResult:
        rowcount = 0

    def interleaved_execute(connection, statement, *args, **kwargs):
        nonlocal captured_update
        if getattr(statement, "table", None) is identity_user_table:
            captured_update = statement
            return NoRowsResult()
        return original_execute(connection, statement, *args, **kwargs)

    monkeypatch.setattr(Connection, "execute", interleaved_execute)

    with pytest.raises(PlatformError) as exc_info:
        service.replace_avatar(user_id=user["id"], content=b"fake-png", content_type="image/png")

    assert exc_info.value.code == "authentication_required"
    assert captured_update is not None
    assert "lifecycle_status" in str(captured_update)
    assert object_store.last_key is not None
    with pytest.raises(StorageKeyError):
        object_store.get(object_store.last_key)
    with engine.connect() as connection:
        user_record = (
            connection.execute(
                identity_user_table.select().where(identity_user_table.c.id == user["id"])
            )
            .mappings()
            .one()
        )
    assert user_record["avatar_url"] is None


def test_avatar_compensation_defers_a_failed_delete_for_retry(monkeypatch) -> None:
    class FailFirstDeleteObjectStore(MemoryObjectStore):
        last_key: str | None = None
        fail_deletes = 1

        def put(self, key, content, metadata) -> None:
            self.last_key = key
            super().put(key, content, metadata)

        def delete(self, key) -> None:
            if self.fail_deletes:
                self.fail_deletes -= 1
                raise StorageKeyError(key)
            super().delete(key)

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    object_store = FailFirstDeleteObjectStore()
    service = IdentityAccessService(
        engine,
        AuthSettings(secret_key="test-secret-that-is-long-enough"),
        object_store=object_store,
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

    class NoRowsResult:
        rowcount = 0

    def rejected_avatar_update(connection, statement, *args, **kwargs):
        if getattr(statement, "table", None) is identity_user_table:
            return NoRowsResult()
        return original_execute(connection, statement, *args, **kwargs)

    monkeypatch.setattr(Connection, "execute", rejected_avatar_update)

    with pytest.raises(PlatformError) as exc_info:
        service.replace_avatar(user_id=user["id"], content=b"fake-png", content_type="image/png")

    assert exc_info.value.code == "authentication_required"
    assert object_store.last_key is not None
    pending = service.list_pending_object_cleanup_operations()
    assert len(pending) == 1
    assert service.finalize_object_cleanup(operation_id=pending[0])["status"] == "completed"
    with pytest.raises(StorageKeyError):
        object_store.get(object_store.last_key)


def test_deployment_admin_provisioning_honors_the_declared_roster() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    service = IdentityAccessService(
        engine,
        AuthSettings(
            secret_key="test-secret-that-is-long-enough",
            admin_roster=("admin",),
        ),
    )

    service.provision_user(
        username="admin",
        password="Password1",
        real_name="Admin",
        display_name="Admin",
        role="admin",
        department_id=None,
    )
    with pytest.raises(PlatformError) as exc_info:
        service.provision_user(
            username="rogue",
            password="Password1",
            real_name="Rogue",
            display_name="Rogue",
            role="admin",
            department_id=None,
        )

    assert exc_info.value.code == "forbidden_target"


def test_bootstrap_initial_admin_creates_an_audited_rostered_admin_once() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    service = IdentityAccessService(
        engine,
        AuthSettings(
            secret_key="test-secret-that-is-long-enough",
            admin_roster=("admin",),
        ),
    )

    created = service.bootstrap_initial_admin(
        username="admin",
        password="Password1",
        real_name="Initial Admin",
        display_name="Admin",
    )
    repeated = service.bootstrap_initial_admin(
        username="admin",
        password="Password1",
        real_name="Initial Admin",
        display_name="Admin",
    )

    assert created["id"] == repeated["id"]
    assert created["role"] == "admin"
    with engine.connect() as connection:
        audits = connection.execute(platform_audit_table.select()).mappings().all()
    assert [(audit["actor_id"], audit["result"]) for audit in audits] == [
        ("system:admin-bootstrap", "admin_bootstrapped")
    ]


def test_bootstrap_initial_admin_refuses_a_nonempty_identity_database() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    service = IdentityAccessService(
        engine,
        AuthSettings(
            secret_key="test-secret-that-is-long-enough",
            admin_roster=("admin",),
        ),
    )
    service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )

    with pytest.raises(PlatformError) as exc_info:
        service.bootstrap_initial_admin(
            username="admin",
            password="Password1",
            real_name="Initial Admin",
            display_name="Admin",
        )

    assert exc_info.value.code == "admin_bootstrap_conflict"


def test_bootstrap_initial_admin_requires_a_declared_roster_seat() -> None:
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

    with pytest.raises(PlatformError) as exc_info:
        service.bootstrap_initial_admin(
            username="admin",
            password="Password1",
            real_name="Initial Admin",
            display_name="Admin",
        )

    assert exc_info.value.code == "admin_roster_invalid"


def test_roster_reconciliation_freezes_removed_admins_and_revokes_sessions() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    secret = "test-secret-that-is-long-enough"
    first_deployment = IdentityAccessService(
        engine,
        AuthSettings(secret_key=secret, admin_roster=("retained", "removed")),
        revocation_port=NoopGenerationRevocationPort(),
    )
    first_deployment.provision_user(
        username="retained",
        password="Password1",
        real_name="Retained",
        display_name="Retained",
        role="admin",
        department_id=None,
    )
    removed = first_deployment.provision_user(
        username="removed",
        password="Password1",
        real_name="Removed",
        display_name="Removed",
        role="admin",
        department_id=None,
    )
    login = first_deployment.login(username="removed", password="Password1")
    after_deployment_change = IdentityAccessService(
        engine,
        AuthSettings(secret_key=secret, admin_roster=("retained",)),
        revocation_port=NoopGenerationRevocationPort(),
    )

    reconciled = after_deployment_change.reconcile_admin_roster()

    assert reconciled == [removed["id"]]
    with pytest.raises(PlatformError) as exc_info:
        after_deployment_change.authenticate_access_token(login.access_token)
    assert exc_info.value.code == "session_revoked"
    with pytest.raises(PlatformError) as exc_info:
        after_deployment_change.login(username="removed", password="Password1")
    assert exc_info.value.code == "invalid_credentials"


def test_roster_reconciliation_rejects_an_empty_active_admin_seat() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    service = IdentityAccessService(
        engine,
        AuthSettings(
            secret_key="test-secret-that-is-long-enough",
            admin_roster=("admin",),
        ),
    )

    with pytest.raises(PlatformError) as exc_info:
        service.reconcile_admin_roster()

    assert exc_info.value.code == "admin_roster_invalid"


def test_idempotency_request_hash_is_bound_to_the_service_secret() -> None:
    def create_request_hash(secret: str) -> str:
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        core_metadata.create_all(engine)
        identity_metadata.create_all(engine)
        service = IdentityAccessService(engine, AuthSettings(secret_key=secret))
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
        with engine.connect() as connection:
            return str(
                connection.execute(
                    identity_idempotency_table.select().with_only_columns(
                        identity_idempotency_table.c.request_hash
                    )
                ).scalar_one()
            )

    assert create_request_hash("first-secret-that-is-long-enough") != create_request_hash(
        "second-secret-that-is-long-enough"
    )


def test_user_response_includes_department_name_and_directory_last_activity() -> None:
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
    user = service.create_managed_user(
        actor=admin_principal,
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=department["id"],
        idempotency_key="user-create-1",
    )
    service.login(username="alice", password="Password1")

    listed = service.list_managed_users(actor=admin_principal)

    assert user["department"] == {"id": department["id"], "name": "Finance"}
    assert service.user_response(user["id"])["department"] == {
        "id": department["id"],
        "name": "Finance",
    }
    assert (
        next(item for item in listed["items"] if item["id"] == user["id"])["last_active_at"]
        is not None
    )


def test_configured_refresh_origin_allowlist_rejects_missing_and_foreign_origins() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    service = IdentityAccessService(
        engine,
        AuthSettings(
            secret_key="test-secret-that-is-long-enough",
            allowed_origins=("https://app.example.test",),
        ),
    )
    service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    login = service.login(username="alice", password="Password1")

    for origin in (None, "https://evil.example.test"):
        with pytest.raises(PlatformError) as exc_info:
            service.refresh(
                refresh_token=login.refresh_token,
                csrf_cookie=login.csrf_token,
                csrf_header=login.csrf_token,
                origin=origin,
            )
        assert exc_info.value.code == "csrf_failed"
