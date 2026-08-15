from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.pool import StaticPool

from app.identity.ports import NoopDepartmentWorkCheckPort
from app.identity.revocation import GenerationRevocationReceipt, NoopGenerationRevocationPort
from app.identity.schema import (
    auth_refresh_token_table,
    identity_idempotency_table,
    identity_login_throttle_table,
    identity_metadata,
    identity_object_cleanup_table,
    identity_revocation_command_table,
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


def _legacy_b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _legacy_replay_payload(secret: bytes, payload: dict[str, str]) -> str:
    nonce = b"legacy-replay-v1"
    plaintext = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    stream = b""
    counter = 0
    while len(stream) < len(plaintext):
        stream += hmac.new(
            secret,
            nonce + counter.to_bytes(4, "big"),
            hashlib.sha256,
        ).digest()
        counter += 1
    ciphertext = bytes(
        left ^ right for left, right in zip(plaintext, stream[: len(plaintext)], strict=True)
    )
    tag = hmac.new(secret, nonce + ciphertext, hashlib.sha256).digest()
    return ".".join((_legacy_b64(nonce), _legacy_b64(ciphertext), _legacy_b64(tag)))


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

    assert exc_info.value.code == "invalid_refresh"
    assert (
        service.authenticate_access_token(rotated.access_token).auth_session_id == login.session_id
    )
    assert (
        service.refresh(
            refresh_token=rotated.refresh_token,
            csrf_cookie=rotated.csrf_token,
            csrf_header=rotated.csrf_token,
            origin=None,
        ).session_id
        == login.session_id
    )


def test_refresh_replays_a_legacy_predecessor_payload_during_rolling_upgrade() -> None:
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
    with engine.begin() as connection:
        connection.execute(
            auth_refresh_token_table.update()
            .where(
                auth_refresh_token_table.c.auth_session_id == login.session_id,
                auth_refresh_token_table.c.sequence == 1,
            )
            .values(
                replay_payload=_legacy_replay_payload(
                    b"test-secret-that-is-long-enough",
                    {
                        "access_token": rotated.access_token,
                        "refresh_token": rotated.refresh_token,
                        "csrf_token": rotated.csrf_token,
                    },
                )
            )
        )

    replayed = service.refresh(
        refresh_token=login.refresh_token,
        csrf_cookie=login.csrf_token,
        csrf_header=login.csrf_token,
        origin=None,
    )

    assert replayed == rotated


def test_tampered_predecessor_replay_payload_is_invalid_without_revocation() -> None:
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
    with engine.begin() as connection:
        connection.execute(
            auth_refresh_token_table.update()
            .where(
                auth_refresh_token_table.c.auth_session_id == login.session_id,
                auth_refresh_token_table.c.sequence == 1,
            )
            .values(replay_payload="invalid-payload")
        )

    with pytest.raises(PlatformError) as exc_info:
        service.refresh(
            refresh_token=login.refresh_token,
            csrf_cookie=login.csrf_token,
            csrf_header=login.csrf_token,
            origin=None,
        )

    assert exc_info.value.code == "invalid_refresh"
    assert (
        service.authenticate_access_token(rotated.access_token).auth_session_id == login.session_id
    )


def test_unconfigured_development_services_do_not_share_auth_secrets() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    first = IdentityAccessService(
        engine,
        AuthSettings(),
        revocation_port=NoopGenerationRevocationPort(),
    )
    second = IdentityAccessService(
        engine,
        AuthSettings(),
        revocation_port=NoopGenerationRevocationPort(),
    )
    user = first.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    token = first.login(username="alice", password="Password1").access_token

    assert first.authenticate_access_token(token).user_id == user["id"]
    with pytest.raises(PlatformError) as exc_info:
        second.authenticate_access_token(token)
    assert exc_info.value.code == "authentication_required"


def test_unconfigured_development_restart_replays_identity_idempotency() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    first = IdentityAccessService(
        engine,
        AuthSettings(),
        revocation_port=NoopGenerationRevocationPort(),
    )
    first.provision_user(
        username="admin",
        password="Password1",
        real_name="Admin",
        display_name="Admin",
        role="admin",
        department_id=None,
    )
    first_admin = first.authenticate_access_token(
        first.login(username="admin", password="Password1").access_token
    )
    created = first.create_managed_user(
        actor=first_admin,
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
        idempotency_key="restart-user-create",
    )
    restarted = IdentityAccessService(
        engine,
        AuthSettings(),
        revocation_port=NoopGenerationRevocationPort(),
    )
    restarted_admin = restarted.authenticate_access_token(
        restarted.login(username="admin", password="Password1").access_token
    )

    replayed = restarted.create_managed_user(
        actor=restarted_admin,
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
        idempotency_key="restart-user-create",
    )

    assert replayed == created


def test_unconfigured_development_replays_legacy_idempotency_hash() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    service = IdentityAccessService(
        engine,
        AuthSettings(),
        revocation_port=NoopGenerationRevocationPort(),
    )
    admin = service.provision_user(
        username="admin",
        password="Password1",
        real_name="Admin",
        display_name="Admin",
        role="admin",
        department_id=None,
    )
    principal = service.authenticate_access_token(
        service.login(username="admin", password="Password1").access_token
    )
    initial_password_fingerprint = hmac.new(
        b"ragqs-development-auth-secret",
        b"initial-password\x00Password1",
        hashlib.sha256,
    ).hexdigest()
    payload = {
        "username": "alice",
        "real_name": "Alice",
        "display_name": "Alice",
        "role": "user",
        "department_id": None,
        "initial_password_fingerprint": initial_password_fingerprint,
    }
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    request_hash = hmac.new(
        b"ragqs-development-auth-secret",
        b"identity-idempotency-v1\x00" + encoded.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    expected = {"id": "user_legacy", "username": "alice"}
    with engine.begin() as connection:
        connection.execute(
            identity_idempotency_table.insert().values(
                actor_id=admin["id"],
                endpoint="POST:/admin/users",
                target_id="",
                idempotency_key="legacy-user-create",
                request_hash=request_hash,
                completed=True,
                response_json=expected,
                created_at_utc=datetime.now(UTC),
            )
        )

    replayed = service.create_managed_user(
        actor=principal,
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
        idempotency_key="legacy-user-create",
    )

    assert replayed == expected


def test_locked_login_does_not_verify_password(monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service()
    service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    now = datetime.now(UTC)
    with service._engine.begin() as connection:
        connection.execute(
            identity_login_throttle_table.insert().values(
                normalized_username="alice",
                failed_attempts=5,
                locked_until_utc=now + timedelta(minutes=1),
                updated_at_utc=now,
            )
        )
    monkeypatch.setattr(
        "app.identity.service.verify_password",
        lambda *_args: pytest.fail("locked login must not verify a password"),
    )

    with pytest.raises(PlatformError) as exc_info:
        service.login(username="alice", password="Password1")

    assert exc_info.value.code == "too_many_attempts"


def test_invalid_login_verifies_password_for_missing_and_inactive_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = make_service()
    user = service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    with service._engine.begin() as connection:
        connection.execute(
            update(identity_user_table)
            .where(identity_user_table.c.id == user["id"])
            .values(lifecycle_status="pending_delete")
        )
    verified_hashes: list[str] = []

    def record_verification(_password: str, encoded: str) -> bool:
        verified_hashes.append(encoded)
        return False

    monkeypatch.setattr("app.identity.service.verify_password", record_verification)

    for username in ("missing", "alice"):
        with pytest.raises(PlatformError) as exc_info:
            service.login(username=username, password="WrongPassword1")
        assert exc_info.value.code == "invalid_credentials"

    assert len(verified_hashes) == 2


def test_login_samples_time_after_throttle_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service()
    user = service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    initial = datetime(2026, 8, 6, tzinfo=UTC)
    clock = {"now": initial}
    with service._engine.begin() as connection:
        connection.execute(
            identity_login_throttle_table.insert().values(
                normalized_username="alice",
                failed_attempts=5,
                locked_until_utc=initial + timedelta(seconds=1),
                updated_at_utc=initial,
            )
        )
    original_throttle_record = service._login_throttle_record

    def advance_clock_after_lock(connection, normalized_username):
        throttle = original_throttle_record(connection, normalized_username)
        clock["now"] = initial + timedelta(seconds=2)
        return throttle

    monkeypatch.setattr(service, "_login_throttle_record", advance_clock_after_lock)
    monkeypatch.setattr(service, "_current_time", lambda: clock["now"])

    result = service.login(username="alice", password="Password1")

    assert result.user["id"] == user["id"]


def test_managed_users_database_query_applies_pagination() -> None:
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
        department_work_check=NoopDepartmentWorkCheckPort(),
    )
    admin = service.provision_user(
        username="admin",
        password="Password1",
        real_name="Admin",
        display_name="Admin",
        role="admin",
        department_id=None,
    )
    service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    service.provision_user(
        username="bob",
        password="Password1",
        real_name="Bob",
        display_name="Bob",
        role="user",
        department_id=None,
    )
    principal = service.authenticate_access_token(
        service.login(username="admin", password="Password1").access_token
    )
    assert principal.user_id == admin["id"]
    statements: list[str] = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _executemany):
        if "identity_user" in statement.casefold():
            statements.append(statement.casefold())

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        result = service.list_managed_users(actor=principal, page=2, page_size=1)
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)

    assert result["total"] == 3
    assert [item["username"] for item in result["items"]] == ["alice"]
    assert any("limit" in statement and "offset" in statement for statement in statements)


