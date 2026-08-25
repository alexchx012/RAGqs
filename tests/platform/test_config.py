from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from app.platform.config import (
    _ENV_KEYS,
    PlatformConfigurationError,
    load_platform_settings,
    validate_startup_settings,
)


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
        "RAG_BUSINESS_TIMEZONE": "UTC",
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

    with pytest.raises(PlatformConfigurationError, match="^platform configuration is invalid$"):
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

    with pytest.raises(ValueError, match=message):
        load_platform_settings(values)


def test_judge_and_image_vlm_credential_reference_labels_may_match() -> None:
    settings = load_platform_settings(
        development_environment(
            RAG_EVALUATION_JUDGE_CREDENTIAL_REF="shared-provider-label",
            RAG_INDEX_IMAGE_VLM_CREDENTIAL_REF="shared-provider-label",
        )
    )

    validate_startup_settings(settings)


@pytest.mark.parametrize("retention", ["30", "367"])
def test_observability_retention_has_bounded_range(retention: str) -> None:
    with pytest.raises(PlatformConfigurationError, match="^platform configuration is invalid$"):
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


def test_index_backend_keys_load_from_environment() -> None:
    settings = load_platform_settings(
        development_environment(
            RAG_INDEX_EMBEDDING_PROVIDER="openai-compatible",
            RAG_INDEX_EMBEDDING_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1",
            RAG_INDEX_EMBEDDING_API_KEY="emb-secret",
            RAG_INDEX_EMBEDDING_MODEL="text-embedding-v4",
            RAG_INDEX_EMBEDDING_REVISION="text-embedding-v4",
            RAG_INDEX_EMBEDDING_DIMENSION="1024",
            RAG_INDEX_EMBEDDING_METRIC="cosine",
            RAG_INDEX_VECTOR_PROVIDER="milvus",
            RAG_INDEX_VECTOR_URI="http://127.0.0.1:9091",
            RAG_INDEX_VECTOR_COLLECTION_PREFIX="ragqs",
            RAG_INDEX_SPARSE_URL="http://127.0.0.1:7700",
            RAG_INDEX_SPARSE_API_KEY="ragqs-dev-meili-key",
            RAG_INDEX_SPARSE_PROVIDER="opensearch+ik",
            RAG_INDEX_SPARSE_USERNAME="admin",
            RAG_INDEX_SPARSE_PASSWORD="opensearch-secret",
            RAG_INDEX_SPARSE_CA_PATH="./volumes/opensearch/ca.crt",
            RAG_INDEX_SPARSE_JVM_HEAP_MIN_GB="2",
            RAG_INDEX_SPARSE_INDEX="ragqs_chunks",
            RAG_INDEX_SPARSE_DATA_PATH="./volumes/meilisearch",
        )
    )

    assert settings.index.embedding_provider == "openai-compatible"
    assert settings.index.embedding_dimension == 1024
    assert settings.index.vector_provider == "milvus"
    assert settings.index.vector_uri == "http://127.0.0.1:9091"
    assert settings.index.sparse_provider == "opensearch+ik"
    assert settings.index.sparse_url == "http://127.0.0.1:7700"
    assert settings.index.sparse_username == "admin"
    assert settings.index.sparse_jvm_heap_min_gb == 2
    assert settings.index.sparse_data_path == "./volumes/meilisearch"


def test_opensearch_jvm_heap_baseline_must_be_positive() -> None:
    with pytest.raises(PlatformConfigurationError, match="^platform configuration is invalid$"):
        load_platform_settings(
            development_environment(
                RAG_INDEX_SPARSE_PROVIDER="opensearch+ik",
                RAG_INDEX_SPARSE_JVM_HEAP_MIN_GB="0",
            )
        )


