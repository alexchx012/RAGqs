from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.platform.config import load_platform_settings, validate_startup_settings


def development_environment(**overrides: str) -> dict[str, str]:
    values = {
        "RAG_PLATFORM_PROFILE": "development",
        "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
        "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
        "RAG_PROVIDER_NAME": "fake",
        "RAG_OBSERVABILITY_API_METRIC_RETENTION_DAYS": "90",
    }
    values.update(overrides)
    return values


def production_environment(**overrides: str) -> dict[str, str]:
    values = {
        "RAG_PLATFORM_PROFILE": "production",
        "RAG_DATABASE_URL": "postgresql+psycopg://app:secret@db/rag",
        "RAG_OBJECT_STORAGE_ENDPOINT": "https://objects.example.test",
        "RAG_OBJECT_STORAGE_BUCKET": "rag-prod",
        "RAG_PROVIDER_NAME": "openai-compatible",
        "RAG_PROVIDER_API_KEY": "provider-secret",
        "RAG_OBSERVABILITY_API_METRIC_RETENTION_DAYS": "90",
        "RAG_DEBUG": "false",
        "RAG_AUTH_SECRET_KEY": "auth-secret-that-is-long-enough",
        "RAG_AUTH_ALLOWED_ORIGINS": "https://app.example.test",
        "RAG_AUTH_ADMIN_ROSTER": "admin",
    }
    values.update(overrides)
    return values


def test_development_profile_loads_with_explicit_sqlite_provider() -> None:
    settings = load_platform_settings(development_environment())

    assert settings.profile == "development"
    assert settings.database.url.startswith("sqlite")
    assert settings.provider.name == "fake"
    assert settings.observability.api_metric_retention_days == 90


def test_production_profile_requires_database_and_object_storage() -> None:
    values = production_environment()
    values.pop("RAG_DATABASE_URL")
    values.pop("RAG_OBJECT_STORAGE_BUCKET")

    with pytest.raises((ValidationError, ValueError), match="database|object_storage"):
        load_platform_settings(values)


@pytest.mark.parametrize(
    "key, message",
    [
        ("RAG_AUTH_SECRET_KEY", "auth secret"),
        ("RAG_AUTH_ALLOWED_ORIGINS", "allowed origins"),
        ("RAG_AUTH_ADMIN_ROSTER", "admin roster"),
    ],
)
def test_production_profile_requires_identity_access_security_configuration(
    key: str, message: str
) -> None:
    values = production_environment()
    values.pop(key)

    with pytest.raises((ValidationError, ValueError), match=message):
        load_platform_settings(values)


@pytest.mark.parametrize("retention", ["30", "367"])
def test_observability_retention_has_bounded_range(retention: str) -> None:
    with pytest.raises((ValidationError, ValueError), match="retention"):
        load_platform_settings(
            development_environment(
                RAG_OBSERVABILITY_API_METRIC_RETENTION_DAYS=retention,
            )
        )


def test_default_observability_retention_is_ninety_days() -> None:
    values = development_environment()
    values.pop("RAG_OBSERVABILITY_API_METRIC_RETENTION_DAYS")

    settings = load_platform_settings(values)

    assert settings.observability.api_metric_retention_days == 90


def test_configuration_includes_shared_logging_and_index_namespace() -> None:
    settings = load_platform_settings(
        development_environment(
            RAG_LOG_LEVEL="WARNING",
            RAG_INDEX_NAMESPACE="enterprise-main",
        )
    )

    assert settings.logging.level == "WARNING"
    assert settings.index.namespace == "enterprise-main"


@pytest.mark.parametrize(
    "key",
    ["TENANT_ID", "ENTERPRISE_ID", "OBSERVABILITY_RETENTION_DAYS", "TEST_POSTGRES_URL"],
)
def test_legacy_or_tenant_aliases_are_rejected(key: str) -> None:
    values = development_environment(**{f"RAG_{key}": "unexpected"})

    with pytest.raises((ValidationError, ValueError), match="unknown|tenant|legacy"):
        load_platform_settings(values)


def test_unprefixed_legacy_alias_is_rejected() -> None:
    values = development_environment()
    values["DATABASE_URL"] = values.pop("RAG_DATABASE_URL")

    with pytest.raises((ValidationError, ValueError), match="unknown|tenant|legacy"):
        load_platform_settings(values)


def test_ambient_strict_runtime_prefix_rejects_unknown_test_variables(monkeypatch) -> None:
    for key, value in development_environment().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RAG_TEST_POSTGRES_URL", "postgresql+psycopg://app:secret@db/rag")

    with pytest.raises((ValidationError, ValueError), match="unknown|legacy"):
        load_platform_settings()


def test_renamed_integration_namespace_does_not_affect_platform_settings(monkeypatch) -> None:
    for key, value in development_environment().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RAGQS_TEST_POSTGRES_URL", "postgresql+psycopg://app:secret@db/rag")

    settings = load_platform_settings()

    assert settings.database.url.startswith("sqlite")


@pytest.mark.parametrize(
    "overrides",
    [
        {"RAG_OBJECT_STORAGE_ACCESS_KEY": "access-only"},
        {"RAG_OBJECT_STORAGE_SECRET_KEY": "secret-only"},
    ],
)
def test_object_storage_credentials_must_be_configured_as_a_pair(overrides: dict[str, str]) -> None:
    with pytest.raises((ValidationError, ValueError), match="access_key|secret_key|together"):
        load_platform_settings(development_environment(**overrides))


def test_production_rejects_unsafe_flags_and_fake_provider() -> None:
    with pytest.raises((ValidationError, ValueError), match="production|debug|provider"):
        settings = load_platform_settings(
            production_environment(
                RAG_DEBUG="true",
                RAG_PROVIDER_NAME="fake",
            )
        )
        validate_startup_settings(settings)


def test_bootstrap_admin_settings_must_be_configured_as_a_complete_group() -> None:
    with pytest.raises((ValidationError, ValueError), match="bootstrap.*together"):
        load_platform_settings(
            development_environment(
                RAG_AUTH_BOOTSTRAP_USERNAME="admin",
            )
        )

    settings = load_platform_settings(
        development_environment(
            RAG_AUTH_BOOTSTRAP_USERNAME="admin",
            RAG_AUTH_BOOTSTRAP_PASSWORD="Password1",
            RAG_AUTH_BOOTSTRAP_REAL_NAME="Initial Admin",
            RAG_AUTH_BOOTSTRAP_DISPLAY_NAME="Admin",
        )
    )

    assert settings.auth.bootstrap_username == "admin"
    assert settings.auth.bootstrap_password is not None
