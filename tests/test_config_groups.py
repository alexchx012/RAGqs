from app.config import (
    AgentConfig,
    AppConfig,
    ChunkingConfig,
    CorsConfig,
    DashScopeConfig,
    DeploymentConfig,
    MilvusConfig,
    OpenAICompatibleConfig,
    ProviderConfig,
    RagConfig,
    Settings,
    StorageConfig,
    UploadConfig,
)
from app.operations.config_validation import validate_settings
from app.providers.selection import ProviderSelection


def test_development_defaults_use_sqlite_for_runtime_state():
    settings = Settings(_env_file=None, dashscope_api_key="sk-valid")
    selection = ProviderSelection.from_settings(settings)

    assert settings.upload_allowed_extensions == "txt,md,markdown,csv,html,htm,json"
    assert settings.session_store_provider == "sqlite"
    assert settings.retrieval_audit_store_provider == "sqlite"
    assert settings.indexing_job_store_provider == "sqlite"
    assert settings.indexing_queue_provider == "sqlite"
    assert settings.indexing_queue_sqlite_path == "data/indexing-queue.sqlite3"
    assert settings.indexing_queue_postgres_dsn == ""
    assert settings.indexing_queue_lease_timeout_seconds == 300.0
    assert settings.indexing_worker_recover_pending_jobs is True
    assert settings.document_catalog_provider == "sqlite"
    assert settings.checkpoint_provider == "sqlite"
    assert settings.providers.session_store == "sqlite"
    assert settings.providers.retrieval_audit_store == "sqlite"
    assert settings.providers.checkpoint == "sqlite"
    assert settings.storage.indexing_queue_provider == "sqlite"
    assert settings.storage.indexing_queue_sqlite_path == "data/indexing-queue.sqlite3"
    assert settings.storage.indexing_job_store_provider == "sqlite"
    assert settings.storage.document_catalog_provider == "sqlite"
    assert selection.session_store_provider == "sqlite"
    assert selection.retrieval_audit_store_provider == "sqlite"
    assert selection.checkpoint_provider == "sqlite"
    assert validate_settings(settings).is_valid is True