def test_documents_and_indexing_processing_limits_are_configurable() -> None:
    settings = load_platform_settings(
        development_environment(
            RAG_DOCUMENTS_UPLOAD_MAX_BYTES="9",
            RAG_DOCUMENTS_CLEANUP_MAX_ATTEMPTS="2",
            RAG_INDEX_TEXT_CHUNK_MAX_CHARS="7",
            RAG_INDEX_XLSX_MERGED_CELLS_MAX="11",
        )
    )

    assert settings.documents.upload_max_bytes == 9
    assert settings.documents.cleanup_max_attempts == 2
    assert settings.index.text_chunk_max_chars == 7
    assert settings.index.xlsx_merged_cells_max == 11


@pytest.mark.parametrize(
    "key",
    ["TENANT_ID", "ENTERPRISE_ID", "OBSERVABILITY_RETENTION_DAYS", "TEST_POSTGRES_URL"],
)
def test_legacy_or_tenant_aliases_are_rejected(key: str) -> None:
    values = development_environment(**{f"RAG_{key}": "unexpected"})

    with pytest.raises(ValueError, match="unknown|tenant|legacy"):
        load_platform_settings(values)


def test_unprefixed_legacy_alias_is_rejected() -> None:
    values = development_environment()
    values["DATABASE_URL"] = values.pop("RAG_DATABASE_URL")

    with pytest.raises(ValueError, match="unknown|tenant|legacy"):
        load_platform_settings(values)


@pytest.mark.parametrize(
    "key",
    [
        "SPARSE_INDEX_PROVIDER",
        "RERANKER_PROVIDER",
        "IMAGE_VLM_PROVIDER",
        "INDEX_GENERATION_ROLLBACK_DAYS",
    ],
)
def test_legacy_indexing_aliases_are_rejected(key: str) -> None:
    with pytest.raises(ValueError, match="unknown|legacy"):
        load_platform_settings(development_environment(**{key: "unexpected"}))


def test_legacy_indexing_keys_have_no_fallback_parsers() -> None:
    parser_source = inspect.getsource(load_platform_settings)

    for legacy_parser in (
        '_optional(env, "SPARSE_INDEX_PROVIDER")',
        '_optional(env, "RERANKER_PROVIDER")',
        '_optional(env, "IMAGE_VLM_PROVIDER")',
        '_int(env, "INDEX_GENERATION_ROLLBACK_DAYS")',
    ):
        assert legacy_parser not in parser_source


def test_env_example_documents_every_supported_environment_key() -> None:
    example = Path(".env.example").read_text(encoding="utf-8")
    missing = sorted(key for key in _ENV_KEYS if key not in example)

    assert missing == []


def test_ambient_strict_runtime_prefix_rejects_unknown_test_variables(monkeypatch) -> None:
    for key, value in development_environment().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RAG_TEST_POSTGRES_URL", "postgresql+psycopg://app:secret@db/rag")

    with pytest.raises(ValueError, match="unknown|legacy"):
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
    with pytest.raises(PlatformConfigurationError, match="^platform configuration is invalid$"):
        load_platform_settings(development_environment(**overrides))


def test_production_rejects_unsafe_flags_and_fake_provider() -> None:
    with pytest.raises(ValueError, match="production|debug|provider"):
        settings = load_platform_settings(
            production_environment(
                RAG_DEBUG="true",
                RAG_PROVIDER_NAME="fake",
            )
        )
        validate_startup_settings(settings)


def test_bootstrap_admin_settings_must_be_configured_as_a_complete_group() -> None:
    with pytest.raises(PlatformConfigurationError, match="^platform configuration is invalid$"):
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


def test_business_timezone_defaults_to_utc_in_development() -> None:
    settings = load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
        }
    )
    assert settings.business_timezone is None  # 未配置
    configured = load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
            "RAG_BUSINESS_TIMEZONE": "Asia/Shanghai",
        }
    )
    assert configured.business_timezone == "Asia/Shanghai"


