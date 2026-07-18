"""Administrator-facing service for managing local users safely."""

from __future__ import annotations

from typing import Any

from app.config import config
from app.security.auth import AuthContext, ROLE_PERMISSIONS
from app.security.password import hash_password
from app.security.session_store import SessionStore
from app.security.user_store import (
    LastAdminProtectionError,
    UserManageScopeConflictError,
    UsernameAlreadyExistsError,
    UserNotFoundError,
    UserRecord,
    UserStore,
    UserVersionConflictError,
)

_VALIDATION_ERROR = "invalid administrator user input"
_ALREADY_EXISTS_ERROR = "administrator user already exists"
_NOT_FOUND_ERROR = "administrator user not found"
_VERSION_CONFLICT_ERROR = "administrator user version conflict"
_LAST_ADMIN_ERROR = "cannot remove last administrator"
_SCOPE_ERROR = "administrator lacks management scope for this operation"
_DEPARTMENT_REQUIRED_ERROR = "department_admin role requires a department_id"


class AdminUserServiceError(Exception):
    """Base error for administrator user operations."""


class AdminUserValidationError(AdminUserServiceError):
    """Raised when administrator user input violates a domain rule."""


class AdminUserAlreadyExistsError(AdminUserServiceError):
    """Raised when a username is already registered."""


class AdminUserNotFoundError(AdminUserServiceError):
    """Raised when a requested user does not exist."""


class AdminUserVersionConflictError(AdminUserServiceError):
    """Raised when a mutation uses a stale user version."""


class LastAdminError(AdminUserServiceError):
    """Raised when a mutation would remove the last administrator."""


class AdminUserScopeError(AdminUserServiceError):
    """Raised when an actor exceeds its department scope or role-write authority."""


class AdminUserService:
    """Apply administrator user rules around the user and session stores."""

    def __init__(
        self,
        *,
        settings: Any = None,
        user_store: UserStore | None = None,
        session_store: SessionStore | None = None,
    ) -> None:
        self.settings = settings if settings is not None else config
        db_path = _auth_setting(self.settings, "local_db_path", "data/auth.sqlite3")
        self.user_store = user_store if user_store is not None else UserStore(db_path)
        self.session_store = session_store if session_store is not None else SessionStore(db_path)

    def list_users(self, *, actor: AuthContext | None = None) -> list[dict[str, Any]]:
        """Return every user without exposing credential hashes."""

        actor_is_super_admin = True if actor is None else "super_admin" in actor.roles
        actor_department_id = None if actor is None else actor.department_id

        users = self.user_store.list_users()
        if not actor_is_super_admin:
            if actor_department_id is None:
                return []
            users = [user for user in users if user.department_id == actor_department_id]
        return [self._safe_user(user) for user in users]

    def get_user(self, user_id: str, *, actor: AuthContext | None = None) -> dict[str, Any]:
        """Return one user without exposing its credential hash."""

        actor_is_super_admin = True if actor is None else "super_admin" in actor.roles
        actor_department_id = None if actor is None else actor.department_id

        try:
            user = self.user_store.get_by_id(user_id)
        except UserNotFoundError as exc:
            raise AdminUserNotFoundError(_NOT_FOUND_ERROR) from exc
        if user is None:
            raise AdminUserNotFoundError(_NOT_FOUND_ERROR)
        if not actor_is_super_admin:
            if actor_department_id is None or user.department_id != actor_department_id:
                raise AdminUserNotFoundError(_NOT_FOUND_ERROR)
        return self._safe_user(user)

    def create_user(
        self,
        *,
        actor: AuthContext | None = None,
        username: str,
        password: str,
        roles: list[str],
        spaces: list[str],
        department_id: str | None = None,
    ) -> dict[str, Any]:
        """Validate, hash, and persist a new local user within the actor's management scope."""

        normalized_username = _normalize_username(username)
        normalized_password = _normalize_password(password)
        normalized_roles = _normalize_roles(roles)
        normalized_spaces = _normalize_spaces(spaces)

        actor_is_super_admin = True if actor is None else "super_admin" in actor.roles
        actor_department_id = None if actor is None else actor.department_id

        if not actor_is_super_admin and {"super_admin", "department_admin"} & set(normalized_roles):
            raise AdminUserScopeError(_SCOPE_ERROR)

        if actor_is_super_admin:
            resolved_department_id = department_id
        else:
            if department_id is not None and department_id != actor_department_id:
                raise AdminUserScopeError(_SCOPE_ERROR)
            resolved_department_id = actor_department_id

        if "department_admin" in normalized_roles and resolved_department_id is None:
            raise AdminUserValidationError(_DEPARTMENT_REQUIRED_ERROR)

        try:
            user = self.user_store.create_user(
                username=normalized_username,
                password_hash=hash_password(normalized_password),
                roles=normalized_roles,
                spaces=normalized_spaces,
                department_ids=_normalize_department_ids(resolved_department_id),
            )
        except UsernameAlreadyExistsError as exc:
            raise AdminUserAlreadyExistsError(_ALREADY_EXISTS_ERROR) from exc
        return self._safe_user(user)

    def update_user(
        self,
        *,
        actor: AuthContext | None = None,
        user_id: str,
        expected_version: int,
        roles: list[str] | None = None,
        spaces: list[str] | None = None,
        department_id: str | None = None,
    ) -> dict[str, Any]:
        """Update optional role, space, and department fields using an optimistic version."""

        normalized_roles = None if roles is None else _normalize_roles(roles)
        normalized_spaces = None if spaces is None else _normalize_spaces(spaces)

        actor_is_super_admin = True if actor is None else "super_admin" in actor.roles
        actor_department_id = None if actor is None else actor.department_id

        if (
            normalized_roles is not None
            and not actor_is_super_admin
            and {"super_admin", "department_admin"} & set(normalized_roles)
        ):
            raise AdminUserScopeError(_SCOPE_ERROR)
        if (
            normalized_roles is not None
            and "department_admin" in normalized_roles
            and department_id is None
        ):
            raise AdminUserValidationError(_DEPARTMENT_REQUIRED_ERROR)

        try:
            user = self.user_store.update_user(
                user_id=user_id,
                expected_version=expected_version,
                actor_is_super_admin=actor_is_super_admin,
                actor_department_id=actor_department_id,
                roles=normalized_roles,
                spaces=normalized_spaces,
                department_ids=(None if department_id is None else [department_id]),
            )
        except UserManageScopeConflictError as exc:
            raise AdminUserScopeError(_SCOPE_ERROR) from exc
        except UserVersionConflictError as exc:
            raise AdminUserVersionConflictError(_VERSION_CONFLICT_ERROR) from exc
        except LastAdminProtectionError as exc:
            raise LastAdminError(_LAST_ADMIN_ERROR) from exc
        except UserNotFoundError as exc:
            raise AdminUserNotFoundError(_NOT_FOUND_ERROR) from exc
        return self._safe_user(user)

    def delete_user(
        self, *, actor: AuthContext | None = None, user_id: str, expected_version: int
    ) -> dict[str, str | bool]:
        """Delete a user and revoke its sessions only after deletion commits."""

        actor_is_super_admin = True if actor is None else "super_admin" in actor.roles
        actor_department_id = None if actor is None else actor.department_id

        try:
            self.user_store.delete_user(
                user_id=user_id,
                expected_version=expected_version,
                actor_is_super_admin=actor_is_super_admin,
                actor_department_id=actor_department_id,
            )
        except UserManageScopeConflictError as exc:
            raise AdminUserScopeError(_SCOPE_ERROR) from exc
        except UserVersionConflictError as exc:
            raise AdminUserVersionConflictError(_VERSION_CONFLICT_ERROR) from exc
        except LastAdminProtectionError as exc:
            raise LastAdminError(_LAST_ADMIN_ERROR) from exc
        except UserNotFoundError as exc:
            raise AdminUserNotFoundError(_NOT_FOUND_ERROR) from exc

        self.session_store.revoke_all_for_user(user_id)
        return {"deleted": True, "user_id": user_id}

    @staticmethod
    def _safe_user(user: UserRecord) -> dict[str, Any]:
        """Project a stored user to the seven fields safe for service callers."""

        return {
            "id": user.id,
            "username": user.username,
            "roles": list(user.roles),
            "spaces": list(user.spaces),
            "department_id": user.department_id,
            "version": user.version,
            "created_at": user.created_at,
        }


