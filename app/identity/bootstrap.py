from __future__ import annotations

import logging

from app.platform.config import PlatformSettings, load_platform_settings
from app.platform.runtime import PlatformRuntime, build_runtime

from .service import IdentityAccessService

_logger = logging.getLogger(__name__)


def _bootstrap_values(settings: PlatformSettings) -> tuple[str, str, str, str]:
    auth = settings.auth
    if (
        auth.bootstrap_username is None
        or auth.bootstrap_password is None
        or auth.bootstrap_real_name is None
        or auth.bootstrap_display_name is None
    ):
        raise ValueError("RAG_AUTH_BOOTSTRAP_* settings are required for admin bootstrap")
    return (
        auth.bootstrap_username,
        auth.bootstrap_password.get_secret_value(),
        auth.bootstrap_real_name,
        auth.bootstrap_display_name,
    )


def run_initial_admin_bootstrap(
    settings: PlatformSettings,
    *,
    runtime: PlatformRuntime | None = None,
) -> dict[str, object]:
    active_runtime = runtime or build_runtime(settings)
    owns_runtime = runtime is None
    try:
        identity_access = active_runtime.resolve("identity_access")
        if not isinstance(identity_access, IdentityAccessService):
            raise RuntimeError("identity access service is not configured")
        username, password, real_name, display_name = _bootstrap_values(settings)
        return identity_access.bootstrap_initial_admin(
            username=username,
            password=password,
            real_name=real_name,
            display_name=display_name,
        )
    finally:
        if owns_runtime:
            active_runtime.close()


def main() -> None:
    result = run_initial_admin_bootstrap(load_platform_settings())
    _logger.info("initial administrator bootstrap completed for user_id=%s", result["id"])