def test_production_requires_explicit_business_timezone() -> None:
    import pytest as _pytest

    with _pytest.raises(ValueError, match="business timezone"):
        load_platform_settings(
            {
                "RAG_PLATFORM_PROFILE": "production",
                "RAG_DATABASE_URL": "postgresql+psycopg://u:p@localhost/db",
                "RAG_OBJECT_STORAGE_ENDPOINT": "https://s3.example.com",
                "RAG_OBJECT_STORAGE_BUCKET": "rag",
                "RAG_PROVIDER_NAME": "dashscope",
                "RAG_PROVIDER_API_KEY": "secret",
                "RAG_AUTH_SECRET_KEY": "secret-key-long-enough",
                "RAG_AUTH_ALLOWED_ORIGINS": "https://app.example.com",
                "RAG_AUTH_ADMIN_ROSTER": "root",
            }
        )


def test_production_explicit_utc_is_allowed_and_invalid_tz_rejected(tmp_path) -> None:
    import pytest as _pytest

    archive_dir = tmp_path / "user-deletion-archive"

    settings = load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "production",
            "RAG_DATABASE_URL": "postgresql+psycopg://u:p@localhost/db",
            "RAG_OBJECT_STORAGE_ENDPOINT": "https://s3.example.com",
            "RAG_OBJECT_STORAGE_BUCKET": "rag",
            "RAG_PROVIDER_NAME": "dashscope",
            "RAG_PROVIDER_API_KEY": "secret",
            "RAG_AUTH_SECRET_KEY": "secret-key-long-enough",
            "RAG_AUTH_ALLOWED_ORIGINS": "https://app.example.com",
            "RAG_AUTH_ADMIN_ROSTER": "root",
            "RAG_BUSINESS_TIMEZONE": "UTC",
            "USER_DELETION_ARCHIVE_DIR": str(archive_dir),
        }
    )
    assert settings.business_timezone == "UTC"
    with _pytest.raises(ValueError):
        load_platform_settings(
            {
                "RAG_PLATFORM_PROFILE": "production",
                "RAG_DATABASE_URL": "postgresql+psycopg://u:p@localhost/db",
                "RAG_OBJECT_STORAGE_ENDPOINT": "https://s3.example.com",
                "RAG_OBJECT_STORAGE_BUCKET": "rag",
                "RAG_PROVIDER_NAME": "dashscope",
                "RAG_PROVIDER_API_KEY": "secret",
                "RAG_AUTH_SECRET_KEY": "secret-key-long-enough",
                "RAG_AUTH_ALLOWED_ORIGINS": "https://app.example.com",
                "RAG_AUTH_ADMIN_ROSTER": "root",
                "RAG_BUSINESS_TIMEZONE": "Not/AZone",
                "USER_DELETION_ARCHIVE_DIR": str(archive_dir),
            }
        )


@pytest.mark.parametrize("maintenance_key", [None, "", " \t "])
def test_blank_maintenance_key_is_normalized_to_absent(maintenance_key: str | None) -> None:
    values = development_environment()
    if maintenance_key is not None:
        values["RAG_MAINTENANCE_KEY"] = maintenance_key

    settings = load_platform_settings(values)

    assert settings.maintenance_key is None


@pytest.mark.parametrize(
    ("key", "expected_message"),
    [
        ("RAG_DATABASE_POOL_SIZE", "invalid integer for RAG_DATABASE_POOL_SIZE"),
        (
            "RAG_DATABASE_CONNECT_TIMEOUT_SECONDS",
            "invalid number for RAG_DATABASE_CONNECT_TIMEOUT_SECONDS",
        ),
    ],
)
def test_numeric_configuration_errors_are_stable_and_hide_values(
    key: str, expected_message: str
) -> None:
    sentinel = "sentinel-numeric-credential-do-not-leak"

    with pytest.raises(ValueError) as exc_info:
        load_platform_settings(development_environment(**{key: sentinel}))

    assert str(exc_info.value) == expected_message
    assert sentinel not in str(exc_info.value)


def test_invalid_timezone_error_is_stable_and_hides_value() -> None:
    sentinel = "Sentinel/Timezone-Credential-Do-Not-Leak"

    with pytest.raises(ValueError) as exc_info:
        load_platform_settings(
            development_environment(
                RAG_BUSINESS_TIMEZONE=sentinel,
            )
        )

    assert str(exc_info.value) == "business timezone is not a valid IANA timezone"
    assert sentinel not in str(exc_info.value)