def test_managed_user_search_preserves_literal_and_cross_field_matching() -> None:
    service = make_service()
    admin = service.provision_user(
        username="admin",
        password="Password1",
        real_name="Admin",
        display_name="Admin",
        role="admin",
        department_id=None,
    )
    principal = service.authenticate_access_token(
        service.login(username="admin", password="Password1").access_token
    )
    assert principal.user_id == admin["id"]
    department = service.create_department(
        actor=principal,
        name="Finance",
        idempotency_key="department-finance",
    )
    service.provision_user(
        username="percent%user",
        password="Password1",
        real_name="Percent",
        display_name="Percent",
        role="user",
        department_id=None,
    )
    service.provision_user(
        username="under_score",
        password="Password1",
        real_name="Underscore",
        display_name="Underscore",
        role="user",
        department_id=None,
    )
    target = service.provision_user(
        username="directory-target",
        password="Password1",
        real_name="Directory",
        display_name="Target",
        role="user",
        department_id=department["id"],
    )

    percent_matches = service.list_managed_users(actor=principal, q="%")
    underscore_matches = service.list_managed_users(actor=principal, q="_")
    cross_field_matches = service.list_managed_users(actor=principal, q="user finance")

    assert [item["username"] for item in percent_matches["items"]] == ["percent%user"]
    assert [item["username"] for item in underscore_matches["items"]] == ["under_score"]
    assert [item["id"] for item in cross_field_matches["items"]] == [target["id"]]


