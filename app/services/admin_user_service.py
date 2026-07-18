"""Administrator-facing service for managing local users safely."""

from __future__ import annotations

from typing import Any

from app.config import config
from app.security.auth import ROLE_PERMISSIONS
from app.security.password import hash_password
from app.security.session_store import SessionStore
from app.security.user_store import (
    LastAdminProtectionError,
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

    def list_users(self) -> list[dict[str, Any]]:
        """Return every user without exposing credential hashes."""

        return [self._safe_user(user) for user in self.user_store.list_users()]

    def get_user(self, user_id: str) -> dict[str, Any]:
        """Return one user without exposing its credential hash."""

        try:
            user = self.user_store.get_by_id(user_id)
        except UserNotFoundError as exc:
            raise AdminUserNotFoundError(_NOT_FOUND_ERROR) from exc
        if user is None:
            raise AdminUserNotFoundError(_NOT_FOUND_ERROR)
        return self._safe_user(user)

    def create_user(
        self,
        *,
        username: str,
        password: str,
        roles: list[str],
        spaces: list[str],
    ) -> dict[str, Any]:
        """Validate, hash, and persist a new local user."""

        normalized_username = _normalize_username(username)
        normalized_password = _normalize_password(password)
        normalized_roles = _normalize_roles(roles)
        normalized_spaces = _normalize_spaces(spaces)

        try:
            user = self.user_store.create_user(
                username=normalized_username,
                password_hash=hash_password(normalized_password),
                roles=normalized_roles,
                spaces=normalized_spaces,
            )
        except UsernameAlreadyExistsError as exc:
            raise AdminUserAlreadyExistsError(_ALREADY_EXISTS_ERROR) from exc
        return self._safe_user(user)

    def update_user(
        self,
        *,
        user_id: str,
        expected_version: int,
        roles: list[str] | None = None,
        spaces: list[str] | None = None,
    ) -> dict[str, Any]:
        """Update optional role and space fields using an optimistic version."""

        normalized_roles = None if roles is None else _normalize_roles(roles)
        normalized_spaces = None if spaces is None else _normalize_spaces(spaces)

        try:
            user = self.user_store.update_user(
                user_id=user_id,
                expected_version=expected_version,
                actor_is_super_admin=True,
                actor_department_id=None,
                roles=normalized_roles,
                spaces=normalized_spaces,
            )
        except UserVersionConflictError as exc:
            raise AdminUserVersionConflictError(_VERSION_CONFLICT_ERROR) from exc
        except LastAdminProtectionError as exc:
            raise LastAdminError(_LAST_ADMIN_ERROR) from exc
        except UserNotFoundError as exc:
            raise AdminUserNotFoundError(_NOT_FOUND_ERROR) from exc
        return self._safe_user(user)

    def delete_user(self, *, user_id: str, expected_version: int) -> dict[str, str | bool]:
        """Delete a user and revoke its sessions only after deletion commits."""

        try:
            self.user_store.delete_user(
                user_id=user_id,
                expected_version=expected_version,
                actor_is_super_admin=True,
                actor_department_id=None,
            )
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
        """Project a stored user to the six fields safe for service callers."""

        return {
            "id": user.id,
            "username": user.username,
            "roles": list(user.roles),
            "spaces": list(user.spaces),
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


def _auth_setting(settings: Any, name: str, default: Any) -> Any:
    group = getattr(settings, "auth", None)
    if group is not None and hasattr(group, name):
        return getattr(group, name)
    return getattr(settings, f"auth_{name}", default)
