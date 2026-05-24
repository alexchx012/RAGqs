"""配置管理模块"""

from pydantic import BaseModel, ConfigDict
from pydantic_settings import BaseSettings, SettingsConfigDict


class FrozenConfigModel(BaseModel):
    """Immutable typed view over a settings group."""

    model_config = ConfigDict(frozen=True)


class AppConfig(FrozenConfigModel):
    name: str
    version: str
    debug: bool
    host: str
    port: int


class CorsConfig(FrozenConfigModel):
    allow_origins: str
    allow_credentials: bool


class UploadConfig(FrozenConfigModel):
    allowed_extensions: str
    max_bytes: int
    prompt_injection_scan_enabled: bool


class ProviderConfig(FrozenConfigModel):
    chat: str
    embedding: str
    vector_store: str
    session_store: str
    ingestion: str
    checkpoint: str


class StorageConfig(FrozenConfigModel):
    session_store_sqlite_path: str
    session_store_postgres_dsn: str
    indexing_job_store_provider: str
    indexing_job_store_sqlite_path: str
    indexing_job_store_postgres_dsn: str
    document_catalog_provider: str
    document_catalog_sqlite_path: str
    document_catalog_postgres_dsn: str
    checkpoint_sqlite_path: str
    checkpoint_postgres_dsn: str


class AgentConfig(FrozenConfigModel):
    runtime: str
    enabled_tools: str
    tool_planning_enabled: bool
    tool_planning_excluded_tools: str
    prompt_profile: str


class OpenAICompatibleConfig(FrozenConfigModel):
    api_key: str
    base_url: str
    model: str
    embedding_model: str


class DashScopeConfig(FrozenConfigModel):
    api_key: str
    model: str
    embedding_model: str


class MilvusConfig(FrozenConfigModel):
    host: str
    port: int
    timeout: int


class RagConfig(FrozenConfigModel):
    top_k: int
    model: str


class ChunkingConfig(FrozenConfigModel):
    max_size: int
    overlap: int


class Settings(BaseSettings):
    """应用配置"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 应用配置
    app_name: str = "RAG Knowledge Agent"
    app_version: str = "1.0.0"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 9900
    cors_allow_origins: str = "http://127.0.0.1:9900,http://localhost:9900"
    cors_allow_credentials: bool = True
    upload_allowed_extensions: str = "txt,md"
    upload_max_bytes: int = 10 * 1024 * 1024
    upload_prompt_injection_scan_enabled: bool = True

    # 扩展配置
    chat_provider: str = "dashscope"
    embedding_provider: str = "dashscope"
    vector_store_provider: str = "milvus"
    session_store_provider: str = "memory"
    session_store_sqlite_path: str = "data/sessions.sqlite3"
    session_store_postgres_dsn: str = ""
    ingestion_provider: str = "vector_index"
    indexing_job_store_provider: str = "memory"
    indexing_job_store_sqlite_path: str = "data/indexing-jobs.sqlite3"
    indexing_job_store_postgres_dsn: str = ""
    document_catalog_provider: str = "memory"
    document_catalog_sqlite_path: str = "data/document-catalog.sqlite3"
    document_catalog_postgres_dsn: str = ""
    checkpoint_provider: str = "memory"
    checkpoint_sqlite_path: str = "data/checkpoints.sqlite3"
    checkpoint_postgres_dsn: str = ""
    agent_runtime: str = "explicit_graph"
    enabled_tools: str = "retrieve_knowledge,get_current_time"
    tool_planning_enabled: bool = False
    tool_planning_excluded_tools: str = "retrieve_knowledge"
    prompt_profile: str = "default"

    # OpenAI-compatible provider 配置
    openai_compatible_api_key: str = ""
    openai_compatible_base_url: str = ""
    openai_compatible_model: str = ""
    openai_compatible_embedding_model: str = ""

    # DashScope 配置
    dashscope_api_key: str = ""
    dashscope_model: str = "qwen-max"
    dashscope_embedding_model: str = "text-embedding-v4"

    # Milvus 配置
    milvus_host: str = "localhost"
    milvus_port: int = 19530
    milvus_timeout: int = 10000

    # RAG 配置
    rag_top_k: int = 3
    rag_model: str = "qwen-max"

    # 文档分块配置
    chunk_max_size: int = 800
    chunk_overlap: int = 100

    @property
    def app(self) -> AppConfig:
        return AppConfig(
            name=self.app_name,
            version=self.app_version,
            debug=self.debug,
            host=self.host,
            port=self.port,
        )

    @property
    def cors(self) -> CorsConfig:
        return CorsConfig(
            allow_origins=self.cors_allow_origins,
            allow_credentials=self.cors_allow_credentials,
        )

    @property
    def upload(self) -> UploadConfig:
        return UploadConfig(
            allowed_extensions=self.upload_allowed_extensions,
            max_bytes=self.upload_max_bytes,
            prompt_injection_scan_enabled=self.upload_prompt_injection_scan_enabled,
        )

    @property
    def providers(self) -> ProviderConfig:
        return ProviderConfig(
            chat=self.chat_provider,
            embedding=self.embedding_provider,
            vector_store=self.vector_store_provider,
            session_store=self.session_store_provider,
            ingestion=self.ingestion_provider,
            checkpoint=self.checkpoint_provider,
        )

    @property
    def storage(self) -> StorageConfig:
        return StorageConfig(
            session_store_sqlite_path=self.session_store_sqlite_path,
            session_store_postgres_dsn=self.session_store_postgres_dsn,
            indexing_job_store_provider=self.indexing_job_store_provider,
            indexing_job_store_sqlite_path=self.indexing_job_store_sqlite_path,
            indexing_job_store_postgres_dsn=self.indexing_job_store_postgres_dsn,
            document_catalog_provider=self.document_catalog_provider,
            document_catalog_sqlite_path=self.document_catalog_sqlite_path,
            document_catalog_postgres_dsn=self.document_catalog_postgres_dsn,
            checkpoint_sqlite_path=self.checkpoint_sqlite_path,
            checkpoint_postgres_dsn=self.checkpoint_postgres_dsn,
        )

    @property
    def agent(self) -> AgentConfig:
        return AgentConfig(
            runtime=self.agent_runtime,
            enabled_tools=self.enabled_tools,
            tool_planning_enabled=self.tool_planning_enabled,
            tool_planning_excluded_tools=self.tool_planning_excluded_tools,
            prompt_profile=self.prompt_profile,
        )

    @property
    def openai_compatible(self) -> OpenAICompatibleConfig:
        return OpenAICompatibleConfig(
            api_key=self.openai_compatible_api_key,
            base_url=self.openai_compatible_base_url,
            model=self.openai_compatible_model,
            embedding_model=self.openai_compatible_embedding_model,
        )

    @property
    def dashscope(self) -> DashScopeConfig:
        return DashScopeConfig(
            api_key=self.dashscope_api_key,
            model=self.dashscope_model,
            embedding_model=self.dashscope_embedding_model,
        )

    @property
    def milvus(self) -> MilvusConfig:
        return MilvusConfig(
            host=self.milvus_host,
            port=self.milvus_port,
            timeout=self.milvus_timeout,
        )

    @property
    def rag(self) -> RagConfig:
        return RagConfig(top_k=self.rag_top_k, model=self.rag_model)

    @property
    def chunking(self) -> ChunkingConfig:
        return ChunkingConfig(max_size=self.chunk_max_size, overlap=self.chunk_overlap)


config = Settings()
