from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TRACKED_BASELINE_ARTIFACTS = [
    ".env.example",
    "scripts/validate-baseline.ps1",
    "scripts/run-evaluation.ps1",
    "scripts/run-integration-smoke.ps1",
    "scripts/run-postgres-smoke.ps1",
    "scripts/check-api-health.ps1",
    ".github/workflows/ci.yml",
    "data/evaluation/golden.jsonl",
    "data/evaluation/business.example.jsonl",
    "data/evaluation/business-samples/hr-handbook.md",
    "data/evaluation/business-samples/benefits-guide.md",
    "data/evaluation/business-samples/expense-policy.md",
    "data/evaluation/business-samples/support-sla.md",
]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_phase0_baseline_artifacts_exist():
    missing = [path for path in TRACKED_BASELINE_ARTIFACTS if not (ROOT / path).exists()]

    assert missing == []


def test_phase0_baseline_artifacts_do_not_require_local_docs():
    assert all(not path.startswith("docs/") for path in TRACKED_BASELINE_ARTIFACTS)


def test_env_example_documents_all_current_settings():
    env_example = read(".env.example")

    for key in [
        "APP_NAME",
        "APP_VERSION",
        "DEBUG",
        "HOST",
        "PORT",
        "CORS_ALLOW_ORIGINS",
        "CORS_ALLOW_CREDENTIALS",
        "UPLOAD_ALLOWED_EXTENSIONS",
        "UPLOAD_MAX_BYTES",
        "UPLOAD_PROMPT_INJECTION_SCAN_ENABLED",
        "DEPLOYMENT_ENVIRONMENT",
        "CHAT_PROVIDER",
        "EMBEDDING_PROVIDER",
        "VECTOR_STORE_PROVIDER",
        "SESSION_STORE_PROVIDER",
        "RETRIEVAL_AUDIT_STORE_PROVIDER",
        "SESSION_STORE_POSTGRES_DSN",
        "RETRIEVAL_AUDIT_SQLITE_PATH",
        "RETRIEVAL_AUDIT_POSTGRES_DSN",
        "INGESTION_PROVIDER",
        "INDEXING_EXECUTION_MODE",
        "INDEXING_QUEUE_PROVIDER",
        "INDEXING_QUEUE_SQLITE_PATH",
        "INDEXING_QUEUE_POSTGRES_DSN",
        "INDEXING_QUEUE_LEASE_TIMEOUT_SECONDS",
        "INDEXING_WORKER_POLL_INTERVAL_SECONDS",
        "INDEXING_WORKER_SHUTDOWN_TIMEOUT_SECONDS",
        "INDEXING_WORKER_RECOVER_PENDING_JOBS",
        "INDEXING_JOB_STORE_POSTGRES_DSN",
        "DOCUMENT_CATALOG_POSTGRES_DSN",
        "CHECKPOINT_POSTGRES_DSN",
        "ENABLED_TOOLS",
        "TOOL_PLANNING_ENABLED",
        "TOOL_PLANNING_EXCLUDED_TOOLS",
        "PROMPT_PROFILE",
        "OPENAI_COMPATIBLE_API_KEY",
        "OPENAI_COMPATIBLE_BASE_URL",
        "OPENAI_COMPATIBLE_MODEL",
        "OPENAI_COMPATIBLE_EMBEDDING_MODEL",
        "DASHSCOPE_API_KEY",
        "DASHSCOPE_MODEL",
        "DASHSCOPE_EMBEDDING_MODEL",
        "MILVUS_HOST",
        "MILVUS_PORT",
        "MILVUS_HEALTH_PORT",
        "MILVUS_TIMEOUT",
        "RAG_TOP_K",
        "RAG_MODEL",
        "RETRIEVAL_PROFILE",
        "RETRIEVAL_HIGH_RECALL_TOP_K_MULTIPLIER",
        "RETRIEVAL_RELAXED_FILTER_PRESERVE_KEYS",
        "QUERY_REWRITER_PROVIDER",
        "RERANKER_PROVIDER",
        "CONTEXT_COMPRESSOR_PROVIDER",
        "CONTEXT_COMPRESSOR_MAX_CHARACTERS",
        "CHUNK_MAX_SIZE",
        "CHUNK_OVERLAP",
    ]:
        assert f"{key}=" in env_example


