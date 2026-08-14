from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

Profile = Literal["development", "production"]


class PlatformConfigurationError(ValueError):
    """Stable public error for invalid structured platform configuration."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class DatabaseSettings(_StrictModel):
    url: str
    pool_size: int = Field(default=5, ge=1, le=100)
    connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        if "://" not in value or not value.split("://", 1)[0]:
            raise ValueError("database.url must be a supported URL")
        return value


class ObjectStorageSettings(_StrictModel):
    endpoint: str
    bucket: str = Field(min_length=1, max_length=128)
    access_key: str | None = None
    secret_key: SecretStr | None = None

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("object_storage.endpoint must be an HTTP URL")
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_credentials(self) -> ObjectStorageSettings:
        if (self.access_key is None) != (self.secret_key is None):
            raise ValueError("object_storage access_key and secret_key must be configured together")
        return self


class ProviderSettings(_StrictModel):
    name: str = Field(min_length=1, max_length=64)
    api_key: SecretStr | None = None
    base_url: str | None = None


class WorkerSettings(_StrictModel):
    concurrency: int = Field(default=1, ge=1, le=128)
    lease_seconds: int = Field(default=60, ge=5, le=3600)


class LoggingSettings(_StrictModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class IndexSettings(_StrictModel):
    namespace: str = Field(
        default="default", min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_-]*$"
    )
    sparse_provider: Literal["meilisearch", "opensearch", "opensearch+ik"] = "meilisearch"
    reranker_provider: str = Field(default="configured", min_length=1, max_length=64)
    image_vlm_provider: str = Field(default="configured", min_length=1, max_length=64)
    image_vlm_credential_ref: str = Field(default="image-vlm")
    generation_rollback_days: int = Field(default=7, ge=1, le=365)


class EvaluationSettings(_StrictModel):
    judge_provider: str = Field(default="bailian", min_length=1, max_length=64)
    judge_model: str = Field(default="qwen3.7-plus", min_length=1, max_length=128)
    judge_mode: str = Field(default="non_thinking", min_length=1, max_length=64)
    judge_credential_ref: str = Field(default="judge", min_length=1, max_length=64)
    judge_base_url: str | None = None
    judge_api_key: SecretStr | None = None
    candidate_configs: tuple[str, ...] = ("default",)


class ObservabilitySettings(_StrictModel):
    api_metric_retention_days: int = Field(default=90, ge=31, le=366)
    success_sample_rate: float = Field(default=0.1, ge=0, le=1)
    max_route_templates: int = Field(default=100, ge=1, le=1000)


class OutboxSettings(_StrictModel):
    notification_retention_days: int = Field(default=90, gt=0)
    outbox_delivered_retention_days: int = Field(default=30, gt=0)


class AuthSettings(_StrictModel):
    access_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    refresh_ttl_seconds: int = Field(default=604800, ge=3600, le=2592000)
    refresh_reuse_grace_seconds: int = Field(default=5, ge=1, le=60)
    login_max_attempts: int = Field(default=5, ge=1, le=20)
    login_lock_seconds: int = Field(default=60, ge=1, le=3600)
    secret_key: SecretStr | None = None
    allowed_origins: tuple[str, ...] = ()
    admin_roster: tuple[str, ...] = ()
    bootstrap_username: str | None = None
    bootstrap_password: SecretStr | None = None
    bootstrap_real_name: str | None = None
    bootstrap_display_name: str | None = None

    @model_validator(mode="after")
    def validate_bootstrap_settings(self) -> AuthSettings:
        values = (
            self.bootstrap_username,
            self.bootstrap_password,
            self.bootstrap_real_name,
            self.bootstrap_display_name,
        )
        if any(value is not None for value in values) and not all(
            value is not None for value in values
        ):
            raise ValueError("auth bootstrap settings must be configured together")
        return self

    @property
    def refresh_ttl(self) -> timedelta:
        return timedelta(seconds=self.refresh_ttl_seconds)


class PlatformSettings(BaseSettings):
    """Validated, versioned configuration for API and worker processes."""

    model_config = SettingsConfigDict(
        extra="forbid",
        env_prefix="RAG_",
        case_sensitive=True,
        env_file=None,
        hide_input_in_errors=True,
    )

    profile: Profile = "development"
    database: DatabaseSettings
    object_storage: ObjectStorageSettings
    provider: ProviderSettings
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    index: IndexSettings = Field(default_factory=IndexSettings)
    evaluation: EvaluationSettings = Field(default_factory=EvaluationSettings)
    business_timezone: str | None = None
    # 受保护维护 CLI（ragqs-usage-maintenance）的显式密钥：只从环境读取，不进参数/
    # 日志/输出；production 下缺失时维护入口拒绝执行（fail-closed）。
    maintenance_key: SecretStr | None = None
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    outbox: OutboxSettings = Field(default_factory=OutboxSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    debug: bool = False

    @field_validator("maintenance_key", mode="before")
    @classmethod
    def normalize_maintenance_key(cls, value: object) -> object:
        if value is None:
            return None
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if isinstance(raw, str) and not raw.strip():
            return None
        return value


_ENV_KEYS = {
    "RAG_PLATFORM_PROFILE",
    "RAG_DATABASE_URL",
    "RAG_DATABASE_POOL_SIZE",
    "RAG_DATABASE_CONNECT_TIMEOUT_SECONDS",
    "RAG_OBJECT_STORAGE_ENDPOINT",
    "RAG_OBJECT_STORAGE_BUCKET",
    "RAG_OBJECT_STORAGE_ACCESS_KEY",
    "RAG_OBJECT_STORAGE_SECRET_KEY",
    "RAG_PROVIDER_NAME",
    "RAG_PROVIDER_API_KEY",
    "RAG_PROVIDER_BASE_URL",
    "RAG_WORKER_CONCURRENCY",
    "RAG_WORKER_LEASE_SECONDS",
    "RAG_LOG_LEVEL",
    "RAG_INDEX_NAMESPACE",
    "RAG_INDEX_SPARSE_PROVIDER",
    "RAG_INDEX_RERANKER_PROVIDER",
    "RAG_INDEX_IMAGE_VLM_PROVIDER",
    "RAG_INDEX_IMAGE_VLM_CREDENTIAL_REF",
    "RAG_INDEX_GENERATION_ROLLBACK_DAYS",
    "RAG_EVALUATION_JUDGE_CREDENTIAL_REF",
    "RAG_EVALUATION_JUDGE_BASE_URL",
    "RAG_EVALUATION_JUDGE_API_KEY",
    "RAG_EVALUATION_CANDIDATE_CONFIGS",
    "RAG_EVALUATION_JUDGE_PROVIDER",
    "RAG_EVALUATION_JUDGE_MODEL",
    "RAG_EVALUATION_JUDGE_MODE",
    "RAG_BUSINESS_TIMEZONE",
    "RAG_MAINTENANCE_KEY",
    "RAG_OBSERVABILITY_API_METRIC_RETENTION_DAYS",
    "RAG_OBSERVABILITY_SUCCESS_SAMPLE_RATE",
    "RAG_OBSERVABILITY_MAX_ROUTE_TEMPLATES",
    "RAG_OUTBOX_NOTIFICATION_RETENTION_DAYS",
    "RAG_OUTBOX_DELIVERED_RETENTION_DAYS",
    "RAG_AUTH_ACCESS_TTL_SECONDS",
    "RAG_AUTH_REFRESH_TTL_SECONDS",
    "RAG_AUTH_REFRESH_REUSE_GRACE_SECONDS",
    "RAG_AUTH_LOGIN_MAX_ATTEMPTS",
    "RAG_AUTH_LOGIN_LOCK_SECONDS",
    "RAG_AUTH_SECRET_KEY",
    "RAG_AUTH_ALLOWED_ORIGINS",
    "RAG_AUTH_ADMIN_ROSTER",
    "RAG_AUTH_BOOTSTRAP_USERNAME",
    "RAG_AUTH_BOOTSTRAP_PASSWORD",
    "RAG_AUTH_BOOTSTRAP_REAL_NAME",
    "RAG_AUTH_BOOTSTRAP_DISPLAY_NAME",
    "RAG_DEBUG",
}
_INDEXING_ENV_KEYS = {
    "SPARSE_INDEX_PROVIDER",
    "RERANKER_PROVIDER",
    "IMAGE_VLM_PROVIDER",
    "INDEX_GENERATION_ROLLBACK_DAYS",
}
_LEGACY_OR_FORBIDDEN_KEYS = {
    "DATABASE_URL",
    "TENANT_ID",
    "ENTERPRISE_ID",
    "RAG_TENANT_ID",
    "RAG_ENTERPRISE_ID",
}


def _parse_bool(value: str, key: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean for {key}")


def _optional(env: Mapping[str, str], key: str) -> str | None:
    value = env.get(key)
    return value if value not in (None, "") else None


def _optional_secret(env: Mapping[str, str], key: str) -> str | None:
    value = _optional(env, key)
    return value if value is not None and value.strip() else None


def _int(env: Mapping[str, str], key: str) -> int | None:
    value = _optional(env, key)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"invalid integer for {key}") from None


def _float(env: Mapping[str, str], key: str) -> float | None:
    value = _optional(env, key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"invalid number for {key}") from None


def _csv(env: Mapping[str, str], key: str) -> tuple[str, ...]:
    value = _optional(env, key)
    if value is None:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def load_platform_settings(
    environ: Mapping[str, str] | None = None,
) -> PlatformSettings:
    env = dict(os.environ if environ is None else environ)
    relevant = {
        key: value
        for key, value in env.items()
        if key.startswith("RAG_") or key in _LEGACY_OR_FORBIDDEN_KEYS or key in _INDEXING_ENV_KEYS
    }
    unknown = sorted(key for key in relevant if key not in _ENV_KEYS)
    if unknown:
        raise ValueError(f"unknown or legacy configuration keys: {', '.join(unknown)}")

    database = {
        "url": _optional(env, "RAG_DATABASE_URL"),
        "pool_size": _int(env, "RAG_DATABASE_POOL_SIZE"),
        "connect_timeout_seconds": _float(env, "RAG_DATABASE_CONNECT_TIMEOUT_SECONDS"),
    }
    object_storage = {
        "endpoint": _optional(env, "RAG_OBJECT_STORAGE_ENDPOINT"),
        "bucket": _optional(env, "RAG_OBJECT_STORAGE_BUCKET"),
        "access_key": _optional(env, "RAG_OBJECT_STORAGE_ACCESS_KEY"),
        "secret_key": _optional(env, "RAG_OBJECT_STORAGE_SECRET_KEY"),
    }
    provider = {
        "name": _optional(env, "RAG_PROVIDER_NAME"),
        "api_key": _optional(env, "RAG_PROVIDER_API_KEY"),
        "base_url": _optional(env, "RAG_PROVIDER_BASE_URL"),
    }
    data = {
        "profile": env.get("RAG_PLATFORM_PROFILE", "development"),
        "database": {key: value for key, value in database.items() if value is not None},
        "object_storage": {
            key: value for key, value in object_storage.items() if value is not None
        },
        "provider": {key: value for key, value in provider.items() if value is not None},
        "worker": {
            key: value
            for key, value in {
                "concurrency": _int(env, "RAG_WORKER_CONCURRENCY"),
                "lease_seconds": _int(env, "RAG_WORKER_LEASE_SECONDS"),
            }.items()
            if value is not None
        },
        "logging": {
            "level": _optional(env, "RAG_LOG_LEVEL") or "INFO",
        },
        "index": {
            key: value
            for key, value in {
                "namespace": _optional(env, "RAG_INDEX_NAMESPACE") or "default",
                "sparse_provider": _optional(env, "RAG_INDEX_SPARSE_PROVIDER")
                or _optional(env, "SPARSE_INDEX_PROVIDER")
                or "meilisearch",
                "reranker_provider": _optional(env, "RAG_INDEX_RERANKER_PROVIDER")
                or _optional(env, "RERANKER_PROVIDER")
                or "configured",
                "image_vlm_provider": _optional(env, "RAG_INDEX_IMAGE_VLM_PROVIDER")
                or _optional(env, "IMAGE_VLM_PROVIDER")
                or "configured",
                "image_vlm_credential_ref": _optional(env, "RAG_INDEX_IMAGE_VLM_CREDENTIAL_REF")
                or "image-vlm",
                "generation_rollback_days": _int(env, "RAG_INDEX_GENERATION_ROLLBACK_DAYS")
                or _int(env, "INDEX_GENERATION_ROLLBACK_DAYS")
                or 7,
            }.items()
            if value is not None
        },
        "evaluation": {
            key: value
            for key, value in {
                "judge_provider": _optional(env, "RAG_EVALUATION_JUDGE_PROVIDER") or "bailian",
                "judge_model": _optional(env, "RAG_EVALUATION_JUDGE_MODEL") or "qwen3.7-plus",
                "judge_mode": _optional(env, "RAG_EVALUATION_JUDGE_MODE") or "non_thinking",
                "judge_credential_ref": _optional(env, "RAG_EVALUATION_JUDGE_CREDENTIAL_REF")
                or "judge",
                "judge_base_url": _optional(env, "RAG_EVALUATION_JUDGE_BASE_URL"),
                "judge_api_key": _optional_secret(env, "RAG_EVALUATION_JUDGE_API_KEY"),
                "candidate_configs": _csv(env, "RAG_EVALUATION_CANDIDATE_CONFIGS") or ("default",),
            }.items()
            if value not in (None, ())
        },
        "business_timezone": _optional(env, "RAG_BUSINESS_TIMEZONE"),
        "maintenance_key": _optional_secret(env, "RAG_MAINTENANCE_KEY"),
        "observability": {
            key: value
            for key, value in {
                "api_metric_retention_days": _int(
                    env, "RAG_OBSERVABILITY_API_METRIC_RETENTION_DAYS"
                ),
                "success_sample_rate": _float(env, "RAG_OBSERVABILITY_SUCCESS_SAMPLE_RATE"),
                "max_route_templates": _int(env, "RAG_OBSERVABILITY_MAX_ROUTE_TEMPLATES"),
            }.items()
            if value is not None
        },
        "outbox": {
            key: value
            for key, value in {
                "notification_retention_days": _int(env, "RAG_OUTBOX_NOTIFICATION_RETENTION_DAYS"),
                "outbox_delivered_retention_days": _int(env, "RAG_OUTBOX_DELIVERED_RETENTION_DAYS"),
            }.items()
            if value is not None
        },
        "auth": {
            key: value
            for key, value in {
                "access_ttl_seconds": _int(env, "RAG_AUTH_ACCESS_TTL_SECONDS"),
                "refresh_ttl_seconds": _int(env, "RAG_AUTH_REFRESH_TTL_SECONDS"),
                "refresh_reuse_grace_seconds": _int(env, "RAG_AUTH_REFRESH_REUSE_GRACE_SECONDS"),
                "login_max_attempts": _int(env, "RAG_AUTH_LOGIN_MAX_ATTEMPTS"),
                "login_lock_seconds": _int(env, "RAG_AUTH_LOGIN_LOCK_SECONDS"),
                "secret_key": _optional(env, "RAG_AUTH_SECRET_KEY"),
                "allowed_origins": _csv(env, "RAG_AUTH_ALLOWED_ORIGINS"),
                "admin_roster": _csv(env, "RAG_AUTH_ADMIN_ROSTER"),
                "bootstrap_username": _optional(env, "RAG_AUTH_BOOTSTRAP_USERNAME"),
                "bootstrap_password": _optional(env, "RAG_AUTH_BOOTSTRAP_PASSWORD"),
                "bootstrap_real_name": _optional(env, "RAG_AUTH_BOOTSTRAP_REAL_NAME"),
                "bootstrap_display_name": _optional(env, "RAG_AUTH_BOOTSTRAP_DISPLAY_NAME"),
            }.items()
            if value not in (None, ())
        },
        "debug": _parse_bool(env["RAG_DEBUG"], "RAG_DEBUG") if "RAG_DEBUG" in env else False,
    }
    settings: PlatformSettings | None = None
    try:
        settings = PlatformSettings.model_validate(data)
    except ValidationError:
        # Do not retain or chain Pydantic's structured error: errors()/json() include inputs.
        pass
    if settings is None:
        # Raised outside the except suite so __context__ cannot expose the ValidationError.
        raise PlatformConfigurationError("platform configuration is invalid") from None
    validate_startup_settings(settings)
    return settings


def validate_startup_settings(settings: PlatformSettings) -> None:
    timezone = settings.business_timezone
    if timezone is None:
        if settings.profile == "production":
            raise ValueError(
                "production requires an explicit business timezone (RAG_BUSINESS_TIMEZONE)"
            )
        timezone = "UTC"
    try:
        ZoneInfo(timezone)
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        raise ValueError("business timezone is not a valid IANA timezone") from None

    if settings.profile == "production":
        if not settings.database.url.startswith(("postgresql://", "postgresql+")):
            raise ValueError("production requires a PostgreSQL database")
        if not settings.object_storage.endpoint.startswith("https://"):
            raise ValueError("production requires an HTTPS object_storage endpoint")
        if settings.provider.name.lower() in {"fake", "memory", "mock"}:
            raise ValueError("production provider cannot be fake or memory")
        if settings.index.reranker_provider.casefold() == "none":
            raise ValueError("production reranker provider cannot be none")
        if settings.index.image_vlm_provider.casefold() == "none":
            raise ValueError("production image VLM provider cannot be none")
        if settings.provider.api_key is None or not settings.provider.api_key.get_secret_value():
            raise ValueError("production provider api key is required")
        if settings.debug:
            raise ValueError("production debug must be disabled")
        if settings.auth.secret_key is None or not settings.auth.secret_key.get_secret_value():
            raise ValueError("production auth secret key is required")
        if not settings.auth.allowed_origins:
            raise ValueError("production auth allowed origins are required")
        if not settings.auth.admin_roster:
            raise ValueError("production auth admin roster is required")

    evaluation = settings.evaluation
    if evaluation.judge_provider != "bailian":
        raise ValueError("evaluation judge provider must be bailian")
    if evaluation.judge_model != "qwen3.7-plus":
        raise ValueError("evaluation judge model must be qwen3.7-plus")
    if evaluation.judge_mode != "non_thinking":
        raise ValueError("evaluation judge mode must be non_thinking")
