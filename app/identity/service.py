from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Literal

from sqlalchemy import Engine, and_, delete, exists, select, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from app.platform.config import AuthSettings
from app.platform.context import current_context
from app.platform.database import platform_audit_table
from app.platform.errors import PlatformError
from app.platform.storage import ObjectMetadata, ObjectStorePort, StorageKeyError

from .crypto import (
    decrypt_replay_payload,
    encrypt_replay_payload,
    hash_password,
    hash_refresh_token,
    new_csrf_token,
    new_refresh_token,
    sign_access_token,
    verify_access_token,
    verify_csrf_token,
    verify_password,
)
from .ports import (
    AccountDeletionCleanupCommand,
    AccountDeletionCleanupPort,
    AccountDeletionCleanupReceipt,
    DepartmentWorkCheckPort,
    DepartmentWorkState,
    UnavailableAccountDeletionCleanupPort,
    UnavailableDepartmentWorkCheckPort,
)
from .revocation import (
    GenerationRevocationCommand,
    GenerationRevocationPort,
    GenerationRevocationReceipt,
    UnavailableGenerationRevocationPort,
)
from .schema import (
    auth_refresh_token_table,
    auth_session_table,
    identity_deletion_workflow_table,
    identity_department_table,
    identity_idempotency_table,
    identity_login_throttle_table,
    identity_object_cleanup_table,
    identity_revocation_command_table,
    identity_space_table,
    identity_user_table,
)

Role = Literal["user", "minister", "ops", "admin"]
_ROLES = frozenset({"user", "minister", "ops", "admin"})


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _optional_utc(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError("Expected a datetime value")
    return _utc(value)


def _normalize_username(value: str) -> str:
    normalized = value.strip().casefold()
    if not normalized or len(normalized) > 128:
        raise PlatformError("validation_error", "Username is invalid", {}, 422)
    return normalized


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(18)}"


