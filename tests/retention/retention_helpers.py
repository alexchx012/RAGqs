"""Shared test helpers for the retention domain tests."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.identity.ports import NoopDepartmentWorkCheckPort
from app.identity.revocation import NoopGenerationRevocationPort
from app.identity.service import IdentityAccessService
from app.platform.config import PlatformSettings, load_platform_settings

_ENGINES: list = []


def fixed_now() -> datetime:
    return datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)


def build_engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _ENGINES.append(engine)
    return engine


def dispose_all_test_engines() -> None:
    for engine in list(_ENGINES):
        engine.dispose()
    _ENGINES.clear()


def make_settings() -> PlatformSettings:
    return load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
            "RAG_AUTH_SECRET_KEY": "test-secret-that-is-long-enough",
        }
    )


def build_identity_service(engine) -> IdentityAccessService:
    configured = make_settings()
    return IdentityAccessService(
        engine,
        configured.auth,
        revocation_port=NoopGenerationRevocationPort(),
        department_work_check=NoopDepartmentWorkCheckPort(),
    )


def provision_user(service: IdentityAccessService, username: str, role: str) -> dict:
    user = service.provision_user(
        username=username,
        password="Password1",
        real_name=username.title(),
        display_name=username.title(),
        role=role,
        department_id=None,
    )
    token = service.login(username=username, password="Password1").access_token
    return {"record": user, "token": token}