def _normalize_username(username: str) -> str:
    if not isinstance(username, str):
        raise AdminUserValidationError(_VALIDATION_ERROR)
    normalized = username.strip()
    if not normalized:
        raise AdminUserValidationError(_VALIDATION_ERROR)
    return normalized


def _normalize_password(password: str) -> str:
    if not isinstance(password, str) or password == "":
        raise AdminUserValidationError(_VALIDATION_ERROR)
    return password


def _normalize_roles(roles: list[str]) -> list[str]:
    if not isinstance(roles, list):
        raise AdminUserValidationError(_VALIDATION_ERROR)

    normalized: list[str] = []
    seen: set[str] = set()
    for role in roles:
        if not isinstance(role, str):
            raise AdminUserValidationError(_VALIDATION_ERROR)
        value = role.lower()
        if value not in ROLE_PERMISSIONS:
            raise AdminUserValidationError(_VALIDATION_ERROR)
        if value not in seen:
            normalized.append(value)
            seen.add(value)
    return normalized


def _normalize_spaces(spaces: list[str]) -> list[str]:
    if not isinstance(spaces, list):
        raise AdminUserValidationError(_VALIDATION_ERROR)

    normalized: list[str] = []
    seen: set[str] = set()
    for space in spaces:
        if not isinstance(space, str):
            raise AdminUserValidationError(_VALIDATION_ERROR)
        value = space.strip()
        if not value:
            raise AdminUserValidationError(_VALIDATION_ERROR)
        if value not in seen:
            normalized.append(value)
            seen.add(value)
    return normalized


def _normalize_department_ids(department_id: str | None) -> list[str]:
    return [] if department_id is None else [department_id]


def _auth_setting(settings: Any, name: str, default: Any) -> Any:
    group = getattr(settings, "auth", None)
    if group is not None and hasattr(group, name):
        return getattr(group, name)
    return getattr(settings, f"auth_{name}", default)