def test_load_platform_settings_replaces_pydantic_errors_with_safe_boundary() -> None:
    sentinel = "sentinel-access-key-do-not-leak"

    with pytest.raises(PlatformConfigurationError) as exc_info:
        load_platform_settings(
            development_environment(
                RAG_OBJECT_STORAGE_ACCESS_KEY=sentinel,
            )
        )

    error = exc_info.value
    assert str(error) == "platform configuration is invalid"
    assert error.args == ("platform configuration is invalid",)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.__suppress_context__ is True
    assert vars(error) == {}
    assert not hasattr(error, "errors")
    assert not hasattr(error, "json")
    visible_representations = (
        str(error),
        repr(error),
        repr(error.args),
        repr(vars(error)),
    )
    assert all(sentinel not in representation for representation in visible_representations)


def test_backup_settings_defaults_and_env_overrides() -> None:
    settings = load_platform_settings(development_environment())
    assert settings.backup.schedule_interval_seconds == 60
    assert settings.backup.gate_settle_seconds == 2.0
    assert settings.backup.gate_drain_timeout_seconds == 30.0
    assert settings.backup.retention_batch_limit == 50

    overridden = load_platform_settings(
        development_environment(
            RAG_BACKUP_SCHEDULE_INTERVAL_SECONDS="120",
            RAG_BACKUP_GATE_SETTLE_SECONDS="0.5",
            RAG_BACKUP_GATE_DRAIN_TIMEOUT_SECONDS="10",
            RAG_BACKUP_RETENTION_BATCH_LIMIT="5",
        )
    )
    assert overridden.backup.schedule_interval_seconds == 120
    assert overridden.backup.gate_settle_seconds == 0.5
    assert overridden.backup.gate_drain_timeout_seconds == 10.0
    assert overridden.backup.retention_batch_limit == 5


def test_backup_settings_reject_out_of_range_values() -> None:
    with pytest.raises(PlatformConfigurationError, match="^platform configuration is invalid$"):
        load_platform_settings(development_environment(RAG_BACKUP_RETENTION_BATCH_LIMIT="0"))
    with pytest.raises(PlatformConfigurationError, match="^platform configuration is invalid$"):
        load_platform_settings(development_environment(RAG_BACKUP_SCHEDULE_INTERVAL_SECONDS="1"))


def test_effort_rag_call_limits_default_and_env_overrides() -> None:
    settings = load_platform_settings(development_environment())
    assert settings.chat.effort_rag_call_limits == {"quick": 1, "think": 8, "deep": 10}

    overridden = load_platform_settings(
        development_environment(
            RAG_EFFORT_RAG_CALL_LIMIT_QUICK="2",
            RAG_EFFORT_RAG_CALL_LIMIT_THINK="6",
            RAG_EFFORT_RAG_CALL_LIMIT_DEEP="12",
        )
    )
    assert overridden.chat.effort_rag_call_limits == {"quick": 2, "think": 6, "deep": 12}


@pytest.mark.parametrize(
    "value",
    ["0", "-1"],
)
def test_effort_rag_call_limits_reject_non_positive_values(value: str) -> None:
    with pytest.raises(PlatformConfigurationError, match="^platform configuration is invalid$"):
        load_platform_settings(development_environment(RAG_EFFORT_RAG_CALL_LIMIT_THINK=value))


@pytest.mark.parametrize(
    "value",
    ["1.5", "abc"],
)
def test_effort_rag_call_limits_reject_non_integer_values(value: str) -> None:
    with pytest.raises(ValueError, match="invalid integer for RAG_EFFORT_RAG_CALL_LIMIT_THINK"):
        load_platform_settings(development_environment(RAG_EFFORT_RAG_CALL_LIMIT_THINK=value))


def test_effort_rag_call_limits_reject_unknown_effort_keys() -> None:
    with pytest.raises(ValueError, match="unknown|legacy"):
        load_platform_settings(development_environment(RAG_EFFORT_RAG_CALL_LIMIT_FAST="2"))
