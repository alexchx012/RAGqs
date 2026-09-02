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
    lease_seconds: int = Field(default=60, ge=5, le=3600)
    ingestion_lease_seconds: int = Field(default=300, ge=5, le=3600)
    ingestion_heartbeat_seconds: int = Field(default=20, ge=1, le=300)
    ingestion_poll_interval_seconds: int = Field(default=5, ge=1, le=3600)

    @model_validator(mode="after")
    def validate_ingestion_heartbeat(self) -> WorkerSettings:
        if self.ingestion_heartbeat_seconds >= self.ingestion_lease_seconds:
            raise ValueError("ingestion heartbeat must be shorter than the ingestion lease")
        return self


class BackupSettings(_StrictModel):
    """Resident backup maintenance worker tuning (schedule poll, write-gate
    drain protocol and retention sweep batch size) plus the backup target in
    object storage."""

    schedule_interval_seconds: int = Field(default=60, ge=5, le=3600)
    gate_settle_seconds: float = Field(default=2.0, ge=0.0, le=300.0)
    gate_drain_timeout_seconds: float = Field(default=30.0, ge=1.0, le=900.0)
    retention_batch_limit: int = Field(default=50, ge=1, le=1000)
    # 备份目标：对象存储 bucket 内的备份命名空间（必填段）与可选子前缀。
    # 备份/恢复产物统一落在该键前缀下，不与业务对象混放；生产未配置命名空间
    # 时启动拒绝（fail-closed，见 validate_startup_settings）。
    target_namespace: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9_-]*$"
    )
    target_prefix: str | None = Field(
        default=None, min_length=1, max_length=256, pattern=r"^[a-z0-9][a-z0-9_/-]*$"
    )

    @property
    def target_key_prefix(self) -> str | None:
        """Effective object key prefix of the backup target, or None when no
        backup target is configured (dev/test keep the Noop defaults)."""

        if self.target_namespace is None:
            return None
        namespace = self.target_namespace.strip("/")
        prefix = (self.target_prefix or "").strip("/")
        if prefix:
            return f"{namespace}/{prefix}"
        return namespace


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
    image_vlm_base_url: str | None = None
    image_vlm_api_key: SecretStr | None = None
    image_vlm_model: str = Field(default="qwen-vl-plus", min_length=1, max_length=128)
    image_vlm_revision: str = Field(default="", max_length=128)
    image_vlm_timeout_seconds: int = Field(default=60, ge=1, le=600)
    # InternVL 部署 profile（A69）：设备分配、显存上限、并发上限与单图输入上限。
    # 设备/显存作为 profile 事实透传；并发/输入上限在 describer 实现内生效。
    image_vlm_devices: tuple[str, ...] = ()
    image_vlm_vram_gb: float | None = Field(default=None, gt=0.0, le=1024.0)
    image_vlm_concurrency: int = Field(default=4, ge=1, le=32)
    image_vlm_max_input_bytes: int | None = Field(default=None, ge=1)
    generation_rollback_days: int = Field(default=7, ge=1, le=365)
    embedding_provider: Literal["openai-compatible", "memory"] = "memory"
    embedding_base_url: str | None = None
    embedding_api_key: SecretStr | None = None
    embedding_model: str | None = None
    embedding_revision: str | None = None
    embedding_dimension: int | None = Field(default=None, ge=1, le=8192)
    embedding_metric: Literal["cosine", "l2", "ip"] = "cosine"
    vector_provider: Literal["milvus", "memory"] = "memory"
    vector_uri: str | None = None
    vector_token: SecretStr | None = None
    vector_collection_prefix: str = Field(
        default="ragqs", min_length=1, max_length=64, pattern=r"^[a-zA-Z][a-zA-Z0-9_]*$"
    )
    sparse_url: str | None = None
    sparse_api_key: SecretStr | None = None
    sparse_username: str | None = None
    sparse_password: SecretStr | None = None
    sparse_ca_path: str | None = None
    sparse_jvm_heap_min_gb: float = Field(default=1.0, gt=0.0, le=1024.0)
    sparse_index: str = Field(
        default="ragqs_chunks", min_length=1, max_length=128, pattern=r"^[a-zA-Z][a-zA-Z0-9_-]*$"
    )
    sparse_data_path: str | None = None
    text_chunk_max_chars: int = Field(default=8_000, ge=1)
    xlsx_merged_cells_max: int = Field(default=10_000, ge=1)
    ocr_confidence_threshold: float = Field(default=0.9, ge=0.0, le=1.0)
    mineru_provider: Literal["disabled", "local"] = "disabled"
    mineru_executable: str = Field(default="mineru", min_length=1, max_length=256)
    mineru_timeout_seconds: int = Field(default=900, ge=1, le=7200)
    contextual_retrieval_provider: Literal["disabled", "dashscope"] = "disabled"
    contextual_retrieval_base_url: str | None = None
    contextual_retrieval_api_key: SecretStr | None = None
    contextual_retrieval_model: Literal["ds-v4-flash"] = "ds-v4-flash"
    contextual_retrieval_revision: str = Field(default="ds-v4-flash", min_length=1, max_length=128)
    contextual_retrieval_timeout_seconds: int = Field(default=60, ge=1, le=600)
    contextual_retrieval_concurrency: int = Field(default=4, ge=1, le=32)
    contextual_retrieval_prefix_token_limit: int = Field(default=30_000, ge=1_000, le=100_000)
    contextual_prefix_cache_provider: Literal["memory", "disabled"] = "memory"
    # 第二阶段树搜索（PageIndex）：仅远程 ds-v4-flash；disabled 时不装配 tree
    # router，检索保持既有混合检索路径。
    tree_search_provider: Literal["disabled", "dashscope"] = "disabled"
    tree_search_base_url: str | None = None
    tree_search_api_key: SecretStr | None = None
    tree_search_model: Literal["ds-v4-flash"] = "ds-v4-flash"
    tree_search_timeout_seconds: int = Field(default=30, ge=1, le=600)
    # 两阶段远程 cross-encoder reranker：单端点两模型参数（vLLM /rerank）。
    # base_url 未配置时沿用既有装配（生产要求显式注入 reranker）。
    reranker_base_url: str | None = None
    reranker_api_key: SecretStr | None = None
    reranker_coarse_model: str = Field(default="qwen3-reranker-0.6b", min_length=1, max_length=128)
    reranker_final_model: str = Field(default="qwen3-reranker-8b", min_length=1, max_length=128)
    reranker_coarse_revision: str = Field(default="", max_length=128)
    reranker_final_revision: str = Field(default="", max_length=128)
    reranker_quantization: str = Field(default="int8", min_length=1, max_length=64)
    reranker_tokenizer_version: str = Field(default="unspecified", min_length=1, max_length=128)
    reranker_timeout_seconds: int = Field(default=10, ge=1, le=600)


