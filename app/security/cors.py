"""CORS configuration helpers."""

from __future__ import annotations

from typing import Any


def parse_cors_origins(value: str) -> list[str]:
    """Parse comma-separated CORS origins from settings."""

    return [origin.strip() for origin in value.split(",") if origin.strip()]


def build_cors_options(settings: Any) -> dict[str, Any]:
    """Return FastAPI CORSMiddleware options from app settings."""

    return {
        "allow_origins": parse_cors_origins(
            _settings_value(
                settings,
                "cors",
                "allow_origins",
                "cors_allow_origins",
                "http://127.0.0.1:9900,http://localhost:9900",
            )
        ),
        "allow_credentials": bool(
            _settings_value(settings, "cors", "allow_credentials", "cors_allow_credentials", True)
        ),
        "allow_methods": ["*"],
        "allow_headers": ["*"],
    }


def _settings_value(
    settings: Any,
    group_name: str,
    group_field_name: str,
    flat_field_name: str,
    default: Any,
) -> Any:
    group = getattr(settings, group_name, None)
    if group is not None and hasattr(group, group_field_name):
        return getattr(group, group_field_name)
    return getattr(settings, flat_field_name, default)
