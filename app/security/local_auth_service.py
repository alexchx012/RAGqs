"""Single orchestration entry point for local username/password auth.

Route handlers and app/security/auth.py depend only on this module; they must
not import user_store.py / session_store.py / password.py directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger

from app.config import config
from app.security.auth import AuthContext
from app.security.password import hash_password, verify_password
from app.security.session_store import SessionStore
from app.security.user_store import UserStore

_LOGIN_FAILURE_MESSAGE = "invalid username or password"


class LocalAuthError(Exception):
    """Raised when username/password login fails. Message is safe to show to clients."""


@dataclass(frozen=True)
class LoginResult:
    """Successful login outcome: issued session token plus user identity."""

    token: str
    user_id: str
    roles: set[str]
    spaces: set[str]


class LocalAuthService:
    """Orchestrates password.py, user_store.py, and session_store.py."""

    def __init__(
        self,
        *,
        settings: Any = None,
        user_store: UserStore | None = None,
        session_store: SessionStore | None = None,
    ):
        self.settings = settings if settings is not None else config
        db_path = _auth_setting(self.settings, "local_db_path", "data/auth.sqlite3")
        self.user_store = user_store if user_store is not None else UserStore(db_path)
        self.session_store = (
            session_store if session_store is not None else SessionStore(db_path)
        )

    def login(self, username: str, password: str) -> LoginResult:
        user = self.user_store.get_by_username(username)
        if user is None:
            logger.info(f"local auth login failed: unknown username={username!r}")
            raise LocalAuthError(_LOGIN_FAILURE_MESSAGE)
        if not verify_password(password, user.password_hash):
            logger.info(f"local auth login failed: wrong password for username={username!r}")
            raise LocalAuthError(_LOGIN_FAILURE_MESSAGE)

        ttl_seconds = int(_auth_setting(self.settings, "session_ttl_seconds", 604800))
        session = self.session_store.create_session(user.id, ttl_seconds)
        return LoginResult(
            token=session.token,
            user_id=user.id,
            roles=set(user.roles),
            spaces=set(user.spaces),
        )

    def resolve(self, token: str) -> AuthContext | None:
        session = self.session_store.get_valid_session(token)
        if session is None:
            return None
        user = self.user_store.get_by_id(session.user_id)
        if user is None:
            return None
        return AuthContext(
            user_id=user.id,
            roles=set(user.roles),
            spaces=set(user.spaces),
            provider="local_credentials",
            metadata={},
        )

    def logout(self, token: str) -> None:
        self.session_store.revoke_session(token)

    def seed_initial_admin(self) -> None:
        if self.user_store.count_users() > 0:
            return
        seed = _auth_setting(self.settings, "local_admin_seed", None)
        if not seed:
            return
        username, _, password = str(seed).partition(":")
        username = username.strip()
        if not username or not password:
            return
        self.user_store.create_user(
            username=username,
            password_hash=hash_password(password),
            roles=["admin"],
            spaces=["*"],
        )


def _auth_setting(settings: Any, name: str, default: Any) -> Any:
    group = getattr(settings, "auth", None)
    if group is not None and hasattr(group, name):
        return getattr(group, name)
    return getattr(settings, f"auth_{name}", default)


_default_local_auth_service: LocalAuthService | None = None


def get_local_auth_service(settings: Any = config) -> LocalAuthService:
    """Return the process-wide LocalAuthService singleton."""

    global _default_local_auth_service
    if _default_local_auth_service is None:
        _default_local_auth_service = LocalAuthService(settings=settings)
    return _default_local_auth_service


def reset_local_auth_service() -> None:
    """Reset the default LocalAuthService; intended for tests."""

    global _default_local_auth_service
    _default_local_auth_service = None