class GraphSettings(_StrictModel):
    """公共图谱抽取 provider 装配开关。

    ``deterministic`` 保持开发/测试确定性实现；``llm`` 在配置提供端点时由
    runtime 装配远程 LLM 抽取传输实现（替换确定性实现的路由开关）。
    """

    extraction_provider: Literal["deterministic", "llm"] = "deterministic"
    extraction_base_url: str | None = None
    extraction_api_key: SecretStr | None = None
    extraction_model: str = Field(
        default="public-graph-extraction-v1", min_length=1, max_length=128
    )
    extraction_prompt_version: str = Field(default="public-graph-v1", min_length=1, max_length=128)
    extraction_timeout_seconds: int = Field(default=60, ge=1, le=600)


class ChatSettings(_StrictModel):
    """Per-effort logical RAG operation caps (deployment-level, restart to apply)."""

    effort_rag_call_limit_quick: int = Field(default=1, ge=1)
    effort_rag_call_limit_think: int = Field(default=8, ge=1)
    effort_rag_call_limit_deep: int = Field(default=10, ge=1)
    ask_rate_limit_per_minute: int = Field(default=20, ge=1)
    generation_disconnect_grace_seconds: int = Field(default=60, ge=0, le=3600)
    # 「优化输入」端点：模型名以 Literal 锁死（平台唯一支持的增强模型），密钥与
    # 地址复用全局 ProviderSettings；超时与输入上限为可部署配置。
    enhance_model: Literal["qwen3.7-plus"] = "qwen3.7-plus"
    enhance_timeout_seconds: int = Field(default=30, ge=1, le=600)
    enhance_max_prompt_chars: int = Field(default=4000, ge=1)
    # 聊天 content 入口上限：超过返回 422（与 enhance 输入上限同一部署模式）。
    max_content_chars: int = Field(default=4000, ge=1)

    @property
    def effort_rag_call_limits(self) -> dict[str, int]:
        return {
            "quick": self.effort_rag_call_limit_quick,
            "think": self.effort_rag_call_limit_think,
            "deep": self.effort_rag_call_limit_deep,
        }


class DocumentsSettings(_StrictModel):
    upload_max_bytes: int = Field(default=25 * 1024 * 1024, ge=1)
    upload_max_files_per_request: int = Field(default=20, ge=1)
    upload_max_request_bytes: int = Field(default=100 * 1024 * 1024, ge=1)
    cleanup_max_attempts: int = Field(default=3, ge=1)
    version_retention_days: int = Field(default=30, ge=1)


