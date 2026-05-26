from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_phase0_baseline_artifacts_exist():
    expected_paths = [
        ".env.example",
        "docs/architecture/baseline-audit.md",
        "docs/architecture/risk-register.md",
        "docs/evaluation.md",
        "docs/operations.md",
        "docs/deployment.md",
        "docs/extension-guide.md",
        "docs/templates/business-rag-template.md",
        "docs/superpowers/plans/2026-05-23-rag-agent-foundation.md",
        "scripts/validate-baseline.ps1",
        "scripts/run-evaluation.ps1",
        "scripts/run-integration-smoke.ps1",
        "scripts/run-postgres-smoke.ps1",
        "scripts/check-api-health.ps1",
        ".github/workflows/ci.yml",
        "data/evaluation/golden.jsonl",
        "data/evaluation/business.example.jsonl",
        "docs/business-samples/hr-handbook.md",
        "docs/business-samples/benefits-guide.md",
        "docs/business-samples/expense-policy.md",
        "docs/business-samples/support-sla.md",
    ]

    missing = [path for path in expected_paths if not (ROOT / path).exists()]

    assert missing == []


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


def test_architecture_audit_captures_current_boundaries_and_limitations():
    audit = read("docs/architecture/baseline-audit.md")

    for phrase in [
        "Current Architecture",
        "FastAPI",
        "LangGraph",
        "Milvus",
        "DashScope",
        "Known Limitations",
        "Target Foundation Direction",
    ]:
        assert phrase in audit


def test_risk_register_covers_foundation_risks():
    risk_register = read("docs/architecture/risk-register.md")

    for phrase in [
        "Configuration",
        "Retrieval Quality",
        "Session Persistence",
        "Indexing Reliability",
        "Observability",
        "Security",
        "Mitigation",
    ]:
        assert phrase in risk_register


def test_implementation_plan_preserves_full_goal_scope():
    plan = read("docs/superpowers/plans/2026-05-23-rag-agent-foundation.md")

    for phrase in [
        "Phase 0",
        "Phase 1",
        "Phase 2",
        "Phase 3",
        "Phase 4",
        "Phase 5",
        "Phase 6",
        "Phase 7",
        "Phase 8",
        "StateGraph",
        "provider",
        "evaluation",
    ]:
        assert phrase in plan


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


def test_deployment_docs_cover_phase7_operational_runbook():
    docs = read("docs/deployment.md")

    for phrase in [
        "Deployment Runbook",
        "start.ps1",
        "DockerProfile",
        "check-api-health.ps1",
        "run-integration-smoke.ps1",
        "run-postgres-smoke.ps1",
        "run-evaluation.ps1",
        "ReportPath",
        ".github/workflows/ci.yml",
        "GitHub Actions",
        "Milvus stays running",
    ]:
        assert phrase in docs


def test_extension_docs_cover_phase8_foundation_templates():
    docs = read("docs/extension-guide.md")
    template = read("docs/templates/business-rag-template.md")

    for phrase in [
        "Tool Registry",
        "Provider Switching",
        "Prompt Profiles",
        "Tool Planning",
        "Retrieval Enhancers",
        "Second-Business Template",
    ]:
        assert phrase in docs

    for phrase in [
        "ENABLED_TOOLS",
        "TOOL_PLANNING_ENABLED",
        "PROMPT_PROFILE",
        "CHAT_PROVIDER",
        "INDEXING_QUEUE_PROVIDER",
        "QUERY_REWRITER_PROVIDER",
        "RETRIEVAL_PROFILE",
        "RERANKER_PROVIDER",
        "CONTEXT_COMPRESSOR_PROVIDER",
        "do not modify core code",
    ]:
        assert phrase in template


def test_evaluation_docs_explain_langsmith_tracing_setup():
    env_example = read(".env.example")
    docs = read("docs/evaluation.md")

    for key in [
        "LANGSMITH_TRACING",
        "LANGSMITH_ENDPOINT",
        "LANGSMITH_API_KEY",
        "LANGSMITH_PROJECT",
    ]:
        assert f"{key}=" in env_example
        assert key in docs

    for phrase in [
        "data/evaluation/business.example.jsonl",
        "MinExamples",
        "metadata.spaceId",
    ]:
        assert phrase in docs


def test_operations_docs_cover_trace_ids_and_health_checks():
    docs = read("docs/operations.md")

    for phrase in [
        "X-Trace-Id",
        "http_request",
        "latencyMs",
        "Retrieval Audit",
        "RETRIEVAL_AUDIT_STORE_PROVIDER",
        "/api/chat/audits",
        "modelProvider",
        "embeddingProvider",
        "vectorStore",
        "sessionStore",
        "config validation",
        "Docker Profiles",
        "Dependency Health Preflight",
        "Background Indexing",
        "INDEXING_QUEUE_PROVIDER",
        "INDEXING_WORKER_RECOVER_PENDING_JOBS",
        "Postgres Smoke Checks",
        "run-postgres-smoke.ps1",
        "CORS_ALLOW_ORIGINS",
        "CORS_ALLOW_CREDENTIALS",
        "DEPLOYMENT_ENVIRONMENT",
    ]:
        assert phrase in docs


if __name__ == "__main__":
    test_phase0_baseline_artifacts_exist()
    test_env_example_documents_all_current_settings()
    test_architecture_audit_captures_current_boundaries_and_limitations()
    test_risk_register_covers_foundation_risks()
    test_implementation_plan_preserves_full_goal_scope()
    test_baseline_validation_script_runs_core_checks()
    test_evaluation_script_enforces_core_metric_thresholds()
    test_deployment_docs_cover_phase7_operational_runbook()
    test_extension_docs_cover_phase8_foundation_templates()
    test_evaluation_docs_explain_langsmith_tracing_setup()
    test_operations_docs_cover_trace_ids_and_health_checks()
    print("phase0 baseline tests passed")