def test_settings_exposes_typed_groups_while_preserving_flat_env_fields():
    settings = Settings(
        _env_file=None,
        app_name="Grouped RAG",
        app_version="2.0.0",
        debug=True,
        host="127.0.0.1",
        port=9911,
        cors_allow_origins="http://localhost:9911,https://rag.example.com",
        cors_allow_credentials=False,
        upload_allowed_extensions="txt,md,pdf",
        upload_max_bytes=2048,
        upload_prompt_injection_scan_enabled=False,
        deployment_environment="staging",
        chat_provider="openai_compatible",
        embedding_provider="fake",
        vector_store_provider="fake",
        session_store_provider="sqlite",
        retrieval_audit_store_provider="sqlite",
        ingestion_provider="fake",
        checkpoint_provider="sqlite",
        session_store_sqlite_path="data/test-sessions.sqlite3",
        session_store_postgres_dsn="postgresql://rag:secret@db/ragqs",
        retrieval_audit_sqlite_path="data/test-retrieval-audits.sqlite3",
        retrieval_audit_postgres_dsn="postgresql://rag:secret@db/ragqs-audits",
        indexing_execution_mode="background",
        indexing_queue_provider="postgres",
        indexing_queue_sqlite_path="data/test-indexing-queue.sqlite3",
        indexing_queue_postgres_dsn="postgresql://rag:secret@db/ragqs-queue",
        indexing_queue_lease_timeout_seconds=120.0,
        indexing_worker_poll_interval_seconds=0.5,
        indexing_worker_shutdown_timeout_seconds=3.0,
        indexing_worker_recover_pending_jobs=False,
        indexing_job_store_provider="sqlite",
        indexing_job_store_sqlite_path="data/test-indexing.sqlite3",
        indexing_job_store_postgres_dsn="postgresql://rag:secret@db/ragqs-indexing",
        document_catalog_provider="sqlite",
        document_catalog_sqlite_path="data/test-documents.sqlite3",
        document_catalog_postgres_dsn="postgresql://rag:secret@db/ragqs-documents",
        checkpoint_sqlite_path="data/test-checkpoints.sqlite3",
        checkpoint_postgres_dsn="postgresql://rag:secret@db/ragqs-checkpoints",
        agent_runtime="legacy",
        enabled_tools="retrieve_knowledge",
        tool_planning_enabled=True,
        tool_planning_excluded_tools="retrieve_knowledge",
        prompt_profile="strict",
        openai_compatible_api_key="sk-openai",
        openai_compatible_base_url="https://models.example.com/v1",
        openai_compatible_model="gpt-test",
        openai_compatible_embedding_model="embed-test",
        dashscope_api_key="sk-dashscope",
        dashscope_model="qwen-plus",
        dashscope_embedding_model="text-embedding-v4",
        milvus_host="milvus.local",
        milvus_port=19531,
        milvus_timeout=5000,
        rag_top_k=8,
        rag_model="qwen-plus",
        retrieval_profile="high_recall",
        retrieval_high_recall_top_k_multiplier=3,
        retrieval_relaxed_filter_preserve_keys="space_id,tenant_id",
        query_rewriter_provider="llm",
        reranker_provider="llm",
        context_compressor_provider="llm",
        context_compressor_max_characters=600,
        chunk_max_size=1200,
        chunk_overlap=120,
    )

    assert settings.app == AppConfig(
        name="Grouped RAG",
        version="2.0.0",
        debug=True,
        host="127.0.0.1",
        port=9911,
    )
    assert settings.cors == CorsConfig(
        allow_origins="http://localhost:9911,https://rag.example.com",
        allow_credentials=False,
    )
    assert settings.upload == UploadConfig(
        allowed_extensions="txt,md,pdf",
        max_bytes=2048,
        prompt_injection_scan_enabled=False,
    )
    assert settings.deployment == DeploymentConfig(environment="staging")
    assert settings.providers == ProviderConfig(
        chat="openai_compatible",
        embedding="fake",
        vector_store="fake",
        session_store="sqlite",
        retrieval_audit_store="sqlite",
        ingestion="fake",
        checkpoint="sqlite",
    )
    assert settings.storage == StorageConfig(
        session_store_sqlite_path="data/test-sessions.sqlite3",
        session_store_postgres_dsn="postgresql://rag:secret@db/ragqs",
        retrieval_audit_sqlite_path="data/test-retrieval-audits.sqlite3",
        retrieval_audit_postgres_dsn="postgresql://rag:secret@db/ragqs-audits",
        indexing_execution_mode="background",
        indexing_queue_provider="postgres",
        indexing_queue_sqlite_path="data/test-indexing-queue.sqlite3",
        indexing_queue_postgres_dsn="postgresql://rag:secret@db/ragqs-queue",
        indexing_queue_lease_timeout_seconds=120.0,
        indexing_worker_poll_interval_seconds=0.5,
        indexing_worker_shutdown_timeout_seconds=3.0,
        indexing_worker_recover_pending_jobs=False,
        indexing_job_store_provider="sqlite",
        indexing_job_store_sqlite_path="data/test-indexing.sqlite3",
        indexing_job_store_postgres_dsn="postgresql://rag:secret@db/ragqs-indexing",
        document_catalog_provider="sqlite",
        document_catalog_sqlite_path="data/test-documents.sqlite3",
        document_catalog_postgres_dsn="postgresql://rag:secret@db/ragqs-documents",
        checkpoint_sqlite_path="data/test-checkpoints.sqlite3",
        checkpoint_postgres_dsn="postgresql://rag:secret@db/ragqs-checkpoints",
    )
    assert settings.agent == AgentConfig(
        runtime="legacy",
        enabled_tools="retrieve_knowledge",
        tool_planning_enabled=True,
        tool_planning_excluded_tools="retrieve_knowledge",
        prompt_profile="strict",
    )
    assert settings.openai_compatible == OpenAICompatibleConfig(
        api_key="sk-openai",
        base_url="https://models.example.com/v1",
        model="gpt-test",
        embedding_model="embed-test",
    )
    assert settings.dashscope == DashScopeConfig(
        api_key="sk-dashscope",
        model="qwen-plus",
        embedding_model="text-embedding-v4",
    )
    assert settings.milvus == MilvusConfig(
        host="milvus.local",
        port=19531,
        timeout=5000,
    )
    assert settings.rag == RagConfig(
        top_k=8,
        model="qwen-plus",
        retrieval_profile="high_recall",
        retrieval_high_recall_top_k_multiplier=3,
        retrieval_relaxed_filter_preserve_keys="space_id,tenant_id",
        query_rewriter_provider="llm",
        reranker_provider="llm",
        context_compressor_provider="llm",
        context_compressor_max_characters=600,
    )
    assert settings.chunking == ChunkingConfig(max_size=1200, overlap=120)

    assert settings.app_name == settings.app.name
    assert settings.deployment_environment == settings.deployment.environment
    assert settings.chat_provider == settings.providers.chat
    assert settings.retrieval_audit_store_provider == settings.providers.retrieval_audit_store
    assert settings.rag_top_k == settings.rag.top_k


def test_provider_selection_and_validation_use_grouped_settings():
    settings = Settings(
        _env_file=None,
        dashscope_api_key="sk-valid",
        chat_provider="fake",
        embedding_provider="fake",
        vector_store_provider="fake",
        session_store_provider="sqlite",
        session_store_sqlite_path="data/sessions.sqlite3",
        retrieval_audit_store_provider="sqlite",
        retrieval_audit_sqlite_path="data/retrieval-audits.sqlite3",
        retrieval_audit_postgres_dsn="postgresql://rag:secret@db/ragqs-audits",
        ingestion_provider="fake",
        checkpoint_provider="sqlite",
        checkpoint_sqlite_path="data/checkpoints.sqlite3",
        indexing_job_store_provider="sqlite",
        indexing_job_store_sqlite_path="data/indexing.sqlite3",
        document_catalog_provider="sqlite",
        document_catalog_sqlite_path="data/documents.sqlite3",
    )

    selection = ProviderSelection.from_settings(settings)
    report = validate_settings(settings)

    assert selection.session_store_provider == "sqlite"
    assert selection.retrieval_audit_store_provider == "sqlite"
    assert selection.checkpoint_provider == "sqlite"
    assert report.is_valid is True