def test_managed_user_search_preserves_unicode_casefold_semantics() -> None:
    service = make_service()
    admin = service.provision_user(
        username="admin",
        password="Password1",
        real_name="Admin",
        display_name="Admin",
        role="admin",
        department_id=None,
    )
    principal = service.authenticate_access_token(
        service.login(username="admin", password="Password1").access_token
    )
    target = service.provision_user(
        username="maria",
        password="Password1",
        real_name="Straße",
        display_name="Maria",
        role="user",
        department_id=None,
    )

    matches = service.list_managed_users(actor=principal, q="STRASSE")

    assert admin["id"] not in [item["id"] for item in matches["items"]]
    assert [item["id"] for item in matches["items"]] == [target["id"]]


def test_directory_search_backfills_mixed_version_user_rows() -> None:
    service = make_service()
    admin = service.provision_user(
        username="admin",
        password="Password1",
        real_name="Admin",
        display_name="Admin",
        role="admin",
        department_id=None,
    )
    principal = service.authenticate_access_token(
        service.login(username="admin", password="Password1").access_token
    )
    now = datetime.now(UTC)
    with service._engine.begin() as connection:
        connection.execute(
            identity_user_table.insert().values(
                id="user_legacy",
                username="maria",
                normalized_username="maria",
                password_hash="hash",
                real_name="Straße",
                display_name="Maria",
                department_id=None,
                role="user",
                lifecycle_status="active",
                version=1,
                avatar_url=None,
                preferences_json={},
                transition_version=1,
                created_at_utc=now,
                updated_at_utc=now,
                deletion_requested_at_utc=None,
                purge_after_at_utc=None,
            )
        )

    matches = service.list_managed_users(actor=principal, q="STRASSE")

    assert admin["id"] not in [item["id"] for item in matches["items"]]
    assert [item["id"] for item in matches["items"]] == ["user_legacy"]
    assert matches["total"] == 1
    with service._engine.connect() as connection:
        assert (
            connection.execute(
                select(identity_user_table.c.directory_search_text).where(
                    identity_user_table.c.id == "user_legacy"
                )
            ).scalar_one()
            == "maria strasse maria user"
        )