def _normalize_name(value: str, *, subject: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 256:
        raise PlatformError("validation_error", f"{subject} is invalid", {}, 422)
    return normalized


def _validate_password_rule(password: str) -> None:
    if len(password) < 8 or password.isalpha() or password.isdigit():
        raise PlatformError("invalid_password_rule", "Password does not meet the policy", {}, 400)


@dataclass(frozen=True, slots=True)
class AuthPrincipal:
    user_id: str
    auth_session_id: str
    username: str
    role: Role
    department_id: str | None


@dataclass(frozen=True, slots=True)
class SessionActionPrincipal:
    user_id: str
    auth_session_id: str
    session_revoked: bool


@dataclass(frozen=True, slots=True)
class AuthResult:
    access_token: str
    refresh_token: str
    csrf_token: str
    session_id: str
    user: dict[str, object]


@dataclass(slots=True)
class _IdempotencyOperation:
    service: IdentityAccessService
    actor_id: str
    endpoint: str
    target_id: str
    idempotency_key: str | None
    request_hash: str
    now: datetime
    replay: dict[str, object] | None = None
    error: PlatformError | None = None
    _transaction: Any = None
    _business_transaction: Any = None
    _connection: Connection | None = None
    _reserved: bool = False

    @property
    def connection(self) -> Connection:
        assert self._connection is not None
        return self._connection

    def __enter__(self) -> _IdempotencyOperation:
        self._transaction = self.service._engine.begin()
        self._connection = self._transaction.__enter__()
        try:
            self.replay = self.service._idempotency_replay(
                self.connection,
                actor_id=self.actor_id,
                endpoint=self.endpoint,
                target_id=self.target_id,
                idempotency_key=self.idempotency_key,
                request_hash=self.request_hash,
                now=self.now,
            )
            self._reserved = self.replay is None
            self._business_transaction = self.connection.begin_nested()
        except BaseException as error:
            self._transaction.__exit__(type(error), error, error.__traceback__)
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if isinstance(error, PlatformError) and self._reserved:
            self._business_transaction.__exit__(exc_type, error, traceback)
            self.service._complete_idempotency_error(
                self.connection,
                actor_id=self.actor_id,
                endpoint=self.endpoint,
                target_id=self.target_id,
                idempotency_key=self.idempotency_key.strip() if self.idempotency_key else "",
                error=error,
            )
            self.error = error
            self._transaction.__exit__(None, None, None)
            return True
        self._business_transaction.__exit__(exc_type, error, traceback)
        self._transaction.__exit__(exc_type, error, traceback)
        return False


class IdentityAccessService:
    """Authoritative identity and device-session service for the V1 application."""

    def __init__(
        self,
        engine: Engine,
        settings: AuthSettings,
        *,
        now: Callable[[], datetime] | None = None,
        revocation_port: GenerationRevocationPort | None = None,
        department_work_check: DepartmentWorkCheckPort | None = None,
        deletion_cleanup_port: AccountDeletionCleanupPort | None = None,
        object_store: ObjectStorePort | None = None,
    ) -> None:
        self._engine = engine
        self._settings = settings
        configured_secret = settings.secret_key.get_secret_value() if settings.secret_key else None
        self._secret = (configured_secret or "ragqs-development-auth-secret").encode("utf-8")
        self._now = now or (lambda: datetime.now(UTC))
        self._revocation_port = revocation_port or UnavailableGenerationRevocationPort()
        self._department_work_check = department_work_check or UnavailableDepartmentWorkCheckPort()
        self._deletion_cleanup_port = (
            deletion_cleanup_port or UnavailableAccountDeletionCleanupPort()
        )
        self._object_store = object_store

    def _current_time(self) -> datetime:
        return _utc(self._now())

    @staticmethod
    def _user_response(record: dict[str, object]) -> dict[str, object]:
        department_id = record["department_id"]
        department_name = record.get("department_name")
        return {
            "id": record["id"],
            "username": record["username"],
            "display_name": record["display_name"],
            "real_name": record["real_name"],
            "department": {"id": department_id, "name": department_name} if department_id else None,
            "role": record["role"],
            "avatar_url": record["avatar_url"],
        }

    def _user_response_for_id(self, connection: Connection, user_id: str) -> dict[str, object]:
        user = (
            connection.execute(
                select(
                    identity_user_table, identity_department_table.c.name.label("department_name")
                )
                .outerjoin(
                    identity_department_table,
                    identity_user_table.c.department_id == identity_department_table.c.id,
                )
                .where(identity_user_table.c.id == user_id)
            )
            .mappings()
            .one_or_none()
        )
        if user is None or user[identity_user_table.c.lifecycle_status] != "active":
            raise PlatformError("authentication_required", "The account is not active", {}, 401)
        return self._user_response(dict(user))

    def _new_user_record(
        self,
        *,
        username: str,
        password: str,
        real_name: str,
        display_name: str,
        role: Role,
        department_id: str | None,
        now: datetime,
    ) -> dict[str, object]:
        if role not in _ROLES:
            raise PlatformError("validation_error", "Role is invalid", {}, 422)
        _validate_password_rule(password)
        normalized_username = _normalize_username(username)
        roster = {item.strip().casefold() for item in self._settings.admin_roster if item.strip()}
        if role == "admin" and roster and normalized_username not in roster:
            raise PlatformError(
                "forbidden_target", "Admin seats are managed by the deployment roster", {}, 403
            )
        if role == "minister" and department_id is None:
            raise PlatformError(
                "minister_department_required",
                "A minister must belong to an active department",
                {},
                422,
            )
        return {
            "id": _new_id("user"),
            "username": _normalize_name(username, subject="Username"),
            "normalized_username": normalized_username,
            "password_hash": hash_password(password),
            "real_name": _normalize_name(real_name, subject="Real name"),
            "display_name": _normalize_name(display_name, subject="Display name"),
            "department_id": department_id,
            "role": role,
            "lifecycle_status": "active",
            "version": 1,
            "avatar_url": None,
            "preferences_json": {
                "theme": "system",
                "chat_font_size": "standard",
                "ab_opt_out": False,
            },
            "transition_version": 1,
            "created_at_utc": now,
            "updated_at_utc": now,
            "deletion_requested_at_utc": None,
            "purge_after_at_utc": None,
        }

    def _insert_user_record(self, connection: Connection, record: dict[str, object]) -> None:
        duplicate = connection.execute(
            select(identity_user_table.c.id).where(
                identity_user_table.c.normalized_username == record["normalized_username"]
            )
        ).scalar_one_or_none()
        if duplicate is not None:
            raise PlatformError("username_exists", "Username already exists", {}, 409)
        department_id = record["department_id"]
        if department_id is not None:
            department = (
                connection.execute(
                    select(identity_department_table)
                    .where(identity_department_table.c.id == department_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if department is None:
                raise PlatformError("department_not_found", "Department was not found", {}, 404)
            if department["status"] != "active":
                raise PlatformError("department_inactive", "Department is inactive", {}, 409)
        connection.execute(identity_user_table.insert().values(**record))
        connection.execute(
            identity_space_table.insert().values(
                id=f"personal:{record['id']}",
                kind="personal",
                name=f"{record['display_name']} personal space",
                owner_user_id=record["id"],
                department_id=None,
                created_at_utc=record["created_at_utc"],
            )
        )

    @staticmethod
    def _deletion_cleanup_operation_id(
        *,
        user_id: str,
        requested_at: datetime,
        purge_after: datetime,
    ) -> str:
        payload = f"{user_id}|{requested_at.isoformat()}|{purge_after.isoformat()}".encode()
        return hashlib.sha256(b"identity-deletion-cleanup-v1\x00" + payload).hexdigest()

    @classmethod
    def _start_deletion_workflow(
        cls,
        connection: Connection,
        *,
        user_id: str,
        requested_at: datetime,
        purge_after: datetime,
    ) -> None:
        connection.execute(
            identity_deletion_workflow_table.insert().values(
                user_id=user_id,
                status="pending",
                requested_at_utc=requested_at,
                purge_after_at_utc=purge_after,
                cleanup_operation_id=cls._deletion_cleanup_operation_id(
                    user_id=user_id,
                    requested_at=requested_at,
                    purge_after=purge_after,
                ),
                cleanup_reference=None,
                cleanup_completed_at_utc=None,
                completed_at_utc=None,
            )
        )

    def _idempotency_hash(self, payload: dict[str, object]) -> str:
        encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        return hmac.new(
            self._secret,
            b"identity-idempotency-v1\x00" + encoded.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _secret_fingerprint(self, purpose: str, value: str) -> str:
        return hmac.new(
            self._secret,
            purpose.encode("utf-8") + b"\x00" + value.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _idempotency_replay(
        self,
        connection: Connection,
        *,
        actor_id: str,
        endpoint: str,
        target_id: str,
        idempotency_key: str | None,
        request_hash: str,
        now: datetime,
    ) -> dict[str, object] | None:
        if not idempotency_key or not idempotency_key.strip():
            raise PlatformError("validation_error", "Idempotency-Key is required", {}, 422)
        key = idempotency_key.strip()
        existing = self._idempotency_existing(
            connection,
            actor_id=actor_id,
            endpoint=endpoint,
            target_id=target_id,
            idempotency_key=key,
        )
        if existing is not None:
            return self._replay_existing_idempotency(existing, request_hash=request_hash)
        try:
            with connection.begin_nested():
                connection.execute(
                    identity_idempotency_table.insert().values(
                        actor_id=actor_id,
                        endpoint=endpoint,
                        target_id=target_id,
                        idempotency_key=key,
                        request_hash=request_hash,
                        completed=False,
                        response_json=None,
                        created_at_utc=now,
                    )
                )
        except IntegrityError:
            existing = self._idempotency_existing(
                connection,
                actor_id=actor_id,
                endpoint=endpoint,
                target_id=target_id,
                idempotency_key=key,
            )
            if existing is None:
                raise
            return self._replay_existing_idempotency(existing, request_hash=request_hash)
        return None

    @staticmethod
    def _idempotency_existing(
        connection: Connection,
        *,
        actor_id: str,
        endpoint: str,
        target_id: str,
        idempotency_key: str,
    ) -> Any:
        return (
            connection.execute(
                select(identity_idempotency_table).where(
                    and_(
                        identity_idempotency_table.c.actor_id == actor_id,
                        identity_idempotency_table.c.endpoint == endpoint,
                        identity_idempotency_table.c.target_id == target_id,
                        identity_idempotency_table.c.idempotency_key == idempotency_key,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )

    @staticmethod
    def _replay_existing_idempotency(
        existing: Any,
        *,
        request_hash: str,
    ) -> dict[str, object] | None:
        if existing is not None:
            if existing["request_hash"] != request_hash:
                raise PlatformError(
                    "idempotency_key_conflict",
                    "Idempotency key was reused with a different request",
                    {},
                    409,
                )
            if existing["completed"] and isinstance(existing["response_json"], dict):
                response = dict(existing["response_json"])
                error = response.get("__identity_error__")
                if isinstance(error, dict):
                    raise PlatformError(
                        str(error["code"]),
                        str(error["message"]),
                        dict(error.get("details", {})),
                        int(error["status_code"]),
                    )
                return response
            raise PlatformError("idempotency_in_progress", "Request is still in progress", {}, 409)
        return None

    def _idempotency_operation(
        self,
        *,
        actor_id: str,
        endpoint: str,
        target_id: str,
        idempotency_key: str | None,
        request_hash: str,
        now: datetime,
    ) -> _IdempotencyOperation:
        return _IdempotencyOperation(
            service=self,
            actor_id=actor_id,
            endpoint=endpoint,
            target_id=target_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            now=now,
        )

    @staticmethod
    def _complete_idempotency(
        connection: Connection,
        *,
        actor_id: str,
        endpoint: str,
        target_id: str,
        idempotency_key: str,
        response: dict[str, object],
    ) -> None:
        connection.execute(
            update(identity_idempotency_table)
            .where(
                and_(
                    identity_idempotency_table.c.actor_id == actor_id,
                    identity_idempotency_table.c.endpoint == endpoint,
                    identity_idempotency_table.c.target_id == target_id,
                    identity_idempotency_table.c.idempotency_key == idempotency_key,
                )
            )
            .values(completed=True, response_json=response)
        )

    @classmethod
    def _complete_idempotency_error(
        cls,
        connection: Connection,
        *,
        actor_id: str,
        endpoint: str,
        target_id: str,
        idempotency_key: str,
        error: PlatformError,
    ) -> None:
        cls._complete_idempotency(
            connection,
            actor_id=actor_id,
            endpoint=endpoint,
            target_id=target_id,
            idempotency_key=idempotency_key,
            response={
                "__identity_error__": {
                    "code": error.code,
                    "message": error.message,
                    "details": dict(error.details),
                    "status_code": error.status_code,
                }
            },
        )

    @staticmethod
    def _require_admin(actor: AuthPrincipal) -> None:
        if actor.role != "admin":
            raise PlatformError(
                "department_action_forbidden", "Administrator access is required", {}, 403
            )

    @staticmethod
    def _require_user_manager(actor: AuthPrincipal, target_role: str) -> None:
        if actor.role not in {"admin", "ops"}:
            raise PlatformError("forbidden_target", "User management is not allowed", {}, 403)
        if target_role == "admin" or (
            actor.role == "ops" and target_role not in {"user", "minister"}
        ):
            raise PlatformError("forbidden_target", "Target is outside the caller scope", {}, 403)

    @staticmethod
    def _audit(
        connection: Connection,
        *,
        actor_id: str,
        resource_type: str,
        resource_id: str,
        result: str,
        occurred_at: datetime,
    ) -> None:
        context = current_context()
        connection.execute(
            platform_audit_table.insert().values(
                actor_id=actor_id,
                resource_type=resource_type,
                resource_id=resource_id,
                request_id=context.request_id if context is not None else "req_identity",
                occurred_at_utc=occurred_at,
                result=result,
                details_json={},
            )
        )

    def provision_user(
        self,
        *,
        username: str,
        password: str,
        real_name: str,
        display_name: str,
        role: Role,
        department_id: str | None,
    ) -> dict[str, object]:
        now = self._current_time()
        record = self._new_user_record(
            username=username,
            password=password,
            real_name=real_name,
            display_name=display_name,
            role=role,
            department_id=department_id,
            now=now,
        )
        with self._engine.begin() as connection:
            self._insert_user_record(connection, record)
            return self._user_response_for_id(connection, str(record["id"]))

    def bootstrap_initial_admin(
        self,
        *,
        username: str,
        password: str,
        real_name: str,
        display_name: str,
    ) -> dict[str, object]:
        """Create the single deployment-managed administrator for an empty identity database."""

        now = self._current_time()
        record = self._new_user_record(
            username=username,
            password=password,
            real_name=real_name,
            display_name=display_name,
            role="admin",
            department_id=None,
            now=now,
        )
        roster = {item.strip().casefold() for item in self._settings.admin_roster if item.strip()}
        if not roster:
            raise PlatformError(
                "admin_roster_invalid",
                "Initial administrator bootstrap requires a declared roster seat",
                {},
                503,
            )
        with self._engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:lock_name))"),
                    {"lock_name": "ragqs:identity:admin-bootstrap"},
                )
            users = (
                connection.execute(select(identity_user_table).with_for_update()).mappings().all()
            )
            if users:
                existing = dict(users[0]) if len(users) == 1 else None
                if (
                    existing is not None
                    and existing["normalized_username"] == record["normalized_username"]
                    and existing["role"] == "admin"
                    and existing["lifecycle_status"] == "active"
                    and existing["department_id"] is None
                ):
                    return self._user_response_for_id(connection, str(existing["id"]))
                raise PlatformError(
                    "admin_bootstrap_conflict",
                    "Initial administrator bootstrap requires an empty identity database",
                    {},
                    409,
                )
            self._insert_user_record(connection, record)
            self._audit(
                connection,
                actor_id="system:admin-bootstrap",
                resource_type="user",
                resource_id=str(record["id"]),
                result="admin_bootstrapped",
                occurred_at=now,
            )
            return self._user_response_for_id(connection, str(record["id"]))

    def reconcile_admin_roster(self) -> list[str]:
        """Apply a deployment roster removal through the normal irreversible lifecycle."""

        roster = {item.strip().casefold() for item in self._settings.admin_roster if item.strip()}
        if not roster:
            raise PlatformError(
                "admin_roster_invalid", "The admin roster must contain a seat", {}, 503
            )
        now = self._current_time()
        purge_after = now + timedelta(days=30)
        reconciled: list[str] = []
        with self._engine.begin() as connection:
            admins = (
                connection.execute(
                    select(identity_user_table).where(
                        and_(
                            identity_user_table.c.role == "admin",
                            identity_user_table.c.lifecycle_status == "active",
                        )
                    )
                )
                .mappings()
                .all()
            )
            if not any(admin["normalized_username"] in roster for admin in admins):
                raise PlatformError(
                    "admin_roster_invalid",
                    "The declared admin roster has no active seat",
                    {},
                    503,
                )
            for admin in admins:
                if admin["normalized_username"] in roster:
                    continue
                user_id = str(admin["id"])
                next_transition = int(admin["transition_version"]) + 1
                connection.execute(
                    update(identity_user_table)
                    .where(identity_user_table.c.id == user_id)
                    .values(
                        lifecycle_status="pending_delete",
                        version=int(admin["version"]) + 1,
                        transition_version=next_transition,
                        updated_at_utc=now,
                        deletion_requested_at_utc=now,
                        purge_after_at_utc=purge_after,
                    )
                )
                self._start_deletion_workflow(
                    connection,
                    user_id=user_id,
                    requested_at=now,
                    purge_after=purge_after,
                )
                self._revoke_account_sessions_in_transaction(
                    connection,
                    user_id=user_id,
                    reason="admin_roster_removed",
                    revoked_at=now,
                    transition_version=next_transition,
                )
                self._audit(
                    connection,
                    actor_id="system:admin-roster",
                    resource_type="user",
                    resource_id=user_id,
                    result="admin_roster_removed",
                    occurred_at=now,
                )
                reconciled.append(user_id)
        return reconciled

    def create_department(
        self,
        *,
        actor: AuthPrincipal,
        name: str,
        idempotency_key: str | None,
    ) -> dict[str, object]:
        self._require_admin(actor)
        normalized_name = _normalize_name(name, subject="Department name")
        now = self._current_time()
        request_hash = self._idempotency_hash({"name": normalized_name.casefold()})
        with self._idempotency_operation(
            actor_id=actor.user_id,
            endpoint="POST:/admin/departments",
            target_id="",
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            now=now,
        ) as operation:
            if operation.replay is not None:
                return operation.replay
            connection = operation.connection
            duplicate = connection.execute(
                select(identity_department_table.c.id).where(
                    identity_department_table.c.normalized_name == normalized_name.casefold()
                )
            ).scalar_one_or_none()
            if duplicate is not None:
                raise PlatformError("department_name_exists", "Department already exists", {}, 409)
            department = {
                "id": _new_id("department"),
                "name": normalized_name,
                "normalized_name": normalized_name.casefold(),
                "status": "active",
                "version": 1,
                "created_at_utc": now,
                "updated_at_utc": now,
                "deactivated_at_utc": None,
            }
            connection.execute(identity_department_table.insert().values(**department))
            connection.execute(
                identity_space_table.insert().values(
                    id=f"department:{department['id']}",
                    kind="department",
                    name=normalized_name,
                    owner_user_id=None,
                    department_id=department["id"],
                    created_at_utc=now,
                )
            )
            response = {
                "id": department["id"],
                "name": department["name"],
                "status": "active",
                "version": 1,
                "document_count": 0,
                "member_count": 0,
                "nonterminal_job_count": 0,
                "pending_submission_count": 0,
                "deactivated_at": None,
                "allowed_actions": ["rename", "deactivate"],
            }
            self._complete_idempotency(
                connection,
                actor_id=actor.user_id,
                endpoint="POST:/admin/departments",
                target_id="",
                idempotency_key=idempotency_key.strip() if idempotency_key else "",
                response=response,
            )
            self._audit(
                connection,
                actor_id=actor.user_id,
                resource_type="department",
                resource_id=str(department["id"]),
                result="department_created",
                occurred_at=now,
            )
            return response
        if operation.error is not None:
            raise operation.error
        raise RuntimeError("Idempotency operation completed without a result")

    def create_managed_user(
        self,
        *,
        actor: AuthPrincipal,
        username: str,
        password: str,
        real_name: str,
        display_name: str,
        role: Role,
        department_id: str | None,
        idempotency_key: str | None,
    ) -> dict[str, object]:
        self._require_user_manager(actor, role)
        now = self._current_time()
        record = self._new_user_record(
            username=username,
            password=password,
            real_name=real_name,
            display_name=display_name,
            role=role,
            department_id=department_id,
            now=now,
        )
        request_hash = self._idempotency_hash(
            {
                "username": record["normalized_username"],
                "real_name": record["real_name"],
                "display_name": record["display_name"],
                "role": role,
                "department_id": department_id,
                "initial_password_fingerprint": self._secret_fingerprint(
                    "initial-password", password
                ),
            }
        )
        with self._idempotency_operation(
            actor_id=actor.user_id,
            endpoint="POST:/admin/users",
            target_id="",
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            now=now,
        ) as operation:
            if operation.replay is not None:
                return operation.replay
            connection = operation.connection
            self._insert_user_record(connection, record)
            response = self._user_response_for_id(connection, str(record["id"]))
            response["version"] = 1
            self._complete_idempotency(
                connection,
                actor_id=actor.user_id,
                endpoint="POST:/admin/users",
                target_id="",
                idempotency_key=idempotency_key.strip() if idempotency_key else "",
                response=response,
            )
            self._audit(
                connection,
                actor_id=actor.user_id,
                resource_type="user",
                resource_id=str(record["id"]),
                result="user_created",
                occurred_at=now,
            )
            return response
        if operation.error is not None:
            raise operation.error
        raise RuntimeError("Idempotency operation completed without a result")

    def update_managed_user(
        self,
        *,
        actor: AuthPrincipal,
        user_id: str,
        expected_version: int,
        role: Role | None,
        department_id: str | None,
        department_provided: bool,
        idempotency_key: str | None,
    ) -> dict[str, object]:
        if expected_version < 1:
            raise PlatformError("validation_error", "Expected version is invalid", {}, 422)
        now = self._current_time()
        request_hash = self._idempotency_hash(
            {
                "expected_version": expected_version,
                "role": role,
                "department_id": department_id if department_provided else "__unchanged__",
            }
        )
        endpoint = "PATCH:/admin/users/{id}"
        with self._idempotency_operation(
            actor_id=actor.user_id,
            endpoint=endpoint,
            target_id=user_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            now=now,
        ) as operation:
            if operation.replay is not None:
                return operation.replay
            connection = operation.connection
            target = (
                connection.execute(
                    select(identity_user_table)
                    .where(identity_user_table.c.id == user_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if target is None:
                raise PlatformError("not_found", "User was not found", {}, 404)
            self._require_user_manager(actor, str(target["role"]))
            if actor.user_id == user_id:
                raise PlatformError(
                    "cannot_modify_self", "A user cannot modify themselves", {}, 403
                )
            if target["lifecycle_status"] != "active":
                raise PlatformError("user_pending_delete", "User is pending deletion", {}, 409)
            if int(target["version"]) != expected_version:
                raise PlatformError(
                    "version_conflict", "User version is no longer current", {}, 409
                )
            final_role = role if role is not None else target["role"]
            final_department_id = department_id if department_provided else target["department_id"]
            if final_role not in _ROLES:
                raise PlatformError("validation_error", "Role is invalid", {}, 422)
            self._require_user_manager(actor, str(final_role))
            if final_role == "minister" and final_department_id is None:
                raise PlatformError(
                    "minister_department_required",
                    "A minister must belong to an active department",
                    {},
                    422,
                )
            if final_department_id is not None:
                department = (
                    connection.execute(
                        select(identity_department_table)
                        .where(identity_department_table.c.id == final_department_id)
                        .with_for_update()
                    )
                    .mappings()
                    .one_or_none()
                )
                if department is None:
                    raise PlatformError("department_not_found", "Department was not found", {}, 404)
                if department["status"] != "active":
                    raise PlatformError("department_inactive", "Department is inactive", {}, 409)
            authorization_changed = (
                final_role != target["role"] or final_department_id != target["department_id"]
            )
            next_transition = int(target["transition_version"]) + int(authorization_changed)
            next_version = int(target["version"]) + 1
            update_conditions = [
                identity_user_table.c.id == user_id,
                identity_user_table.c.version == expected_version,
                identity_user_table.c.lifecycle_status == "active",
            ]
            if final_department_id is not None:
                update_conditions.append(
                    exists().where(
                        and_(
                            identity_department_table.c.id == final_department_id,
                            identity_department_table.c.status == "active",
                        )
                    )
                )
            updated = connection.execute(
                update(identity_user_table)
                .where(and_(*update_conditions))
                .values(
                    role=final_role,
                    department_id=final_department_id,
                    version=next_version,
                    transition_version=next_transition,
                    updated_at_utc=now,
                )
            ).rowcount
            if updated != 1:
                raise PlatformError(
                    "version_conflict", "User version is no longer current", {}, 409
                )
            if authorization_changed:
                self._revoke_account_sessions_in_transaction(
                    connection,
                    user_id=user_id,
                    reason="authorization_changed",
                    revoked_at=now,
                    transition_version=next_transition,
                )
            result = self._user_response_for_id(connection, user_id)
            result["version"] = next_version
            self._complete_idempotency(
                connection,
                actor_id=actor.user_id,
                endpoint=endpoint,
                target_id=user_id,
                idempotency_key=idempotency_key.strip() if idempotency_key else "",
                response=result,
            )
            self._audit(
                connection,
                actor_id=actor.user_id,
                resource_type="user",
                resource_id=user_id,
                result="user_updated",
                occurred_at=now,
            )
            return result
        if operation.error is not None:
            raise operation.error
        raise RuntimeError("Idempotency operation completed without a result")

    def deactivate_department(
        self,
        *,
        actor: AuthPrincipal,
        department_id: str,
        expected_version: int,
        idempotency_key: str | None,
    ) -> dict[str, object]:
        self._require_admin(actor)
        if expected_version < 1:
            raise PlatformError("validation_error", "Expected version is invalid", {}, 422)
        now = self._current_time()
        endpoint = "POST:/admin/departments/{id}/deactivate"
        request_hash = self._idempotency_hash({"expected_version": expected_version})
        blocked_error: PlatformError | None = None
        result: dict[str, object] | None = None
        with self._idempotency_operation(
            actor_id=actor.user_id,
            endpoint=endpoint,
            target_id=department_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            now=now,
        ) as operation:
            if operation.replay is not None:
                return operation.replay
            connection = operation.connection
            department = (
                connection.execute(
                    select(identity_department_table)
                    .where(identity_department_table.c.id == department_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if department is None:
                raise PlatformError("department_not_found", "Department was not found", {}, 404)
            if department["status"] != "active":
                raise PlatformError("department_inactive", "Department is inactive", {}, 409)
            if int(department["version"]) != expected_version:
                raise PlatformError(
                    "version_conflict", "Department version is no longer current", {}, 409
                )
            member_id = connection.execute(
                select(identity_user_table.c.id).where(
                    and_(
                        identity_user_table.c.department_id == department_id,
                        identity_user_table.c.lifecycle_status.in_(("active", "pending_delete")),
                    )
                )
            ).scalar_one_or_none()
            if member_id is not None:
                blocked_error = PlatformError(
                    "department_has_members",
                    "Department still has members",
                    {},
                    409,
                )
                work_state = DepartmentWorkState()
            else:
                try:
                    work_state = self._department_work_check.inspect(
                        department_id,
                        connection=connection,
                    )
                except Exception:
                    blocked_error = PlatformError(
                        "department_deactivation_unverified",
                        "Department work state could not be verified",
                        {"retryable": True},
                        503,
                        True,
                    )
                    work_state = DepartmentWorkState()
                if blocked_error is None and (
                    not isinstance(work_state, DepartmentWorkState)
                    or work_state.nonterminal_job_count < 0
                    or work_state.pending_submission_count < 0
                ):
                    blocked_error = PlatformError(
                        "department_deactivation_unverified",
                        "Department work state could not be verified",
                        {"retryable": True},
                        503,
                        True,
                    )
                elif blocked_error is None and (
                    work_state.nonterminal_job_count or work_state.pending_submission_count
                ):
                    blocked_error = PlatformError(
                        "department_has_active_work",
                        "Department still has active work",
                        {},
                        409,
                    )
            if blocked_error is not None:
                self._complete_idempotency_error(
                    connection,
                    actor_id=actor.user_id,
                    endpoint=endpoint,
                    target_id=department_id,
                    idempotency_key=idempotency_key.strip() if idempotency_key else "",
                    error=blocked_error,
                )
                self._audit(
                    connection,
                    actor_id=actor.user_id,
                    resource_type="department",
                    resource_id=department_id,
                    result="department_deactivation_blocked",
                    occurred_at=now,
                )
            else:
                next_version = int(department["version"]) + 1
                updated = connection.execute(
                    update(identity_department_table)
                    .where(
                        and_(
                            identity_department_table.c.id == department_id,
                            identity_department_table.c.version == expected_version,
                            identity_department_table.c.status == "active",
                        )
                    )
                    .values(
                        status="inactive",
                        version=next_version,
                        updated_at_utc=now,
                        deactivated_at_utc=now,
                    )
                ).rowcount
                if updated != 1:
                    raise PlatformError(
                        "version_conflict", "Department version is no longer current", {}, 409
                    )
                result = {
                    "id": department_id,
                    "name": department["name"],
                    "status": "inactive",
                    "version": next_version,
                    "document_count": 0,
                    "member_count": 0,
                    "nonterminal_job_count": work_state.nonterminal_job_count,
                    "pending_submission_count": work_state.pending_submission_count,
                    "deactivated_at": now.isoformat(),
                    "allowed_actions": [],
                }
                self._complete_idempotency(
                    connection,
                    actor_id=actor.user_id,
                    endpoint=endpoint,
                    target_id=department_id,
                    idempotency_key=idempotency_key.strip() if idempotency_key else "",
                    response=result,
                )
                self._audit(
                    connection,
                    actor_id=actor.user_id,
                    resource_type="department",
                    resource_id=department_id,
                    result="department_deactivated",
                    occurred_at=now,
                )
        if operation.error is not None:
            raise operation.error
        if blocked_error is not None:
            raise blocked_error
        assert result is not None
        return result

    def delete_managed_user(
        self,
        *,
        actor: AuthPrincipal,
        user_id: str,
        expected_version: int,
        idempotency_key: str | None,
    ) -> dict[str, object]:
        if expected_version < 1:
            raise PlatformError("validation_error", "Expected version is invalid", {}, 422)
        now = self._current_time()
        purge_after = now + timedelta(days=30)
        endpoint = "DELETE:/admin/users/{id}"
        request_hash = self._idempotency_hash({"expected_version": expected_version})
        with self._idempotency_operation(
            actor_id=actor.user_id,
            endpoint=endpoint,
            target_id=user_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            now=now,
        ) as operation:
            if operation.replay is not None:
                return operation.replay
            connection = operation.connection
            target = (
                connection.execute(
                    select(identity_user_table)
                    .where(identity_user_table.c.id == user_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if target is None:
                raise PlatformError("not_found", "User was not found", {}, 404)
            self._require_user_manager(actor, str(target["role"]))
            if actor.user_id == user_id:
                raise PlatformError(
                    "cannot_modify_self", "A user cannot modify themselves", {}, 403
                )
            if target["lifecycle_status"] != "active":
                raise PlatformError("user_pending_delete", "User is pending deletion", {}, 409)
            if int(target["version"]) != expected_version:
                raise PlatformError(
                    "version_conflict", "User version is no longer current", {}, 409
                )
            next_version = int(target["version"]) + 1
            next_transition = int(target["transition_version"]) + 1
            updated = connection.execute(
                update(identity_user_table)
                .where(
                    and_(
                        identity_user_table.c.id == user_id,
                        identity_user_table.c.version == expected_version,
                        identity_user_table.c.lifecycle_status == "active",
                    )
                )
                .values(
                    lifecycle_status="pending_delete",
                    version=next_version,
                    transition_version=next_transition,
                    deletion_requested_at_utc=now,
                    purge_after_at_utc=purge_after,
                    updated_at_utc=now,
                )
            ).rowcount
            if updated != 1:
                raise PlatformError(
                    "version_conflict", "User version is no longer current", {}, 409
                )
            self._start_deletion_workflow(
                connection,
                user_id=user_id,
                requested_at=now,
                purge_after=purge_after,
            )
            self._revoke_account_sessions_in_transaction(
                connection,
                user_id=user_id,
                reason="account_pending_delete",
                revoked_at=now,
                transition_version=next_transition,
            )
            result = {
                "id": user_id,
                "version": next_version,
                "lifecycle_status": "pending_delete",
                "deletion_requested_at": now.isoformat(),
                "purge_after_at": purge_after.isoformat(),
            }
            self._complete_idempotency(
                connection,
                actor_id=actor.user_id,
                endpoint=endpoint,
                target_id=user_id,
                idempotency_key=idempotency_key.strip() if idempotency_key else "",
                response=result,
            )
            self._audit(
                connection,
                actor_id=actor.user_id,
                resource_type="user",
                resource_id=user_id,
                result="user_pending_delete",
                occurred_at=now,
            )
            return result
        if operation.error is not None:
            raise operation.error
        raise RuntimeError("Idempotency operation completed without a result")

    def list_due_deletion_workflows(self, *, limit: int = 100) -> list[str]:
        if limit < 1 or limit > 1000:
            raise PlatformError("validation_error", "Deletion workflow limit is invalid", {}, 422)
        now = self._current_time()
        with self._engine.connect() as connection:
            records = connection.execute(
                select(identity_deletion_workflow_table.c.user_id)
                .where(
                    and_(
                        identity_deletion_workflow_table.c.status == "pending",
                        identity_deletion_workflow_table.c.purge_after_at_utc <= now,
                    )
                )
                .order_by(identity_deletion_workflow_table.c.purge_after_at_utc)
                .limit(limit)
            ).scalars()
            return [str(user_id) for user_id in records]

    def finalize_pending_deletion(
        self,
        *,
        user_id: str,
        connection: Connection | None = None,
    ) -> dict[str, object]:
        """Turn a completed deletion workflow into a non-authenticating tombstone."""

        now = self._current_time()
        with self._engine.begin() if connection is None else nullcontext(connection) as connection:
            workflow = (
                connection.execute(
                    select(identity_deletion_workflow_table)
                    .where(identity_deletion_workflow_table.c.user_id == user_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if workflow is None:
                raise PlatformError(
                    "deletion_workflow_not_found", "Deletion workflow was not found", {}, 404
                )
            user = (
                connection.execute(
                    select(identity_user_table)
                    .where(identity_user_table.c.id == user_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if user is None:
                raise PlatformError("not_found", "User was not found", {}, 404)
            if user["lifecycle_status"] == "deleted":
                return {"id": user_id, "lifecycle_status": "deleted"}
            if user["lifecycle_status"] != "pending_delete" or workflow["status"] != "pending":
                raise PlatformError(
                    "deletion_workflow_invalid",
                    "Deletion workflow cannot be finalized",
                    {},
                    409,
                )
            purge_after = _utc(workflow["purge_after_at_utc"])
            if now < purge_after:
                raise PlatformError(
                    "deletion_not_ready",
                    "Deletion retention period has not elapsed",
                    {"purge_after_at": purge_after.isoformat(), "retryable": True},
                    409,
                    True,
                )
            pending_object_cleanup_operations = connection.execute(
                select(identity_object_cleanup_table.c.operation_id)
                .where(
                    and_(
                        identity_object_cleanup_table.c.user_id == user_id,
                        identity_object_cleanup_table.c.completed_at_utc.is_(None),
                    )
                )
                .order_by(identity_object_cleanup_table.c.created_at_utc)
            ).scalars()
            for operation_id in pending_object_cleanup_operations:
                self.finalize_object_cleanup(
                    operation_id=str(operation_id),
                    connection=connection,
                )
            try:
                receipt = self._deletion_cleanup_port.confirm_cleanup(
                    AccountDeletionCleanupCommand(
                        operation_id=str(workflow["cleanup_operation_id"]),
                        user_id=user_id,
                        requested_at=_utc(workflow["requested_at_utc"]),
                        purge_after=purge_after,
                    ),
                    connection=connection,
                )
            except Exception as exc:
                raise PlatformError(
                    "deletion_cleanup_unverified",
                    "Deletion cleanup could not be confirmed",
                    {"retryable": True},
                    503,
                    True,
                ) from exc
            if (
                not isinstance(receipt, AccountDeletionCleanupReceipt)
                or receipt.state != "completed"
                or not receipt.reference.strip()
            ):
                raise PlatformError(
                    "deletion_cleanup_unverified",
                    "Deletion cleanup could not be confirmed",
                    {"retryable": True},
                    503,
                    True,
                )
            session_ids = select(auth_session_table.c.id).where(
                auth_session_table.c.user_id == user_id
            )
            connection.execute(
                delete(auth_refresh_token_table).where(
                    auth_refresh_token_table.c.auth_session_id.in_(session_ids)
                )
            )
            connection.execute(
                delete(auth_session_table).where(auth_session_table.c.user_id == user_id)
            )
            connection.execute(
                delete(identity_space_table).where(identity_space_table.c.owner_user_id == user_id)
            )
            connection.execute(
                update(identity_user_table)
                .where(identity_user_table.c.id == user_id)
                .values(
                    password_hash="!deleted",
                    real_name="Deleted account",
                    display_name="Deleted account",
                    department_id=None,
                    role="user",
                    lifecycle_status="deleted",
                    version=int(user["version"]) + 1,
                    avatar_url=None,
                    preferences_json={},
                    transition_version=int(user["transition_version"]) + 1,
                    updated_at_utc=now,
                    purge_after_at_utc=None,
                )
            )
            connection.execute(
                update(identity_deletion_workflow_table)
                .where(identity_deletion_workflow_table.c.user_id == user_id)
                .values(
                    status="completed",
                    cleanup_reference=receipt.reference,
                    cleanup_completed_at_utc=now,
                    completed_at_utc=now,
                )
            )
            self._audit(
                connection,
                actor_id="system:deletion-worker",
                resource_type="user",
                resource_id=user_id,
                result="user_deleted",
                occurred_at=now,
            )
        return {"id": user_id, "lifecycle_status": "deleted"}

    @staticmethod
    def _require_directory_reader(actor: AuthPrincipal) -> None:
        if actor.role not in {"admin", "ops"}:
            raise PlatformError("forbidden_target", "Directory access is not allowed", {}, 403)

    def list_managed_users(
        self,
        *,
        actor: AuthPrincipal,
        q: str | None = None,
        department_id: str | None = None,
        role: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, object]:
        self._require_directory_reader(actor)
        if page < 1 or page_size < 1 or page_size > 100:
            raise PlatformError("validation_error", "Pagination is invalid", {}, 422)
        if role is not None and role not in _ROLES:
            raise PlatformError("validation_error", "Role is invalid", {}, 422)
        with self._engine.connect() as connection:
            records = (
                connection.execute(
                    select(
                        identity_user_table,
                        identity_department_table.c.name.label("department_name"),
                    )
                    .outerjoin(
                        identity_department_table,
                        identity_user_table.c.department_id == identity_department_table.c.id,
                    )
                    .where(identity_user_table.c.lifecycle_status.in_(("active", "pending_delete")))
                    .order_by(identity_user_table.c.created_at_utc)
                )
                .mappings()
                .all()
            )
            session_activity = (
                connection.execute(
                    select(auth_session_table.c.user_id, auth_session_table.c.last_active_at_utc)
                )
                .mappings()
                .all()
            )
        last_active_by_user: dict[str, datetime] = {}
        for session in session_activity:
            user_id = str(session["user_id"])
            observed_at = _utc(session["last_active_at_utc"])
            previous = last_active_by_user.get(user_id)
            if previous is None or observed_at > previous:
                last_active_by_user[user_id] = observed_at
        query = q.strip().casefold() if q and q.strip() else None
        matched = []
        for record in records:
            if actor.role == "ops" and record[identity_user_table.c.role] not in {
                "user",
                "minister",
            }:
                continue
            if (
                department_id is not None
                and record[identity_user_table.c.department_id] != department_id
            ):
                continue
            if role is not None and record[identity_user_table.c.role] != role:
                continue
            if query is not None:
                searchable = " ".join(
                    str(record.get(key) or "")
                    for key in (
                        identity_user_table.c.username,
                        identity_user_table.c.real_name,
                        identity_user_table.c.display_name,
                        identity_user_table.c.role,
                        "department_name",
                    )
                ).casefold()
                if query not in searchable:
                    continue
            matched.append(record)
        start = (page - 1) * page_size
        items = []
        for record in matched[start : start + page_size]:
            user_id = record[identity_user_table.c.id]
            items.append(
                {
                    "id": user_id,
                    "username": record[identity_user_table.c.username],
                    "real_name": record[identity_user_table.c.real_name],
                    "display_name": record[identity_user_table.c.display_name],
                    "department": (
                        {
                            "id": record[identity_user_table.c.department_id],
                            "name": record["department_name"],
                        }
                        if record[identity_user_table.c.department_id]
                        else None
                    ),
                    "role": record[identity_user_table.c.role],
                    "last_active_at": (
                        last_active_by_user[str(user_id)].isoformat()
                        if str(user_id) in last_active_by_user
                        else None
                    ),
                    "document_count": 0,
                    "version": record[identity_user_table.c.version],
                    "lifecycle_status": record[identity_user_table.c.lifecycle_status],
                    "deletion_requested_at": (
                        _utc(record[identity_user_table.c.deletion_requested_at_utc]).isoformat()
                        if record[identity_user_table.c.deletion_requested_at_utc]
                        else None
                    ),
                    "purge_after_at": (
                        _utc(record[identity_user_table.c.purge_after_at_utc]).isoformat()
                        if record[identity_user_table.c.purge_after_at_utc]
                        else None
                    ),
                }
            )
        return {"items": items, "total": len(matched), "page": page, "page_size": page_size}

    def list_departments(
        self,
        *,
        actor: AuthPrincipal,
        status: Literal["active", "inactive", "all"] = "active",
    ) -> list[dict[str, object]]:
        self._require_directory_reader(actor)
        if status not in {"active", "inactive", "all"}:
            raise PlatformError("validation_error", "Department status is invalid", {}, 422)
        with self._engine.connect() as connection:
            departments = (
                connection.execute(
                    select(identity_department_table).order_by(identity_department_table.c.name)
                )
                .mappings()
                .all()
            )
            users = connection.execute(select(identity_user_table)).mappings().all()
        items = []
        for department in departments:
            if status != "all" and department["status"] != status:
                continue
            member_count = sum(
                1
                for user in users
                if user["department_id"] == department["id"]
                and user["lifecycle_status"] in {"active", "pending_delete"}
            )
            active = department["status"] == "active"
            items.append(
                {
                    "id": department["id"],
                    "name": department["name"],
                    "status": department["status"],
                    "version": department["version"],
                    "document_count": 0,
                    "member_count": member_count,
                    "nonterminal_job_count": 0,
                    "pending_submission_count": 0,
                    "deactivated_at": (
                        _utc(department["deactivated_at_utc"]).isoformat()
                        if department["deactivated_at_utc"]
                        else None
                    ),
                    "allowed_actions": (
                        ["rename", "deactivate"] if actor.role == "admin" and active else []
                    ),
                }
            )
        return items

    def rename_department(
        self,
        *,
        actor: AuthPrincipal,
        department_id: str,
        expected_version: int,
        name: str,
        idempotency_key: str | None,
    ) -> dict[str, object]:
        self._require_admin(actor)
        if expected_version < 1:
            raise PlatformError("validation_error", "Expected version is invalid", {}, 422)
        normalized_name = _normalize_name(name, subject="Department name")
        now = self._current_time()
        endpoint = "PATCH:/admin/departments/{id}"
        request_hash = self._idempotency_hash(
            {"expected_version": expected_version, "name": normalized_name.casefold()}
        )
        with self._idempotency_operation(
            actor_id=actor.user_id,
            endpoint=endpoint,
            target_id=department_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            now=now,
        ) as operation:
            if operation.replay is not None:
                return operation.replay
            connection = operation.connection
            department = (
                connection.execute(
                    select(identity_department_table).where(
                        identity_department_table.c.id == department_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if department is None:
                raise PlatformError("department_not_found", "Department was not found", {}, 404)
            if department["status"] != "active":
                raise PlatformError("department_inactive", "Department is inactive", {}, 409)
            if int(department["version"]) != expected_version:
                raise PlatformError(
                    "version_conflict", "Department version is no longer current", {}, 409
                )
            duplicate = connection.execute(
                select(identity_department_table.c.id).where(
                    and_(
                        identity_department_table.c.normalized_name == normalized_name.casefold(),
                        identity_department_table.c.id != department_id,
                    )
                )
            ).scalar_one_or_none()
            if duplicate is not None:
                raise PlatformError("department_name_exists", "Department already exists", {}, 409)
            next_version = int(department["version"]) + 1
            updated = connection.execute(
                update(identity_department_table)
                .where(
                    and_(
                        identity_department_table.c.id == department_id,
                        identity_department_table.c.version == expected_version,
                        identity_department_table.c.status == "active",
                    )
                )
                .values(
                    name=normalized_name,
                    normalized_name=normalized_name.casefold(),
                    version=next_version,
                    updated_at_utc=now,
                )
            ).rowcount
            if updated != 1:
                raise PlatformError(
                    "version_conflict", "Department version is no longer current", {}, 409
                )
            connection.execute(
                update(identity_space_table)
                .where(identity_space_table.c.id == f"department:{department_id}")
                .values(name=normalized_name)
            )
            result = {
                "id": department_id,
                "name": normalized_name,
                "status": "active",
                "version": next_version,
                "document_count": 0,
                "member_count": 0,
                "nonterminal_job_count": 0,
                "pending_submission_count": 0,
                "deactivated_at": None,
                "allowed_actions": ["rename", "deactivate"],
            }
            self._complete_idempotency(
                connection,
                actor_id=actor.user_id,
                endpoint=endpoint,
                target_id=department_id,
                idempotency_key=idempotency_key.strip() if idempotency_key else "",
                response=result,
            )
            self._audit(
                connection,
                actor_id=actor.user_id,
                resource_type="department",
                resource_id=department_id,
                result="department_updated",
                occurred_at=now,
            )
            return result
        if operation.error is not None:
            raise operation.error
        raise RuntimeError("Idempotency operation completed without a result")

    def permission_matrix(self, *, actor: AuthPrincipal) -> dict[str, object]:
        if actor.role != "admin":
            raise PlatformError(
                "forbidden_target", "Permission matrix requires admin access", {}, 403
            )
        capabilities = (
            ("spaces_manage", "Knowledge spaces", (True, True, True, True)),
            ("user_manage", "User account management", (False, False, True, True)),
            ("department_manage", "Department management", (False, False, False, True)),
            ("permission_matrix_read", "Permission matrix", (False, False, False, True)),
            ("operations", "Operations", (False, False, True, False)),
        )
        return {
            "capabilities": [
                {
                    "key": key,
                    "label": label,
                    "roles": dict(zip(("user", "minister", "ops", "admin"), roles, strict=True)),
                }
                for key, label, roles in capabilities
            ]
        }

    def _ensure_public_space(self, connection: Connection, now: datetime) -> None:
        existing = connection.execute(
            select(identity_space_table.c.id).where(identity_space_table.c.id == "public")
        ).scalar_one_or_none()
        if existing is None:
            connection.execute(
                identity_space_table.insert().values(
                    id="public",
                    kind="public",
                    name="Public knowledge space",
                    owner_user_id=None,
                    department_id=None,
                    created_at_utc=now,
                )
            )

    @staticmethod
    def _current_acl_principal(connection: Connection, principal: AuthPrincipal) -> AuthPrincipal:
        session = (
            connection.execute(
                select(auth_session_table).where(
                    and_(
                        auth_session_table.c.id == principal.auth_session_id,
                        auth_session_table.c.user_id == principal.user_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if session is None or session["revoked_at_utc"] is not None:
            raise PlatformError("session_revoked", "The session has been revoked", {}, 401)
        user = (
            connection.execute(
                select(identity_user_table).where(identity_user_table.c.id == principal.user_id)
            )
            .mappings()
            .one_or_none()
        )
        if user is None or user["lifecycle_status"] != "active":
            raise PlatformError("authentication_required", "The account is not active", {}, 401)
        if int(session["identity_transition_version"]) != int(user["transition_version"]):
            raise PlatformError("session_revoked", "The session has been revoked", {}, 401)
        return AuthPrincipal(
            user_id=str(user["id"]),
            auth_session_id=principal.auth_session_id,
            username=str(user["username"]),
            role=user["role"],
            department_id=user["department_id"],
        )

    def list_spaces(
        self,
        *,
        principal: AuthPrincipal,
        usage: Literal["retrieval", "upload", "manage"] = "manage",
    ) -> list[dict[str, object]]:
        if usage not in {"retrieval", "upload", "manage"}:
            raise PlatformError("validation_error", "Space usage is invalid", {}, 422)
        with self._engine.begin() as connection:
            principal = self._current_acl_principal(connection, principal)
            self._ensure_public_space(connection, self._current_time())
            rows = (
                connection.execute(
                    select(identity_space_table, identity_department_table.c.status)
                    .outerjoin(
                        identity_department_table,
                        identity_space_table.c.department_id == identity_department_table.c.id,
                    )
                    .order_by(identity_space_table.c.id)
                )
                .mappings()
                .all()
            )
        items: list[dict[str, object]] = []
        for row in rows:
            kind = row[identity_space_table.c.kind]
            department_status = row[identity_department_table.c.status]
            permission: str | None = None
            if kind == "personal":
                if row[identity_space_table.c.owner_user_id] == principal.user_id:
                    permission = "manage"
                elif usage == "manage" and principal.role in {"ops", "admin"}:
                    permission = "read"
            elif kind == "public":
                permission = "manage" if principal.role in {"ops", "admin"} else "contribute"
            elif kind == "department":
                is_own_department = (
                    row[identity_space_table.c.department_id] == principal.department_id
                )
                if department_status == "active":
                    if principal.role in {"ops", "admin"}:
                        permission = "manage"
                    elif is_own_department:
                        permission = "manage" if principal.role == "minister" else "contribute"
                elif usage == "manage" and principal.role in {"ops", "admin"}:
                    permission = "read"
            if permission is None:
                continue
            if usage == "retrieval" and (
                kind == "personal"
                and row[identity_space_table.c.owner_user_id] != principal.user_id
            ):
                continue
            if usage == "retrieval" and kind == "department" and department_status != "active":
                continue
            if usage == "upload" and permission not in {"manage", "contribute"}:
                continue
            item: dict[str, object] = {
                "id": row[identity_space_table.c.id],
                "kind": kind,
                "name": row[identity_space_table.c.name],
                "permission": permission,
                "document_count": 0,
            }
            if kind == "department":
                item["department_status"] = department_status
            items.append(item)
        return items

    def authorize_space(
        self,
        *,
        principal: AuthPrincipal,
        space_id: str,
        action: Literal["read", "contribute", "manage"],
    ) -> Literal["read", "contribute", "manage"]:
        if action not in {"read", "contribute", "manage"}:
            raise PlatformError("validation_error", "Space action is invalid", {}, 422)
        item = next(
            (
                candidate
                for candidate in self.list_spaces(principal=principal, usage="manage")
                if candidate["id"] == space_id
            ),
            None,
        )
        if item is None:
            raise PlatformError("space_not_found", "Space was not found", {}, 404)
        permission = item["permission"]
        permission_rank = {"read": 0, "contribute": 1, "manage": 2}
        if permission_rank[str(permission)] < permission_rank[action]:
            raise PlatformError("space_action_forbidden", "Space action is not allowed", {}, 403)
        return permission  # type: ignore[return-value]

    def user_response(self, user_id: str) -> dict[str, object]:
        with self._engine.connect() as connection:
            return self._user_response_for_id(connection, user_id)

    def list_sessions(
        self,
        *,
        user_id: object,
        current_session_id: str,
    ) -> list[dict[str, object]]:
        with self._engine.connect() as connection:
            sessions = (
                connection.execute(
                    select(auth_session_table)
                    .where(
                        and_(
                            auth_session_table.c.user_id == str(user_id),
                            auth_session_table.c.revoked_at_utc.is_(None),
                        )
                    )
                    .order_by(auth_session_table.c.last_active_at_utc.desc())
                )
                .mappings()
                .all()
            )
        return [
            {
                "id": record["id"],
                "device": record["device"],
                "last_active_at": _utc(record["last_active_at_utc"]).isoformat(),
                "current": record["id"] == current_session_id,
            }
            for record in sessions
        ]

    def revoke_all_sessions(self, *, user_id: object, reason: str) -> int:
        now = self._current_time()
        with self._engine.begin() as connection:
            user = (
                connection.execute(
                    select(identity_user_table)
                    .where(identity_user_table.c.id == str(user_id))
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if user is None or user["lifecycle_status"] != "active":
                return 0
            sessions = (
                connection.execute(
                    select(auth_session_table)
                    .where(
                        and_(
                            auth_session_table.c.user_id == str(user_id),
                            auth_session_table.c.revoked_at_utc.is_(None),
                        )
                    )
                    .with_for_update()
                )
                .mappings()
                .all()
            )
            if not sessions:
                return 0
            transition_version = int(user["transition_version"]) + 1
            updated = connection.execute(
                update(identity_user_table)
                .where(
                    and_(
                        identity_user_table.c.id == str(user_id),
                        identity_user_table.c.lifecycle_status == "active",
                        identity_user_table.c.transition_version == user["transition_version"],
                    )
                )
                .values(transition_version=transition_version, updated_at_utc=now)
            ).rowcount
            if updated != 1:
                raise PlatformError("session_revoked", "The session has been revoked", {}, 401)
            return self._revoke_account_sessions_in_transaction(
                connection,
                user_id=str(user_id),
                reason=reason,
                revoked_at=now,
                transition_version=transition_version,
            )

    def update_profile(self, *, user_id: object, display_name: str) -> dict[str, object]:
        normalized = display_name.strip()
        if not normalized or len(normalized) > 256:
            raise PlatformError("validation_error", "Display name is invalid", {}, 422)
        now = self._current_time()
        with self._engine.begin() as connection:
            updated = connection.execute(
                update(identity_user_table)
                .where(
                    and_(
                        identity_user_table.c.id == str(user_id),
                        identity_user_table.c.lifecycle_status == "active",
                    )
                )
                .values(display_name=normalized, updated_at_utc=now)
            ).rowcount
        if updated != 1:
            raise PlatformError("authentication_required", "The account is not active", {}, 401)
        return self.user_response(str(user_id))

    def replace_avatar(
        self,
        *,
        user_id: object,
        content: bytes,
        content_type: str,
    ) -> dict[str, str]:
        if not content or len(content) > 5 * 1024 * 1024 or not content_type.startswith("image/"):
            raise PlatformError("validation_error", "Avatar must be an image below 5 MiB", {}, 422)
        if self._object_store is None:
            raise PlatformError(
                "avatar_storage_unavailable", "Avatar storage is unavailable", {}, 503, True
            )
        key = f"avatars/{user_id}/{secrets.token_urlsafe(18)}"
        checksum = hashlib.sha256(content).hexdigest()
        try:
            self._object_store.put(
                key,
                content,
                ObjectMetadata(
                    content_type=content_type, size_bytes=len(content), checksum_sha256=checksum
                ),
            )
        except StorageKeyError as exc:
            raise PlatformError(
                "avatar_storage_unavailable", "Avatar storage is unavailable", {}, 503, True
            ) from exc
        url = f"object://{key}"
        now = self._current_time()
        try:
            with self._engine.begin() as connection:
                user = (
                    connection.execute(
                        select(identity_user_table)
                        .where(identity_user_table.c.id == str(user_id))
                        .with_for_update()
                    )
                    .mappings()
                    .one_or_none()
                )
                if user is None or user["lifecycle_status"] != "active":
                    raise PlatformError(
                        "authentication_required", "The account is not active", {}, 401
                    )
                old_url = user["avatar_url"]
                updated = connection.execute(
                    update(identity_user_table)
                    .where(
                        and_(
                            identity_user_table.c.id == str(user_id),
                            identity_user_table.c.lifecycle_status == "active",
                        )
                    )
                    .values(avatar_url=url, updated_at_utc=now)
                ).rowcount
                if updated != 1:
                    raise PlatformError(
                        "authentication_required", "The account is not active", {}, 401
                    )
                if isinstance(old_url, str) and old_url.startswith("object://"):
                    self._record_object_cleanup(
                        connection,
                        user_id=str(user_id),
                        object_key=old_url.removeprefix("object://"),
                        now=now,
                    )
        except Exception:
            self._delete_or_defer_uploaded_avatar(user_id=str(user_id), object_key=key, now=now)
            raise
        return {"avatar_url": url}

    @staticmethod
    def _object_cleanup_operation_id(object_key: str) -> str:
        return hashlib.sha256(f"identity-object-cleanup|{object_key}".encode()).hexdigest()

    def _record_object_cleanup(
        self,
        connection: Connection,
        *,
        user_id: str,
        object_key: str,
        now: datetime,
    ) -> str:
        operation_id = self._object_cleanup_operation_id(object_key)
        existing = (
            connection.execute(
                select(identity_object_cleanup_table).where(
                    identity_object_cleanup_table.c.operation_id == operation_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            if str(existing["user_id"]) != user_id or str(existing["object_key"]) != object_key:
                raise PlatformError(
                    "avatar_cleanup_conflict",
                    "Avatar cleanup conflicts with an existing operation",
                    {},
                    409,
                )
            return operation_id
        connection.execute(
            identity_object_cleanup_table.insert().values(
                operation_id=operation_id,
                user_id=user_id,
                object_key=object_key,
                created_at_utc=now,
                completed_at_utc=None,
            )
        )
        return operation_id

    def _delete_or_defer_uploaded_avatar(
        self,
        *,
        user_id: str,
        object_key: str,
        now: datetime,
    ) -> None:
        assert self._object_store is not None
        try:
            if self._object_store.exists(object_key):
                self._object_store.delete(object_key)
            return
        except StorageKeyError:
            pass
        try:
            with self._engine.begin() as connection:
                self._record_object_cleanup(
                    connection,
                    user_id=user_id,
                    object_key=object_key,
                    now=now,
                )
        except Exception as exc:
            raise PlatformError(
                "avatar_cleanup_unverified",
                "Avatar cleanup could not be recorded",
                {"retryable": True},
                503,
                True,
            ) from exc

    def list_pending_object_cleanup_operations(self, *, limit: int = 100) -> list[str]:
        if limit < 1 or limit > 1000:
            raise PlatformError("validation_error", "Object cleanup limit is invalid", {}, 422)
        with self._engine.connect() as connection:
            operation_ids = connection.execute(
                select(identity_object_cleanup_table.c.operation_id)
                .where(identity_object_cleanup_table.c.completed_at_utc.is_(None))
                .order_by(identity_object_cleanup_table.c.created_at_utc)
                .limit(limit)
            ).scalars()
            return [str(operation_id) for operation_id in operation_ids]

    def finalize_object_cleanup(
        self,
        *,
        operation_id: str,
        connection: Connection | None = None,
    ) -> dict[str, str]:
        now = self._current_time()
        with self._engine.begin() if connection is None else nullcontext(connection) as connection:
            cleanup = (
                connection.execute(
                    select(identity_object_cleanup_table)
                    .where(identity_object_cleanup_table.c.operation_id == operation_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if cleanup is None:
                raise PlatformError(
                    "object_cleanup_not_found", "Object cleanup was not found", {}, 404
                )
            if cleanup["completed_at_utc"] is not None:
                return {"operation_id": operation_id, "status": "completed"}
            if self._object_store is None:
                raise PlatformError(
                    "avatar_cleanup_unavailable",
                    "Avatar cleanup storage is unavailable",
                    {"retryable": True},
                    503,
                    True,
                )
            try:
                object_key = str(cleanup["object_key"])
                if self._object_store.exists(object_key):
                    self._object_store.delete(object_key)
            except StorageKeyError as exc:
                raise PlatformError(
                    "avatar_cleanup_unavailable",
                    "Avatar cleanup storage is unavailable",
                    {"retryable": True},
                    503,
                    True,
                ) from exc
            connection.execute(
                update(identity_object_cleanup_table)
                .where(identity_object_cleanup_table.c.operation_id == operation_id)
                .values(completed_at_utc=now)
            )
        return {"operation_id": operation_id, "status": "completed"}

    def get_preferences(self, *, user_id: object) -> dict[str, object]:
        with self._engine.connect() as connection:
            preferences = connection.execute(
                select(identity_user_table.c.preferences_json).where(
                    and_(
                        identity_user_table.c.id == str(user_id),
                        identity_user_table.c.lifecycle_status == "active",
                    )
                )
            ).scalar_one_or_none()
        if not isinstance(preferences, dict):
            raise PlatformError("authentication_required", "The account is not active", {}, 401)
        return dict(preferences)

    def replace_preferences(
        self,
        *,
        user_id: object,
        preferences: dict[str, object],
    ) -> dict[str, object]:
        required = {"theme", "chat_font_size", "ab_opt_out"}
        if set(preferences) != required or preferences["theme"] not in {"light", "dark", "system"}:
            raise PlatformError("validation_error", "Preferences are invalid", {}, 422)
        if preferences["chat_font_size"] not in {"standard", "large"} or not isinstance(
            preferences["ab_opt_out"], bool
        ):
            raise PlatformError("validation_error", "Preferences are invalid", {}, 422)
        stored = dict(preferences)
        with self._engine.begin() as connection:
            updated = connection.execute(
                update(identity_user_table)
                .where(
                    and_(
                        identity_user_table.c.id == str(user_id),
                        identity_user_table.c.lifecycle_status == "active",
                    )
                )
                .values(preferences_json=stored, updated_at_utc=self._current_time())
            ).rowcount
        if updated != 1:
            raise PlatformError("authentication_required", "The account is not active", {}, 401)
        return stored

    def change_password(self, *, user_id: object, old_password: str, new_password: str) -> None:
        _validate_password_rule(new_password)
        now = self._current_time()
        with self._engine.begin() as connection:
            user = (
                connection.execute(
                    select(identity_user_table)
                    .where(identity_user_table.c.id == str(user_id))
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if (
                user is None
                or user["lifecycle_status"] != "active"
                or not verify_password(old_password, user["password_hash"])
            ):
                raise PlatformError("wrong_old_password", "Current password is invalid", {}, 403)
            next_transition = int(user["transition_version"]) + 1
            updated = connection.execute(
                update(identity_user_table)
                .where(
                    and_(
                        identity_user_table.c.id == str(user_id),
                        identity_user_table.c.lifecycle_status == "active",
                        identity_user_table.c.transition_version == user["transition_version"],
                    )
                )
                .values(
                    password_hash=hash_password(new_password),
                    transition_version=next_transition,
                    updated_at_utc=now,
                )
            ).rowcount
            if updated != 1:
                raise PlatformError("wrong_old_password", "Current password is invalid", {}, 403)
            self._revoke_account_sessions_in_transaction(
                connection,
                user_id=str(user_id),
                reason="password_changed",
                revoked_at=now,
                transition_version=next_transition,
            )

    @staticmethod
    def _login_throttle_record(
        connection: Connection,
        normalized_username: str,
    ) -> dict[str, object] | None:
        record = (
            connection.execute(
                select(identity_login_throttle_table)
                .where(identity_login_throttle_table.c.normalized_username == normalized_username)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        return dict(record) if record is not None else None

    def _record_failed_login(
        self,
        connection: Connection,
        *,
        normalized_username: str,
        now: datetime,
    ) -> tuple[int, datetime | None]:
        for _ in range(4):
            throttle = self._login_throttle_record(connection, normalized_username)
            if throttle is None:
                try:
                    with connection.begin_nested():
                        connection.execute(
                            identity_login_throttle_table.insert().values(
                                normalized_username=normalized_username,
                                failed_attempts=1,
                                locked_until_utc=None,
                                updated_at_utc=now,
                            )
                        )
                except IntegrityError:
                    continue
                return 1, None

            prior_attempts = int(str(throttle["failed_attempts"]))
            prior_locked_until = _optional_utc(throttle["locked_until_utc"])
            if prior_locked_until is not None and prior_locked_until > now:
                return prior_attempts, prior_locked_until
            attempts = prior_attempts + 1
            locked_until = (
                now + timedelta(seconds=self._settings.login_lock_seconds)
                if attempts >= self._settings.login_max_attempts
                else None
            )
            conditions = [
                identity_login_throttle_table.c.normalized_username == normalized_username,
                identity_login_throttle_table.c.failed_attempts == prior_attempts,
            ]
            if prior_locked_until is None:
                conditions.append(identity_login_throttle_table.c.locked_until_utc.is_(None))
            else:
                conditions.append(
                    identity_login_throttle_table.c.locked_until_utc == prior_locked_until
                )
            updated = connection.execute(
                update(identity_login_throttle_table)
                .where(and_(*conditions))
                .values(
                    failed_attempts=attempts,
                    locked_until_utc=locked_until,
                    updated_at_utc=now,
                )
            ).rowcount
            if updated == 1:
                return attempts, locked_until
        raise PlatformError(
            "login_throttle_unavailable",
            "Login throttle could not be updated",
            {"retryable": True},
            503,
            True,
        )

    def login(self, *, username: str, password: str, device: str | None = None) -> AuthResult:
        normalized_username = _normalize_username(username)
        now = self._current_time()
        login_error: PlatformError | None = None
        user: dict[str, object] | None = None
        with self._engine.begin() as connection:
            throttle = self._login_throttle_record(connection, normalized_username)
            locked_until = (
                _optional_utc(throttle["locked_until_utc"]) if throttle is not None else None
            )
            if locked_until is not None and locked_until > now:
                remaining = max(1, int((locked_until - now).total_seconds()))
                login_error = PlatformError(
                    "too_many_attempts",
                    "Too many failed login attempts",
                    {"retry_after_seconds": remaining},
                    429,
                    True,
                )
            user_record = (
                connection.execute(
                    select(identity_user_table)
                    .where(identity_user_table.c.normalized_username == normalized_username)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            valid = (
                user_record is not None
                and user_record["lifecycle_status"] == "active"
                and verify_password(password, user_record["password_hash"])
            )
            if login_error is None and not valid:
                _, locked_until = self._record_failed_login(
                    connection,
                    normalized_username=normalized_username,
                    now=now,
                )
                login_error = (
                    PlatformError(
                        "too_many_attempts",
                        "Too many failed login attempts",
                        {"retry_after_seconds": self._settings.login_lock_seconds},
                        429,
                        True,
                    )
                    if locked_until is not None
                    else PlatformError(
                        "invalid_credentials", "Username or password is invalid", {}, 401
                    )
                )
            if login_error is None:
                assert user_record is not None
                user = dict(user_record)
                connection.execute(
                    delete(identity_login_throttle_table).where(
                        identity_login_throttle_table.c.normalized_username == normalized_username
                    )
                )
                session_id = _new_id("session")
                refresh_token = new_refresh_token()
                family_expires_at = now + self._settings.refresh_ttl
                connection.execute(
                    auth_session_table.insert().values(
                        id=session_id,
                        user_id=user["id"],
                        device=(device or "Unknown device").strip()[:256] or "Unknown device",
                        current_sequence=1,
                        family_expires_at_utc=family_expires_at,
                        last_active_at_utc=now,
                        created_at_utc=now,
                        revoked_at_utc=None,
                        revoked_reason=None,
                        identity_transition_version=user["transition_version"],
                    )
                )
                connection.execute(
                    auth_refresh_token_table.insert().values(
                        auth_session_id=session_id,
                        sequence=1,
                        token_hash=hash_refresh_token(refresh_token),
                        issued_at_utc=now,
                        consumed_at_utc=None,
                        replay_payload=None,
                        replay_expires_at_utc=None,
                    )
                )
        if login_error is not None:
            raise login_error
        assert user is not None
        access_token = sign_access_token(
            self._secret,
            user_id=str(user["id"]),
            auth_session_id=session_id,
            issued_at=now,
            expires_in_seconds=self._settings.access_ttl_seconds,
        )
        csrf_token = new_csrf_token(self._secret, session_id)
        return AuthResult(
            access_token=access_token,
            refresh_token=refresh_token,
            csrf_token=csrf_token,
            session_id=session_id,
            user=self.user_response(str(user["id"])),
        )

    def authenticate_access_token(self, token: str) -> AuthPrincipal:
        now = self._current_time()
        claims = verify_access_token(self._secret, token, now=now)
        if claims is None:
            raise PlatformError("authentication_required", "Access token is invalid", {}, 401)
        with self._engine.begin() as connection:
            session = (
                connection.execute(
                    select(auth_session_table).where(auth_session_table.c.id == claims["sid"])
                )
                .mappings()
                .one_or_none()
            )
            user = (
                connection.execute(
                    select(identity_user_table).where(identity_user_table.c.id == claims["sub"])
                )
                .mappings()
                .one_or_none()
            )
            if (
                session is None
                or session["user_id"] != claims["sub"]
                or session["revoked_at_utc"] is not None
            ):
                raise PlatformError("session_revoked", "The session has been revoked", {}, 401)
            if user is None or user["lifecycle_status"] != "active":
                raise PlatformError("authentication_required", "The account is not active", {}, 401)
            if int(session["identity_transition_version"]) != int(user["transition_version"]):
                raise PlatformError("session_revoked", "The session has been revoked", {}, 401)
            last_active = _utc(session["last_active_at_utc"])
            if (now - last_active).total_seconds() >= 60:
                connection.execute(
                    update(auth_session_table)
                    .where(auth_session_table.c.id == session["id"])
                    .values(last_active_at_utc=now)
                )
        return AuthPrincipal(
            user_id=str(user["id"]),
            auth_session_id=str(session["id"]),
            username=str(user["username"]),
            role=user["role"],
            department_id=user["department_id"],
        )

    def authenticate_session_action_token(self, token: str) -> SessionActionPrincipal:
        claims = verify_access_token(self._secret, token, now=self._current_time())
        if claims is None:
            raise PlatformError("authentication_required", "Access token is invalid", {}, 401)
        user_id = str(claims["sub"])
        session_id = str(claims["sid"])
        with self._engine.connect() as connection:
            session = (
                connection.execute(
                    select(auth_session_table).where(auth_session_table.c.id == session_id)
                )
                .mappings()
                .one_or_none()
            )
            user = (
                connection.execute(
                    select(identity_user_table).where(identity_user_table.c.id == user_id)
                )
                .mappings()
                .one_or_none()
            )
        return SessionActionPrincipal(
            user_id=user_id,
            auth_session_id=session_id,
            session_revoked=session is None
            or str(session["user_id"]) != user_id
            or session["revoked_at_utc"] is not None
            or user is None
            or int(session["identity_transition_version"]) != int(user["transition_version"]),
        )

    def revoke_session_for_action(
        self,
        *,
        principal: SessionActionPrincipal,
        session_id: str,
        reason: str,
    ) -> bool:
        if principal.session_revoked:
            if session_id != principal.auth_session_id:
                raise PlatformError("session_revoked", "The session has been revoked", {}, 401)
            return False
        return self.revoke_session(user_id=principal.user_id, session_id=session_id, reason=reason)

    def revoke_all_sessions_for_action(self, *, principal: SessionActionPrincipal) -> int:
        if not principal.session_revoked:
            return self.revoke_all_sessions(user_id=principal.user_id, reason="all_devices_revoked")
        with self._engine.connect() as connection:
            active_session = connection.execute(
                select(auth_session_table.c.id).where(
                    and_(
                        auth_session_table.c.user_id == principal.user_id,
                        auth_session_table.c.revoked_at_utc.is_(None),
                    )
                )
            ).scalar_one_or_none()
        if active_session is not None:
            raise PlatformError("session_revoked", "The session has been revoked", {}, 401)
        return 0

    @staticmethod
    def _revocation_operation_id(
        *,
        user_id: str,
        session_id: str | None,
        reason: str,
        transition_version: int,
    ) -> str:
        payload = f"{user_id}|{session_id or ''}|{reason}|{transition_version}".encode()
        return hashlib.sha256(payload).hexdigest()

    def _revoke_session_in_transaction(
        self,
        connection: Connection,
        *,
        session: dict[str, object],
        reason: str,
        revoked_at: datetime,
    ) -> bool:
        session_id = str(session["id"])
        user_id = str(session["user_id"])
        transition_version = int(str(session["identity_transition_version"]))
        claimed = connection.execute(
            update(auth_session_table)
            .where(
                and_(
                    auth_session_table.c.id == session_id,
                    auth_session_table.c.user_id == user_id,
                    auth_session_table.c.revoked_at_utc.is_(None),
                )
            )
            .values(revoked_at_utc=revoked_at, revoked_reason=reason)
        ).rowcount
        if claimed != 1:
            return False
        operation_id = self._revocation_operation_id(
            user_id=user_id,
            session_id=session_id,
            reason=reason,
            transition_version=transition_version,
        )
        command = GenerationRevocationCommand(
            operation_id=operation_id,
            user_id=user_id,
            auth_session_id=session_id,
            reason=reason,
            revoked_at=revoked_at,
            identity_transition_version=transition_version,
        )
        receipt = self._revocation_port.revoke(command, connection=connection)
        self._record_generation_revocation_receipt(connection, command=command, receipt=receipt)
        return True

    def _revoke_account_sessions_in_transaction(
        self,
        connection: Connection,
        *,
        user_id: str,
        reason: str,
        revoked_at: datetime,
        transition_version: int,
    ) -> int:
        revoked_count = connection.execute(
            update(auth_session_table)
            .where(
                and_(
                    auth_session_table.c.user_id == user_id,
                    auth_session_table.c.revoked_at_utc.is_(None),
                )
            )
            .values(
                identity_transition_version=transition_version,
                revoked_at_utc=revoked_at,
                revoked_reason=reason,
            )
        ).rowcount
        command = GenerationRevocationCommand(
            operation_id=self._revocation_operation_id(
                user_id=user_id,
                session_id=None,
                reason=reason,
                transition_version=transition_version,
            ),
            user_id=user_id,
            auth_session_id=None,
            reason=reason,
            revoked_at=revoked_at,
            identity_transition_version=transition_version,
        )
        receipt = self._revocation_port.revoke(command, connection=connection)
        self._record_generation_revocation_receipt(connection, command=command, receipt=receipt)
        return revoked_count

    @staticmethod
    def _record_generation_revocation_receipt(
        connection: Connection,
        *,
        command: GenerationRevocationCommand,
        receipt: object,
    ) -> None:
        if (
            not isinstance(receipt, GenerationRevocationReceipt)
            or not receipt.reference.strip()
            or receipt.state not in {"accepted", "completed"}
        ):
            raise PlatformError(
                "generation_revocation_unverified",
                "Generation revocation receipt is invalid",
                {"retryable": True},
                503,
                True,
            )
        existing = (
            connection.execute(
                select(identity_revocation_command_table).where(
                    identity_revocation_command_table.c.operation_id == command.operation_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            fields_match = (
                str(existing["user_id"]) == command.user_id
                and existing["auth_session_id"] == command.auth_session_id
                and str(existing["reason"]) == command.reason
                and int(existing["identity_transition_version"])
                == command.identity_transition_version
                and str(existing["receipt_reference"]) == receipt.reference
                and str(existing["receipt_state"]) == receipt.state
            )
            if not fields_match:
                raise PlatformError(
                    "generation_revocation_conflict",
                    "Generation revocation command conflicts with an existing operation",
                    {},
                    409,
                )
            return
        connection.execute(
            identity_revocation_command_table.insert().values(
                operation_id=command.operation_id,
                user_id=command.user_id,
                auth_session_id=command.auth_session_id,
                reason=command.reason,
                identity_transition_version=command.identity_transition_version,
                receipt_reference=receipt.reference,
                receipt_state=receipt.state,
                created_at_utc=command.revoked_at,
            )
        )

    def revoke_session(self, *, user_id: object, session_id: str, reason: str) -> bool:
        now = self._current_time()
        with self._engine.begin() as connection:
            session = (
                connection.execute(
                    select(auth_session_table)
                    .where(auth_session_table.c.id == session_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if session is None or str(session["user_id"]) != str(user_id):
                return False
            return self._revoke_session_in_transaction(
                connection,
                session=dict(session),
                reason=reason,
                revoked_at=now,
            )

    def refresh(
        self,
        *,
        refresh_token: str | None,
        csrf_cookie: str | None,
        csrf_header: str | None,
        origin: str | None,
    ) -> AuthResult:
        if not refresh_token:
            raise PlatformError("invalid_refresh", "Refresh token is invalid", {}, 401)
        now = self._current_time()
        token_hash = hash_refresh_token(refresh_token)
        reuse_detected = False
        with self._engine.begin() as connection:
            record = (
                connection.execute(
                    select(auth_refresh_token_table, auth_session_table, identity_user_table)
                    .join(
                        auth_session_table,
                        auth_refresh_token_table.c.auth_session_id == auth_session_table.c.id,
                    )
                    .join(
                        identity_user_table,
                        auth_session_table.c.user_id == identity_user_table.c.id,
                    )
                    .where(auth_refresh_token_table.c.token_hash == token_hash)
                    .with_for_update(of=auth_session_table)
                )
                .mappings()
                .one_or_none()
            )
            if record is None:
                raise PlatformError("invalid_refresh", "Refresh token is invalid", {}, 401)
            session_id = str(record[auth_session_table.c.id])
            if (
                csrf_cookie is None
                or csrf_header is None
                or not hmac.compare_digest(csrf_cookie, csrf_header)
                or not verify_csrf_token(self._secret, session_id, csrf_cookie)
                or (self._settings.allowed_origins and origin not in self._settings.allowed_origins)
            ):
                raise PlatformError("csrf_failed", "CSRF validation failed", {}, 403)
            if (
                record[auth_session_table.c.revoked_at_utc] is not None
                or record[identity_user_table.c.lifecycle_status] != "active"
                or int(record[auth_session_table.c.identity_transition_version])
                != int(record[identity_user_table.c.transition_version])
                or _utc(record[auth_session_table.c.family_expires_at_utc]) <= now
            ):
                raise PlatformError("invalid_refresh", "Refresh token is invalid", {}, 401)

            consumed_at = record[auth_refresh_token_table.c.consumed_at_utc]
            current_sequence = int(record[auth_session_table.c.current_sequence])
            token_sequence = int(record[auth_refresh_token_table.c.sequence])
            if consumed_at is not None:
                replay_expires_at = record[auth_refresh_token_table.c.replay_expires_at_utc]
                replay_payload = record[auth_refresh_token_table.c.replay_payload]
                if (
                    token_sequence == current_sequence - 1
                    and replay_expires_at is not None
                    and _utc(replay_expires_at) > now
                    and isinstance(replay_payload, str)
                ):
                    payload = decrypt_replay_payload(self._secret, replay_payload)
                    if payload is not None:
                        return AuthResult(
                            access_token=payload["access_token"],
                            refresh_token=payload["refresh_token"],
                            csrf_token=payload["csrf_token"],
                            session_id=session_id,
                            user=self._user_response_for_id(
                                connection,
                                str(record[identity_user_table.c.id]),
                            ),
                        )
                connection.execute(
                    update(auth_refresh_token_table)
                    .where(
                        and_(
                            auth_refresh_token_table.c.auth_session_id == session_id,
                            auth_refresh_token_table.c.sequence
                            == record[auth_refresh_token_table.c.sequence],
                        )
                    )
                    .values(replay_payload=None, replay_expires_at_utc=None)
                )
                reuse_detected = self._revoke_session_in_transaction(
                    connection,
                    session={
                        "id": record[auth_session_table.c.id],
                        "user_id": record[auth_session_table.c.user_id],
                        "revoked_at_utc": record[auth_session_table.c.revoked_at_utc],
                        "identity_transition_version": record[
                            auth_session_table.c.identity_transition_version
                        ],
                    },
                    reason="refresh_reuse_detected",
                    revoked_at=now,
                )
            else:
                if token_sequence != current_sequence:
                    raise PlatformError("invalid_refresh", "Refresh token is invalid", {}, 401)
                connection.execute(
                    update(auth_refresh_token_table)
                    .where(
                        and_(
                            auth_refresh_token_table.c.auth_session_id == session_id,
                            auth_refresh_token_table.c.consumed_at_utc.is_not(None),
                        )
                    )
                    .values(replay_payload=None, replay_expires_at_utc=None)
                )
                next_sequence = current_sequence + 1
                successor_refresh_token = new_refresh_token()
                successor_access_token = sign_access_token(
                    self._secret,
                    user_id=str(record[identity_user_table.c.id]),
                    auth_session_id=session_id,
                    issued_at=now,
                    expires_in_seconds=self._settings.access_ttl_seconds,
                )
                successor_csrf_token = new_csrf_token(self._secret, session_id)
                replay_payload = encrypt_replay_payload(
                    self._secret,
                    {
                        "access_token": successor_access_token,
                        "refresh_token": successor_refresh_token,
                        "csrf_token": successor_csrf_token,
                    },
                )
                consumed = connection.execute(
                    update(auth_refresh_token_table)
                    .where(
                        and_(
                            auth_refresh_token_table.c.auth_session_id == session_id,
                            auth_refresh_token_table.c.sequence
                            == record[auth_refresh_token_table.c.sequence],
                            auth_refresh_token_table.c.consumed_at_utc.is_(None),
                        )
                    )
                    .values(
                        consumed_at_utc=now,
                        replay_payload=replay_payload,
                        replay_expires_at_utc=now
                        + timedelta(seconds=self._settings.refresh_reuse_grace_seconds),
                    )
                ).rowcount
                if consumed != 1:
                    raise PlatformError("invalid_refresh", "Refresh token is invalid", {}, 401)
                connection.execute(
                    auth_refresh_token_table.insert().values(
                        auth_session_id=session_id,
                        sequence=next_sequence,
                        token_hash=hash_refresh_token(successor_refresh_token),
                        issued_at_utc=now,
                        consumed_at_utc=None,
                        replay_payload=None,
                        replay_expires_at_utc=None,
                    )
                )
                connection.execute(
                    update(auth_session_table)
                    .where(auth_session_table.c.id == session_id)
                    .values(current_sequence=next_sequence, last_active_at_utc=now)
                )
        if reuse_detected:
            raise PlatformError(
                "refresh_reuse_detected",
                "Refresh token reuse was detected",
                {},
                401,
            )
        return AuthResult(
            access_token=successor_access_token,
            refresh_token=successor_refresh_token,
            csrf_token=successor_csrf_token,
            session_id=session_id,
            user=self.user_response(str(record[identity_user_table.c.id])),
        )
