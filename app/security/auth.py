"""Minimal authentication and authorization boundary for internal RAGqs use."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from fastapi import Depends, HTTPException, Request

from app.config import config

LOCAL_CREDENTIALS_PROVIDER = "local_credentials"
LOCAL_SESSION_COOKIE_NAME = "rag_session"

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin": {"*"},
    "viewer": {
        "chat:write",
        "chat:read",
        "session:read",
        "space:read",
        "document:read",
        "index_job:read",
    },
    "uploader": {
        "document:upload",
        "document:read",
        "index_job:read",
        "space:read",
    },
    "maintainer": {
        "document:upload",
        "document:read",
        "document:delete",
        "document:rebuild",
        "index_job:read",
        "index_job:retry",
        "space:read",
        "space:write",
    },
    "auditor": {
        "audit:read",
        "space:read",
        "document:read",
        "index_job:read",
    },
    "ops": {
        "audit:read",
        "metrics:read",
        "space:read",
        "document:read",
        "index_job:read",
    },
}


@dataclass(frozen=True)
class AuthContext:
    """Authenticated internal user and authorization scope."""

    user_id: str
    roles: set[str] = field(default_factory=set)
    spaces: set[str] = field(default_factory=set)
    provider: str = "disabled"
    metadata: dict[str, Any] = field(default_factory=dict)

    def has_permission(self, permission: str) -> bool:
        normalized = _normalize_permission(permission)
        for role in self.roles:
            permissions = ROLE_PERMISSIONS.get(role, set())
            if "*" in permissions or normalized in permissions:
                return True
        return False

    def can_access_space(self, space_id: str | None) -> bool:
        normalized = _normalize_space(space_id)
        return "*" in self.spaces or normalized in self.spaces


class SimpleAuthProvider:
    """Header/static auth provider for local and reverse-proxy deployments."""

    def __init__(self, settings: Any = config):
        self.settings = settings

    def authenticate(
        self,
        headers: Mapping[str, str] | None = None,
        *,
        cookies: Mapping[str, str] | None = None,
    ) -> AuthContext:
        if not _setting_bool(self.settings, "auth_enabled", False):
            return self._default_context(provider="disabled")

        provider = _setting_id(_setting_value(self.settings, "auth_provider", "dev_header"))
        if provider == LOCAL_CREDENTIALS_PROVIDER:
            return self._authenticate_local_credentials(cookies or {})
        if provider not in {"dev_header", "reverse_proxy"}:
            raise HTTPException(status_code=500, detail=f"unsupported auth provider: {provider}")

        header_map = _normalize_headers(headers or {})
        user_header = _setting_value(self.settings, "auth_user_header", "X-RAG-User").lower()
        user_id = header_map.get(user_header, "").strip()
        if not user_id:
            raise HTTPException(status_code=401, detail=f"missing auth header: {user_header}")

        dev_users = parse_dev_users(_setting_value(self.settings, "auth_dev_users", ""))
        if user_id in dev_users:
            roles, spaces = dev_users[user_id]
            return AuthContext(user_id=user_id, roles=roles, spaces=spaces, provider=provider)

        if provider == "dev_header":
            raise HTTPException(status_code=401, detail=f"unknown dev auth user: {user_id}")

        roles_header = _setting_value(self.settings, "auth_roles_header", "X-RAG-Roles").lower()
        spaces_header = _setting_value(self.settings, "auth_spaces_header", "X-RAG-Spaces").lower()
        roles = _parse_list(header_map.get(roles_header, "viewer"))
        spaces = _parse_list(header_map.get(spaces_header, "default"))
        return AuthContext(user_id=user_id, roles=roles, spaces=spaces, provider=provider)

    def _default_context(self, *, provider: str) -> AuthContext:
        return AuthContext(
            user_id=_setting_value(self.settings, "auth_default_user_id", "local-admin"),
            roles=_parse_list(_setting_value(self.settings, "auth_default_roles", "admin")),
            spaces=_parse_list(_setting_value(self.settings, "auth_default_spaces", "*")),
            provider=provider,
        )

    def _authenticate_local_credentials(self, cookies: Mapping[str, str]) -> AuthContext:
        # Deferred import: local_auth_service imports AuthContext from this module at its own
        # module top-level, so importing it back at this module's top level would be circular.
        from app.security.local_auth_service import get_local_auth_service

        token = str(cookies.get(LOCAL_SESSION_COOKIE_NAME, "")).strip()
        if not token:
            raise HTTPException(
                status_code=401,
                detail=f"missing session cookie: {LOCAL_SESSION_COOKIE_NAME}",
            )
        auth_context = get_local_auth_service(self.settings).resolve(token)
        if auth_context is None:
            raise HTTPException(status_code=401, detail="invalid or expired session")
        return auth_context


async def get_current_auth_context(request: Request) -> AuthContext:
    """FastAPI dependency that resolves the current auth context."""

    return SimpleAuthProvider(config).authenticate(request.headers, cookies=request.cookies)


def require_permission(permission: str):
    """Return a FastAPI dependency enforcing one permission."""

    async def dependency(
        auth_context: AuthContext = Depends(get_current_auth_context),
    ) -> AuthContext:
        require_context_permission(auth_context, permission)
        return auth_context

    return dependency


def require_context_permission(auth_context: AuthContext, permission: str) -> None:
    """Raise HTTP 403 when a context lacks a permission."""

    if not auth_context.has_permission(permission):
        raise HTTPException(status_code=403, detail=f"missing permission: {permission}")


def require_space_access(auth_context: AuthContext, space_id: str | None) -> None:
    """Raise HTTP 403 when a context cannot access a knowledge space."""

    normalized = _normalize_space(space_id)
    if not auth_context.can_access_space(normalized):
        raise HTTPException(
            status_code=403,
            detail=f"user is not allowed to access knowledge space: {normalized}",
        )


def active_auth_context(value: Any) -> AuthContext:
    """Support direct unit-test calls to route functions with FastAPI Depends defaults."""

    if isinstance(value, AuthContext):
        return value
    return SimpleAuthProvider(config).authenticate({})


def is_all_space_context(auth_context: AuthContext) -> bool:
    return "*" in auth_context.spaces


def parse_dev_users(value: str) -> dict[str, tuple[set[str], set[str]]]:
    """Parse AUTH_DEV_USERS entries: user:role|role:space|space;user2:admin:*."""

    users: dict[str, tuple[set[str], set[str]]] = {}
    for raw_entry in str(value or "").split(";"):
        entry = raw_entry.strip()
        if not entry:
            continue
        parts = [part.strip() for part in entry.split(":", 2)]
        if len(parts) != 3:
            continue
        user_id, roles, spaces = parts
        if user_id:
            users[user_id] = (_parse_list(roles), _parse_list(spaces))
    return users


def _parse_list(value: str) -> set[str]:
    items = str(value or "").replace("|", ",").split(",")
    return {item.strip().lower() for item in items if item.strip()} or {"default"}


def _normalize_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in headers.items()}


def _normalize_permission(permission: str) -> str:
    return str(permission).strip().lower()


def _normalize_space(space_id: str | None) -> str:
    return str(space_id or "default").strip() or "default"


def _setting_bool(settings: Any, name: str, default: bool) -> bool:
    value = _setting_value(settings, name, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _setting_value(settings: Any, name: str, default: Any) -> Any:
    group = getattr(settings, "auth", None)
    group_name = name.removeprefix("auth_")
    if group is not None and hasattr(group, group_name):
        return getattr(group, group_name)
    return getattr(settings, name, default)


def _setting_id(value: Any) -> str:
    return str(value).strip().lower().replace("-", "_")