def test_departments_database_query_aggregates_member_counts() -> None:
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
    principal = service.authenticate_access_token(
        service.login(username="admin", password="Password1").access_token
    )
    finance = service.create_department(
        actor=principal,
        name="Finance",
        idempotency_key="finance-create",
    )
    service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=finance["id"],
    )
    statements: list[str] = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _executemany):
        if "identity_department" in statement.casefold():
            statements.append(statement.casefold())

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        result = service.list_departments(actor=principal, status="active")
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)

    assert [(item["name"], item["member_count"]) for item in result] == [("Finance", 1)]
    assert any("group by" in statement for statement in statements)


def test_prune_completed_history_keeps_recent_and_incomplete_records() -> None:
    current = datetime(2026, 8, 15, tzinfo=UTC)
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
    user = service.provision_user(
        username="alice",
        password="Password1",
        real_name="Alice",
        display_name="Alice",
        role="user",
        department_id=None,
    )
    old = current - timedelta(days=91)
    recent = current - timedelta(days=89)
    with engine.begin() as connection:
        connection.execute(
            identity_idempotency_table.insert(),
            [
                {
                    "actor_id": user["id"],
                    "endpoint": "users",
                    "target_id": user["id"],
                    "idempotency_key": "old-completed",
                    "request_hash": "hash-old-completed",
                    "completed": True,
                    "response_json": {"status": "ok"},
                    "created_at_utc": old,
                },
                {
                    "actor_id": user["id"],
                    "endpoint": "users",
                    "target_id": user["id"],
                    "idempotency_key": "recent-completed",
                    "request_hash": "hash-recent-completed",
                    "completed": True,
                    "response_json": {"status": "ok"},
                    "created_at_utc": recent,
                },
                {
                    "actor_id": user["id"],
                    "endpoint": "users",
                    "target_id": user["id"],
                    "idempotency_key": "old-pending",
                    "request_hash": "hash-old-pending",
                    "completed": False,
                    "response_json": None,
                    "created_at_utc": old,
                },
            ],
        )
        connection.execute(
            identity_revocation_command_table.insert(),
            [
                {
                    "operation_id": "old-completed",
                    "user_id": user["id"],
                    "auth_session_id": None,
                    "reason": "test",
                    "identity_transition_version": 1,
                    "receipt_reference": "receipt-old-completed",
                    "receipt_state": "completed",
                    "created_at_utc": old,
                },
                {
                    "operation_id": "old-accepted",
                    "user_id": user["id"],
                    "auth_session_id": None,
                    "reason": "test",
                    "identity_transition_version": 1,
                    "receipt_reference": "receipt-old-accepted",
                    "receipt_state": "accepted",
                    "created_at_utc": old,
                },
            ],
        )
        connection.execute(
            identity_object_cleanup_table.insert(),
            [
                {
                    "operation_id": "old-completed-cleanup",
                    "user_id": user["id"],
                    "object_key": "avatars/old-completed",
                    "created_at_utc": old,
                    "completed_at_utc": old,
                },
                {
                    "operation_id": "old-pending-cleanup",
                    "user_id": user["id"],
                    "object_key": "avatars/old-pending",
                    "created_at_utc": old,
                    "completed_at_utc": None,
                },
            ],
        )

    assert hasattr(service, "prune_completed_history")
    assert service.prune_completed_history(limit=100) == {
        "idempotency": 1,
        "revocations": 1,
        "object_cleanup": 1,
    }
    with engine.connect() as connection:
        idempotency_keys = set(
            connection.execute(select(identity_idempotency_table.c.idempotency_key)).scalars()
        )
        revocation_ids = set(
            connection.execute(select(identity_revocation_command_table.c.operation_id)).scalars()
        )
        cleanup_ids = set(
            connection.execute(select(identity_object_cleanup_table.c.operation_id)).scalars()
        )

    assert idempotency_keys == {"recent-completed", "old-pending"}
    assert revocation_ids == {"old-accepted"}
    assert cleanup_ids == {"old-pending-cleanup"}


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