class EvaluationSettings(_StrictModel):
    # judge_provider/judge_model/judge_mode 是固定值（平台唯一支持的评审组合），
    # 不提供 env 覆盖；credential/base_url/api_key 才是可部署配置。
    judge_provider: Literal["bailian"] = "bailian"
    judge_model: Literal["qwen3.7-plus"] = "qwen3.7-plus"
    judge_mode: Literal["non_thinking"] = "non_thinking"
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
    # 显式 Cookie Secure 策略；None 时按 profile 推导（production=True）。
    cookie_secure: bool | None = None
    login_max_attempts: int = Field(default=5, ge=1, le=20)
    login_lock_seconds: int = Field(default=60, ge=1, le=3600)
    user_deletion_retention_days: int = Field(default=30, ge=1, le=3650)
    # 物理归档包目录；生产必须显式配置，开发缺省回退到本地数据目录。
    user_deletion_archive_dir: str | None = None
    secret_key: SecretStr | None = None
    allowed_origins: tuple[str, ...] = ()
    admin_roster: tuple[str, ...] = ()
    bootstrap_username: str | None = None
    bootstrap_password: SecretStr | None = None
    bootstrap_real_name: str | None = None
    bootstrap_display_name: str | None = None
    # Roster 条目是不可变 user_id：bootstrap 必须按清单预声明的 id 建号。
    bootstrap_user_id: str | None = None

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
    backup: BackupSettings = Field(default_factory=BackupSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    index: IndexSettings = Field(default_factory=IndexSettings)
    graph: GraphSettings = Field(default_factory=GraphSettings)
    chat: ChatSettings = Field(default_factory=ChatSettings)
    documents: DocumentsSettings = Field(default_factory=DocumentsSettings)
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
    "RAG_WORKER_LEASE_SECONDS",
    "RAG_INGESTION_LEASE_SECONDS",
    "RAG_INGESTION_HEARTBEAT_SECONDS",
    "RAG_INGESTION_POLL_INTERVAL_SECONDS",
    "RAG_BACKUP_SCHEDULE_INTERVAL_SECONDS",
    "RAG_BACKUP_GATE_SETTLE_SECONDS",
    "RAG_BACKUP_GATE_DRAIN_TIMEOUT_SECONDS",
    "RAG_BACKUP_RETENTION_BATCH_LIMIT",
    "RAG_BACKUP_TARGET_NAMESPACE",
    "RAG_BACKUP_TARGET_PREFIX",
    "RAG_LOG_LEVEL",
    "RAG_INDEX_NAMESPACE",
    "RAG_INDEX_SPARSE_PROVIDER",
    "RAG_INDEX_RERANKER_PROVIDER",
    "RAG_INDEX_IMAGE_VLM_PROVIDER",
    "RAG_INDEX_IMAGE_VLM_CREDENTIAL_REF",
    "RAG_INDEX_IMAGE_VLM_BASE_URL",
    "RAG_INDEX_IMAGE_VLM_API_KEY",
    "RAG_INDEX_IMAGE_VLM_MODEL",
    "RAG_INDEX_IMAGE_VLM_REVISION",
    "RAG_INDEX_IMAGE_VLM_TIMEOUT_SECONDS",
    "RAG_INDEX_IMAGE_VLM_DEVICES",
    "RAG_INDEX_IMAGE_VLM_VRAM_GB",
    "RAG_INDEX_IMAGE_VLM_CONCURRENCY",
    "RAG_INDEX_IMAGE_VLM_MAX_INPUT_BYTES",
    "RAG_INDEX_GENERATION_ROLLBACK_DAYS",
    "RAG_INDEX_EMBEDDING_PROVIDER",
    "RAG_INDEX_EMBEDDING_BASE_URL",
    "RAG_INDEX_EMBEDDING_API_KEY",
    "RAG_INDEX_EMBEDDING_MODEL",
    "RAG_INDEX_EMBEDDING_REVISION",
    "RAG_INDEX_EMBEDDING_DIMENSION",
    "RAG_INDEX_EMBEDDING_METRIC",
    "RAG_INDEX_VECTOR_PROVIDER",
    "RAG_INDEX_VECTOR_URI",
    "RAG_INDEX_VECTOR_TOKEN",
    "RAG_INDEX_VECTOR_COLLECTION_PREFIX",
    "RAG_INDEX_SPARSE_URL",
    "RAG_INDEX_SPARSE_API_KEY",
    "RAG_INDEX_SPARSE_USERNAME",
    "RAG_INDEX_SPARSE_PASSWORD",
    "RAG_INDEX_SPARSE_CA_PATH",
    "RAG_INDEX_SPARSE_JVM_HEAP_MIN_GB",
    "RAG_INDEX_SPARSE_INDEX",
    "RAG_INDEX_SPARSE_DATA_PATH",
    "RAG_INDEX_TEXT_CHUNK_MAX_CHARS",
    "RAG_EFFORT_RAG_CALL_LIMIT_QUICK",
    "RAG_EFFORT_RAG_CALL_LIMIT_THINK",
    "RAG_EFFORT_RAG_CALL_LIMIT_DEEP",
    "RAG_CHAT_ASK_RATE_LIMIT_PER_MINUTE",
    "CHAT_ASK_RATE_LIMIT_PER_MINUTE",
    "RAG_GENERATION_DISCONNECT_GRACE_SECONDS",
    "GENERATION_DISCONNECT_GRACE_SECONDS",
    "RAG_CHAT_ENHANCE_MODEL",
    "RAG_CHAT_ENHANCE_TIMEOUT_SECONDS",
    "RAG_CHAT_ENHANCE_MAX_PROMPT_CHARS",
    "RAG_CHAT_MAX_CONTENT_CHARS",
    "RAG_INDEX_XLSX_MERGED_CELLS_MAX",
    "RAG_INDEX_OCR_CONFIDENCE_THRESHOLD",
    "RAG_INDEX_MINERU_PROVIDER",
    "RAG_INDEX_MINERU_EXECUTABLE",
    "RAG_INDEX_MINERU_TIMEOUT_SECONDS",
    "RAG_INDEX_CONTEXTUAL_RETRIEVAL_PROVIDER",
    "RAG_INDEX_CONTEXTUAL_RETRIEVAL_BASE_URL",
    "RAG_INDEX_CONTEXTUAL_RETRIEVAL_API_KEY",
    "RAG_INDEX_CONTEXTUAL_RETRIEVAL_MODEL",
    "RAG_INDEX_CONTEXTUAL_RETRIEVAL_REVISION",
    "RAG_INDEX_CONTEXTUAL_RETRIEVAL_TIMEOUT_SECONDS",
    "RAG_INDEX_CONTEXTUAL_RETRIEVAL_CONCURRENCY",
    "RAG_INDEX_CONTEXTUAL_RETRIEVAL_PREFIX_TOKEN_LIMIT",
    "RAG_INDEX_CONTEXTUAL_PREFIX_CACHE_PROVIDER",
    "RAG_INDEX_TREE_SEARCH_PROVIDER",
    "RAG_INDEX_TREE_SEARCH_BASE_URL",
    "RAG_INDEX_TREE_SEARCH_API_KEY",
    "RAG_INDEX_TREE_SEARCH_MODEL",
    "RAG_INDEX_TREE_SEARCH_TIMEOUT_SECONDS",
    "RAG_INDEX_RERANKER_BASE_URL",
    "RAG_INDEX_RERANKER_API_KEY",
    "RAG_INDEX_RERANKER_COARSE_MODEL",
    "RAG_INDEX_RERANKER_FINAL_MODEL",
    "RAG_INDEX_RERANKER_COARSE_REVISION",
    "RAG_INDEX_RERANKER_FINAL_REVISION",
    "RAG_INDEX_RERANKER_QUANTIZATION",
    "RAG_INDEX_RERANKER_TOKENIZER_VERSION",
    "RAG_INDEX_RERANKER_TIMEOUT_SECONDS",
    "RAG_GRAPH_EXTRACTION_PROVIDER",
    "RAG_GRAPH_EXTRACTION_BASE_URL",
    "RAG_GRAPH_EXTRACTION_API_KEY",
    "RAG_GRAPH_EXTRACTION_MODEL",
    "RAG_GRAPH_EXTRACTION_PROMPT_VERSION",
    "RAG_GRAPH_EXTRACTION_TIMEOUT_SECONDS",
    "RAG_DOCUMENTS_UPLOAD_MAX_BYTES",
    "RAG_UPLOAD_MAX_FILES_PER_REQUEST",
    "RAG_UPLOAD_MAX_REQUEST_BYTES",
    "RAG_DOCUMENTS_CLEANUP_MAX_ATTEMPTS",
    "RAG_EVALUATION_JUDGE_CREDENTIAL_REF",
    "RAG_EVALUATION_JUDGE_BASE_URL",
    "RAG_EVALUATION_JUDGE_API_KEY",
    "RAG_EVALUATION_CANDIDATE_CONFIGS",
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
    "RAG_AUTH_COOKIE_SECURE",
    "RAG_AUTH_SECRET_KEY",
    "RAG_AUTH_ALLOWED_ORIGINS",
    "RAG_AUTH_ADMIN_ROSTER",
    "RAG_AUTH_BOOTSTRAP_USERNAME",
    "RAG_AUTH_BOOTSTRAP_PASSWORD",
    "RAG_AUTH_BOOTSTRAP_REAL_NAME",
    "RAG_AUTH_BOOTSTRAP_DISPLAY_NAME",
    "RAG_AUTH_BOOTSTRAP_USER_ID",
    "RAG_DEBUG",
}

