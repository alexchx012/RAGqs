"""Uvicorn entrypoint for the versioned core platform API."""

from app.platform.app_factory import create_platform_app

app = create_platform_app()