def test_env_example_documents_langsmith_tracing_settings():
    env_example = read(".env.example")

    for key in [
        "LANGSMITH_TRACING",
        "LANGSMITH_ENDPOINT",
        "LANGSMITH_API_KEY",
        "LANGSMITH_PROJECT",
    ]:
        assert f"{key}=" in env_example


def test_readme_documents_sqlite_development_state_defaults():
    readme = read("README.md")

    for phrase in [
        "本地 SQLite",
        "SESSION_STORE_PROVIDER=sqlite",
        "INDEXING_QUEUE_PROVIDER=sqlite",
        "CHECKPOINT_PROVIDER=sqlite",
        "data/*.sqlite3",
    ]:
        assert phrase in readme


def test_baseline_validation_script_runs_core_checks():
    script = read("scripts/validate-baseline.ps1")

    for phrase in [
        "ruff check app tests",
        "pytest",
        "test_phase0_baseline.py",
        "test_api_models.py",
        "test_config_groups.py",
        "test_provider_contracts.py",
        "test_agent_provider_injection.py",
        "test_provider_factory.py",
        "test_knowledge_tool_provider.py",
        "test_background_indexing_worker.py",
        "test_indexing_queue.py",
        "test_ingestion_foundation.py",
        "test_postgres_indexing_job_store.py",
        "test_postgres_document_catalog.py",
        "test_postgres_checkpoint_provider.py",
        "test_vector_index_service_ingestion.py",
        "test_file_upload_ingestion.py",
        "test_upload_security.py",
        "test_knowledge_spaces_lifecycle.py",
        "test_evaluation_foundation.py",
        "test_retrieval_pipeline.py",
        "test_retrieval_enhancers.py",
        "test_retrieval_profiles.py",
        "test_chat_retrieval_trace.py",
        "test_retrieval_audit.py",
        "test_rag_state_graph.py",
        "test_rag_agent_graph_runtime.py",
        "test_session_store_service.py",
        "test_postgres_session_store.py",
        "test_postgres_smoke.py",
        "test_phase7_operations.py",
        "test_phase8_foundation_templates.py",
        "node tests\\chat-history.test.js",
        "tests\\start-script.validation.ps1",
        "run-evaluation.ps1",
        "run-integration-smoke.ps1",
        "run-postgres-smoke.ps1",
        "start.ps1 -PreflightOnly",
    ]:
        assert phrase in script


def test_evaluation_script_enforces_core_metric_thresholds():
    script = read("scripts/run-evaluation.ps1")

    for phrase in [
        "fake",
        "service",
        "http",
        "model",
        "base-url",
        "timeout-seconds",
        "faithfulness-judge",
        "PreflightOnly",
        "preflight-only",
        "min-retrieval-hit-rate",
        "min-answer-trait-coverage",
        "min-faithfulness",
        "min-citation-accuracy",
        "min-refusal-rate",
        "ReportPath",
        "report-path",
        "MinExamples",
        "min-examples",
    ]:
        assert phrase in script


if __name__ == "__main__":
    test_phase0_baseline_artifacts_exist()
    test_phase0_baseline_artifacts_do_not_require_local_docs()
    test_env_example_documents_all_current_settings()
    test_env_example_documents_langsmith_tracing_settings()
    test_readme_documents_sqlite_development_state_defaults()
    test_baseline_validation_script_runs_core_checks()
    test_evaluation_script_enforces_core_metric_thresholds()
    print("phase0 baseline tests passed")