_UNPREFIXED_SUPPORTED_KEYS = {
    "CHAT_ASK_RATE_LIMIT_PER_MINUTE",
    "GENERATION_DISCONNECT_GRACE_SECONDS",
}
_LEGACY_OR_FORBIDDEN_KEYS = {
    "DATABASE_URL",
    "TENANT_ID",
    "ENTERPRISE_ID",
    "RAG_TENANT_ID",
    "RAG_ENTERPRISE_ID",
    "SPARSE_INDEX_PROVIDER",
    "RERANKER_PROVIDER",
    "IMAGE_VLM_PROVIDER",
    "INDEX_GENERATION_ROLLBACK_DAYS",
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


def _optional_bool(env: Mapping[str, str], key: str) -> bool | None:
    if key not in env or env[key] == "":
        return None
    return _parse_bool(env[key], key)


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
    profile = env.get("RAG_PLATFORM_PROFILE", "development")
    global_provider_base_url = _optional(env, "RAG_PROVIDER_BASE_URL")
    global_provider_api_key = _optional(env, "RAG_PROVIDER_API_KEY")
    contextual_defaults: dict[str, str | None] = {}
    if profile == "production":
        contextual_defaults = {
            "contextual_retrieval_provider": "dashscope",
            "contextual_retrieval_base_url": (
                _optional(env, "RAG_INDEX_CONTEXTUAL_RETRIEVAL_BASE_URL")
                or global_provider_base_url
                or "https://dashscope.aliyuncs.com/compatible-mode/v1"
            ),
            "contextual_retrieval_api_key": (
                _optional(env, "RAG_INDEX_CONTEXTUAL_RETRIEVAL_API_KEY") or global_provider_api_key
            ),
        }
    relevant = {
        key: value
        for key, value in env.items()
        if (
            key.startswith("RAG_")
            or key in _LEGACY_OR_FORBIDDEN_KEYS
            or key in _UNPREFIXED_SUPPORTED_KEYS
        )
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
        "profile": profile,
        "database": {key: value for key, value in database.items() if value is not None},
        "object_storage": {
            key: value for key, value in object_storage.items() if value is not None
        },
        "provider": {key: value for key, value in provider.items() if value is not None},
        "worker": {
            key: value
            for key, value in {
                "lease_seconds": _int(env, "RAG_WORKER_LEASE_SECONDS"),
                "ingestion_lease_seconds": _int(env, "RAG_INGESTION_LEASE_SECONDS"),
                "ingestion_heartbeat_seconds": _int(env, "RAG_INGESTION_HEARTBEAT_SECONDS"),
                "ingestion_poll_interval_seconds": _int(env, "RAG_INGESTION_POLL_INTERVAL_SECONDS"),
            }.items()
            if value is not None
        },
        "backup": {
            key: value
            for key, value in {
                "schedule_interval_seconds": _int(env, "RAG_BACKUP_SCHEDULE_INTERVAL_SECONDS"),
                "gate_settle_seconds": _float(env, "RAG_BACKUP_GATE_SETTLE_SECONDS"),
                "gate_drain_timeout_seconds": _float(env, "RAG_BACKUP_GATE_DRAIN_TIMEOUT_SECONDS"),
                "retention_batch_limit": _int(env, "RAG_BACKUP_RETENTION_BATCH_LIMIT"),
                "target_namespace": _optional(env, "RAG_BACKUP_TARGET_NAMESPACE"),
                "target_prefix": _optional(env, "RAG_BACKUP_TARGET_PREFIX"),
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
                "sparse_provider": _optional(env, "RAG_INDEX_SPARSE_PROVIDER") or "meilisearch",
                "reranker_provider": _optional(env, "RAG_INDEX_RERANKER_PROVIDER") or "configured",
                "image_vlm_provider": _optional(env, "RAG_INDEX_IMAGE_VLM_PROVIDER")
                or "configured",
                "image_vlm_credential_ref": _optional(env, "RAG_INDEX_IMAGE_VLM_CREDENTIAL_REF")
                or "image-vlm",
                "image_vlm_base_url": _optional(env, "RAG_INDEX_IMAGE_VLM_BASE_URL"),
                "image_vlm_api_key": _optional_secret(env, "RAG_INDEX_IMAGE_VLM_API_KEY"),
                "image_vlm_model": _optional(env, "RAG_INDEX_IMAGE_VLM_MODEL") or "qwen-vl-plus",
                "image_vlm_revision": _optional(env, "RAG_INDEX_IMAGE_VLM_REVISION") or "",
                "image_vlm_timeout_seconds": _int(env, "RAG_INDEX_IMAGE_VLM_TIMEOUT_SECONDS"),
                "image_vlm_devices": _csv(env, "RAG_INDEX_IMAGE_VLM_DEVICES"),
                "image_vlm_vram_gb": _float(env, "RAG_INDEX_IMAGE_VLM_VRAM_GB"),
                "image_vlm_concurrency": _int(env, "RAG_INDEX_IMAGE_VLM_CONCURRENCY"),
                "image_vlm_max_input_bytes": _int(env, "RAG_INDEX_IMAGE_VLM_MAX_INPUT_BYTES"),
                "generation_rollback_days": _int(env, "RAG_INDEX_GENERATION_ROLLBACK_DAYS") or 7,
                "embedding_provider": _optional(env, "RAG_INDEX_EMBEDDING_PROVIDER") or "memory",
                "embedding_base_url": _optional(env, "RAG_INDEX_EMBEDDING_BASE_URL"),
                "embedding_api_key": _optional_secret(env, "RAG_INDEX_EMBEDDING_API_KEY"),
                "embedding_model": _optional(env, "RAG_INDEX_EMBEDDING_MODEL"),
                "embedding_revision": _optional(env, "RAG_INDEX_EMBEDDING_REVISION"),
                "embedding_dimension": _int(env, "RAG_INDEX_EMBEDDING_DIMENSION"),
                "embedding_metric": _optional(env, "RAG_INDEX_EMBEDDING_METRIC") or "cosine",
                "vector_provider": _optional(env, "RAG_INDEX_VECTOR_PROVIDER") or "memory",
                "vector_uri": _optional(env, "RAG_INDEX_VECTOR_URI"),
                "vector_token": _optional_secret(env, "RAG_INDEX_VECTOR_TOKEN"),
                "vector_collection_prefix": _optional(env, "RAG_INDEX_VECTOR_COLLECTION_PREFIX")
                or "ragqs",
                "sparse_url": _optional(env, "RAG_INDEX_SPARSE_URL"),
                "sparse_api_key": _optional_secret(env, "RAG_INDEX_SPARSE_API_KEY"),
                "sparse_username": _optional(env, "RAG_INDEX_SPARSE_USERNAME"),
                "sparse_password": _optional_secret(env, "RAG_INDEX_SPARSE_PASSWORD"),
                "sparse_ca_path": _optional(env, "RAG_INDEX_SPARSE_CA_PATH"),
                "sparse_jvm_heap_min_gb": (
                    _float(env, "RAG_INDEX_SPARSE_JVM_HEAP_MIN_GB")
                    if "RAG_INDEX_SPARSE_JVM_HEAP_MIN_GB" in env
                    else 1.0
                ),
                "sparse_index": _optional(env, "RAG_INDEX_SPARSE_INDEX") or "ragqs_chunks",
                "sparse_data_path": _optional(env, "RAG_INDEX_SPARSE_DATA_PATH"),
                "text_chunk_max_chars": _int(env, "RAG_INDEX_TEXT_CHUNK_MAX_CHARS"),
                "xlsx_merged_cells_max": _int(env, "RAG_INDEX_XLSX_MERGED_CELLS_MAX"),
                "ocr_confidence_threshold": _float(env, "RAG_INDEX_OCR_CONFIDENCE_THRESHOLD"),
                "mineru_provider": _optional(env, "RAG_INDEX_MINERU_PROVIDER") or "disabled",
                "mineru_executable": _optional(env, "RAG_INDEX_MINERU_EXECUTABLE") or "mineru",
                "mineru_timeout_seconds": _int(env, "RAG_INDEX_MINERU_TIMEOUT_SECONDS"),
                "contextual_retrieval_provider": (
                    _optional(env, "RAG_INDEX_CONTEXTUAL_RETRIEVAL_PROVIDER")
                    or contextual_defaults.get("contextual_retrieval_provider")
                    or "disabled"
                ),
                "contextual_retrieval_base_url": (
                    _optional(env, "RAG_INDEX_CONTEXTUAL_RETRIEVAL_BASE_URL")
                    or contextual_defaults.get("contextual_retrieval_base_url")
                ),
                "contextual_retrieval_api_key": (
                    _optional_secret(env, "RAG_INDEX_CONTEXTUAL_RETRIEVAL_API_KEY")
                    or contextual_defaults.get("contextual_retrieval_api_key")
                ),
                "contextual_retrieval_model": (
                    _optional(env, "RAG_INDEX_CONTEXTUAL_RETRIEVAL_MODEL") or "ds-v4-flash"
                ),
                "contextual_retrieval_revision": (
                    _optional(env, "RAG_INDEX_CONTEXTUAL_RETRIEVAL_REVISION") or "ds-v4-flash"
                ),
                "contextual_retrieval_timeout_seconds": _int(
                    env, "RAG_INDEX_CONTEXTUAL_RETRIEVAL_TIMEOUT_SECONDS"
                ),
                "contextual_retrieval_concurrency": _int(
                    env, "RAG_INDEX_CONTEXTUAL_RETRIEVAL_CONCURRENCY"
                ),
                "contextual_retrieval_prefix_token_limit": _int(
                    env, "RAG_INDEX_CONTEXTUAL_RETRIEVAL_PREFIX_TOKEN_LIMIT"
                ),
                "contextual_prefix_cache_provider": (
                    _optional(env, "RAG_INDEX_CONTEXTUAL_PREFIX_CACHE_PROVIDER") or "memory"
                ),
                "tree_search_provider": (
                    _optional(env, "RAG_INDEX_TREE_SEARCH_PROVIDER") or "disabled"
                ),
                "tree_search_base_url": _optional(env, "RAG_INDEX_TREE_SEARCH_BASE_URL"),
                "tree_search_api_key": _optional_secret(env, "RAG_INDEX_TREE_SEARCH_API_KEY"),
                "tree_search_model": _optional(env, "RAG_INDEX_TREE_SEARCH_MODEL") or "ds-v4-flash",
                "tree_search_timeout_seconds": _int(env, "RAG_INDEX_TREE_SEARCH_TIMEOUT_SECONDS"),
                "reranker_base_url": _optional(env, "RAG_INDEX_RERANKER_BASE_URL"),
                "reranker_api_key": _optional_secret(env, "RAG_INDEX_RERANKER_API_KEY"),
                "reranker_coarse_model": (
                    _optional(env, "RAG_INDEX_RERANKER_COARSE_MODEL") or "qwen3-reranker-0.6b"
                ),
                "reranker_final_model": (
                    _optional(env, "RAG_INDEX_RERANKER_FINAL_MODEL") or "qwen3-reranker-8b"
                ),
                "reranker_coarse_revision": (
                    _optional(env, "RAG_INDEX_RERANKER_COARSE_REVISION") or ""
                ),
                "reranker_final_revision": _optional(env, "RAG_INDEX_RERANKER_FINAL_REVISION")
                or "",
                "reranker_quantization": (
                    _optional(env, "RAG_INDEX_RERANKER_QUANTIZATION") or "int8"
                ),
                "reranker_tokenizer_version": (
                    _optional(env, "RAG_INDEX_RERANKER_TOKENIZER_VERSION") or "unspecified"
                ),
                "reranker_timeout_seconds": _int(env, "RAG_INDEX_RERANKER_TIMEOUT_SECONDS"),
            }.items()
            if value is not None
        },
        "graph": {
            key: value
            for key, value in {
                "extraction_provider": (
                    _optional(env, "RAG_GRAPH_EXTRACTION_PROVIDER") or "deterministic"
                ),
                "extraction_base_url": _optional(env, "RAG_GRAPH_EXTRACTION_BASE_URL"),
                "extraction_api_key": _optional_secret(env, "RAG_GRAPH_EXTRACTION_API_KEY"),
                "extraction_model": _optional(env, "RAG_GRAPH_EXTRACTION_MODEL")
                or "public-graph-extraction-v1",
                "extraction_prompt_version": (
                    _optional(env, "RAG_GRAPH_EXTRACTION_PROMPT_VERSION") or "public-graph-v1"
                ),
                "extraction_timeout_seconds": _int(env, "RAG_GRAPH_EXTRACTION_TIMEOUT_SECONDS"),
            }.items()
            if value is not None
        },
        "documents": {
            key: value
            for key, value in {
                "upload_max_bytes": _int(env, "RAG_DOCUMENTS_UPLOAD_MAX_BYTES"),
                "upload_max_files_per_request": _int(env, "RAG_UPLOAD_MAX_FILES_PER_REQUEST"),
                "upload_max_request_bytes": _int(env, "RAG_UPLOAD_MAX_REQUEST_BYTES"),
                "cleanup_max_attempts": _int(env, "RAG_DOCUMENTS_CLEANUP_MAX_ATTEMPTS"),
                "version_retention_days": _int(env, "DOCUMENT_VERSION_RETENTION_DAYS"),
            }.items()
            if value is not None
        },
        "chat": {
            key: value
            for key, value in {
                "effort_rag_call_limit_quick": _int(env, "RAG_EFFORT_RAG_CALL_LIMIT_QUICK"),
                "effort_rag_call_limit_think": _int(env, "RAG_EFFORT_RAG_CALL_LIMIT_THINK"),
                "effort_rag_call_limit_deep": _int(env, "RAG_EFFORT_RAG_CALL_LIMIT_DEEP"),
                "ask_rate_limit_per_minute": (
                    _int(env, "RAG_CHAT_ASK_RATE_LIMIT_PER_MINUTE")
                    if _optional(env, "RAG_CHAT_ASK_RATE_LIMIT_PER_MINUTE") is not None
                    else _int(env, "CHAT_ASK_RATE_LIMIT_PER_MINUTE")
                ),
                "generation_disconnect_grace_seconds": _int(
                    env,
                    (
                        "RAG_GENERATION_DISCONNECT_GRACE_SECONDS"
                        if _optional(env, "RAG_GENERATION_DISCONNECT_GRACE_SECONDS") is not None
                        else "GENERATION_DISCONNECT_GRACE_SECONDS"
                    ),
                ),
                "enhance_model": _optional(env, "RAG_CHAT_ENHANCE_MODEL"),
                "enhance_timeout_seconds": _int(env, "RAG_CHAT_ENHANCE_TIMEOUT_SECONDS"),
                "enhance_max_prompt_chars": _int(env, "RAG_CHAT_ENHANCE_MAX_PROMPT_CHARS"),
                "max_content_chars": _int(env, "RAG_CHAT_MAX_CONTENT_CHARS"),
            }.items()
            if value is not None
        },
        "evaluation": {
            key: value
            for key, value in {
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
                "cookie_secure": _optional_bool(env, "RAG_AUTH_COOKIE_SECURE"),
                "login_max_attempts": _int(env, "RAG_AUTH_LOGIN_MAX_ATTEMPTS"),
                "login_lock_seconds": _int(env, "RAG_AUTH_LOGIN_LOCK_SECONDS"),
                "user_deletion_retention_days": _int(env, "USER_DELETION_RETENTION_DAYS"),
                "user_deletion_archive_dir": _optional(env, "USER_DELETION_ARCHIVE_DIR"),
                "secret_key": _optional(env, "RAG_AUTH_SECRET_KEY"),
                "allowed_origins": _csv(env, "RAG_AUTH_ALLOWED_ORIGINS"),
                "admin_roster": _csv(env, "RAG_AUTH_ADMIN_ROSTER"),
                "bootstrap_username": _optional(env, "RAG_AUTH_BOOTSTRAP_USERNAME"),
                "bootstrap_password": _optional(env, "RAG_AUTH_BOOTSTRAP_PASSWORD"),
                "bootstrap_real_name": _optional(env, "RAG_AUTH_BOOTSTRAP_REAL_NAME"),
                "bootstrap_display_name": _optional(env, "RAG_AUTH_BOOTSTRAP_DISPLAY_NAME"),
                "bootstrap_user_id": _optional(env, "RAG_AUTH_BOOTSTRAP_USER_ID"),
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
        judge = settings.evaluation
        judge_api_key = judge.judge_api_key
        if (
            not judge.judge_base_url
            or not judge.judge_base_url.strip()
            or judge_api_key is None
            or not judge_api_key.get_secret_value().strip()
            or not judge.judge_credential_ref.strip()
            or not judge.judge_model.strip()
        ):
            raise PlatformConfigurationError(
                "production evaluation judge configuration is incomplete"
            )
        if judge.judge_credential_ref == settings.index.image_vlm_credential_ref:
            raise PlatformConfigurationError(
                "production judge and image VLM credential references must differ"
            )
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
        image_vlm = settings.index.image_vlm_provider.casefold()
        if image_vlm in {"bailian", "internvl"}:
            if not settings.index.image_vlm_base_url:
                raise ValueError("production image VLM provider requires a base URL")
            if image_vlm == "bailian" and (
                settings.index.image_vlm_api_key is None
                or not settings.index.image_vlm_api_key.get_secret_value()
            ):
                raise ValueError("production bailian image VLM requires an API key")
            if image_vlm == "internvl" and not settings.index.image_vlm_model:
                raise ValueError("production InternVL image VLM requires a model ID")
        if settings.provider.api_key is None or not settings.provider.api_key.get_secret_value():
            raise ValueError("production provider api key is required")
        # 「优化输入」复用全局 provider 凭证/地址（上方已强制 api key）；enhance_model
        # 由 Literal 锁死，enhance_timeout_seconds/enhance_max_prompt_chars 由 Field
        # 边界约束，无额外生产规则。
        if settings.index.contextual_retrieval_provider == "disabled":
            raise ValueError("production contextual retrieval provider cannot be disabled")
        if not settings.index.contextual_retrieval_base_url:
            raise ValueError("production contextual retrieval provider requires a base URL")
        contextual_key = settings.index.contextual_retrieval_api_key
        if contextual_key is None or not contextual_key.get_secret_value():
            raise ValueError("production contextual retrieval provider requires an API key")
        # 判官与被评测管线模型（生成/CR/rerank）不得同族（后端设计 §8.1）。
        # 家族清单唯一维护在评测策略层；此处延迟导入避免拉宽 config 的加载面。
        from app.evaluation.policy import assert_judge_family_isolation

        pipeline_models: dict[str, tuple[str, str | None]] = {
            "generation": (settings.provider.name, None),
        }
        if settings.index.contextual_retrieval_provider != "disabled":
            pipeline_models["contextual_retrieval"] = (
                settings.index.contextual_retrieval_provider,
                settings.index.contextual_retrieval_model,
            )
        if settings.index.reranker_provider not in {"none", "configured"}:
            pipeline_models["reranker"] = (settings.index.reranker_provider, None)
        assert_judge_family_isolation(
            judge_provider=settings.evaluation.judge_provider,
            judge_model=settings.evaluation.judge_model,
            pipeline_models=pipeline_models,
        )
        if settings.debug:
            raise ValueError("production debug must be disabled")
        if settings.auth.secret_key is None or not settings.auth.secret_key.get_secret_value():
            raise ValueError("production auth secret key is required")
        if not settings.auth.allowed_origins:
            raise ValueError("production auth allowed origins are required")
        if not settings.auth.admin_roster:
            raise ValueError("production auth admin roster is required")
        if not settings.backup.target_namespace:
            raise ValueError(
                "production requires a backup target namespace (RAG_BACKUP_TARGET_NAMESPACE)"
            )
        _precheck_user_deletion_archive_dir(settings)
    if settings.index.contextual_retrieval_provider != "disabled":
        if not settings.index.contextual_retrieval_base_url:
            raise ValueError("contextual retrieval provider requires a base URL")
        contextual_key = settings.index.contextual_retrieval_api_key
        if contextual_key is None or not contextual_key.get_secret_value():
            raise ValueError("contextual retrieval provider requires an API key")
    if settings.index.tree_search_provider == "dashscope":
        if not settings.index.tree_search_base_url:
            raise ValueError("tree search provider requires a base URL")
        tree_key = settings.index.tree_search_api_key
        if tree_key is None or not tree_key.get_secret_value():
            raise ValueError("tree search provider requires an API key")
    if settings.index.reranker_base_url:
        # RerankerRelease 身份字段缺省即拒绝装配（发布身份必须锁 revision）。
        if not settings.index.reranker_coarse_revision.strip():
            raise ValueError("configured reranker requires a coarse stage revision")
        if not settings.index.reranker_final_revision.strip():
            raise ValueError("configured reranker requires a final stage revision")
    if settings.graph.extraction_provider == "llm":
        if not settings.graph.extraction_base_url:
            raise ValueError("graph LLM extraction provider requires a base URL")


# 归档目录不得落入 Web 静态资源、上传目录或其他业务接口可直接读取的目录。
_FORBIDDEN_ARCHIVE_DIR_PARTS = frozenset({"static", "uploads", "frontend", "public"})


def resolve_user_deletion_archive_dir(settings: PlatformSettings) -> str:
    """Return the effective account-deletion archive directory as an absolute path."""

    return _resolve_user_deletion_archive_dir(settings.auth, settings.profile)


def _resolve_user_deletion_archive_dir(auth: AuthSettings, profile: str) -> str:
    configured = auth.user_deletion_archive_dir
    if configured is None or not configured.strip():
        if profile == "production":
            raise ValueError("production requires USER_DELETION_ARCHIVE_DIR to be configured")
        return os.path.abspath(os.path.join("data", "user-deletion-archives"))
    # isabs must run on the raw configured string: expanduser would turn "~/x"
    # into an absolute path and silently mask a deployment-relative location.
    raw = configured.strip()
    if not os.path.isabs(raw):
        raise ValueError(
            "USER_DELETION_ARCHIVE_DIR must be an absolute path "
            "(~ expansion and relative paths are not accepted)"
        )
    path = os.path.abspath(os.path.expanduser(raw))
    lowered = {part.lower() for part in path.replace("\\", "/").split("/")}
    if lowered & _FORBIDDEN_ARCHIVE_DIR_PARTS:
        raise ValueError(
            "USER_DELETION_ARCHIVE_DIR must not live in a web-static, upload or "
            "otherwise business-readable directory"
        )
    return path


def _precheck_user_deletion_archive_dir(settings: PlatformSettings) -> None:
    path = resolve_user_deletion_archive_dir(settings)
    if settings.profile != "production":
        return
    os.makedirs(path, exist_ok=True)
    probe = os.path.join(path, ".rag-precheck")
    try:
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
    except OSError as exc:
        raise ValueError(
            "USER_DELETION_ARCHIVE_DIR must be writable by the backend process"
        ) from exc
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass
