"""CORS configuration helpers."""

from __future__ import annotations

from typing import Any

from app.config import Settings


def parse_cors_origins(value: str) -> list[str]:
    """Parse comma-separated CORS origins from settings."""

    return [origin.strip() for origin in value.split(",") if origin.strip()]


def build_cors_options(settings: Settings) -> dict[str, Any]:
    """Return FastAPI CORSMiddleware options from app settings."""

    return {
        "allow_origins": parse_cors_origins(settings.cors_allow_origins),
        "allow_credentials": settings.cors_allow_credentials,
        "allow_methods": ["*"],
        "allow_headers": ["*"],
    }
