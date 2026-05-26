from io import StringIO
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.health import create_health_router
from app.config import Settings
from app.observability.request_context import (
    get_current_trace_id,
    install_request_context_middleware,
)
from app.operations.config_validation import main as config_validation_main
from app.operations.config_validation import validate_settings
from app.operations.health import DependencyHealthCheck, HealthChecker, HealthCheckResult
from app.security.cors import build_cors_options, parse_cors_origins

ROOT = Path(__file__).resolve().parents[1]


def test_request_context_middleware_propagates_trace_id_and_emits_structured_log():
    records = []
    ticks = iter([10.0, 10.125])
    app = FastAPI()
    install_request_context_middleware(
        app,
        access_log_sink=records.append,
        clock=lambda: next(ticks),
    )

    @app.get("/probe")
    async def probe(request: Request):
        return {
            "traceId": request.state.trace_id,
            "currentTraceId": get_current_trace_id(),
        }

    response = TestClient(app).get("/probe", headers={"X-Trace-Id": "trace-123"})

    assert response.status_code == 200
    assert response.headers["X-Trace-Id"] == "trace-123"
    assert response.json() == {
        "traceId": "trace-123",
        "currentTraceId": "trace-123",
    }
    assert records == [
        {
            "event": "http_request",
            "traceId": "trace-123",
            "method": "GET",
            "path": "/probe",
            "statusCode": 200,
            "latencyMs": 125.0,
        }
    ]


def test_request_context_middleware_generates_trace_id_when_missing():
    app = FastAPI()
    install_request_context_middleware(app, access_log_sink=lambda record: None)

    @app.get("/trace")
    async def trace(request: Request):
        return {"traceId": request.state.trace_id}

    response = TestClient(app).get("/trace")

    generated_trace_id = response.headers["X-Trace-Id"]
    assert response.json()["traceId"] == generated_trace_id
    assert UUID(generated_trace_id)


def test_health_checker_splits_dependency_statuses_and_overall_status():
    checker = HealthChecker(
        checks=[
            DependencyHealthCheck(
                name="app",
                check=lambda: HealthCheckResult.healthy(message="ready"),
            ),
            DependencyHealthCheck(
                name="vectorStore",
                check=lambda: HealthCheckResult.unhealthy(
                    message="milvus disconnected",
                    details={"host": "localhost", "port": 19530},
                ),
            ),
        ]
    )

    payload, status_code = checker.as_response()

    assert status_code == 503
    assert payload["status"] == "unhealthy"
    assert payload["dependencies"]["app"] == {
        "status": "healthy",
        "message": "ready",
        "details": {},
    }
    assert payload["dependencies"]["vectorStore"] == {
        "status": "unhealthy",
        "message": "milvus disconnected",
        "details": {"host": "localhost", "port": 19530},
    }


def test_health_router_uses_injected_checker_and_keeps_response_envelope():
    checker = HealthChecker(
        checks=[
            DependencyHealthCheck(
                name="sessionStore",
                check=lambda: HealthCheckResult.unhealthy("session backend unavailable"),
            )
        ]
    )
    app = FastAPI()
    app.include_router(create_health_router(checker=checker))

    response = TestClient(app).get("/health")

    assert response.status_code == 503
    assert response.json() == {
        "code": 503,
        "data": {
            "status": "unhealthy",
            "dependencies": {
                "sessionStore": {
                    "status": "unhealthy",
                    "message": "session backend unavailable",
                    "details": {},
                }
            },
        },
    }


def test_config_validation_accepts_valid_startup_settings():
    report = validate_settings(_settings())

    assert report.is_valid is True
    assert report.errors == []


def test_config_validation_rejects_placeholder_secret_and_invalid_chunking():
    report = validate_settings(
        _settings(
            dashscope_api_key="your-api-key-here",
            rag_top_k=0,
            chunk_max_size=100,
            chunk_overlap=100,
        )
    )

    assert report.is_valid is False
    assert [
        (issue.field, issue.message)
        for issue in report.errors
    ] == [
        ("DASHSCOPE_API_KEY", "must be set to a non-placeholder value"),
        ("RAG_TOP_K", "must be greater than or equal to 1"),
        ("CHUNK_OVERLAP", "must be less than CHUNK_MAX_SIZE"),
    ]


def test_config_validation_rejects_wildcard_cors_with_credentials():
    report = validate_settings(
        _settings(cors_allow_origins="*", cors_allow_credentials=True)
    )

    assert report.is_valid is False
    assert (
        "CORS_ALLOW_ORIGINS",
        "cannot be '*' when CORS_ALLOW_CREDENTIALS is true",
    ) in [(issue.field, issue.message) for issue in report.errors]


def test_config_validation_rejects_invalid_upload_security_settings():
    report = validate_settings(
        _settings(
            upload_allowed_extensions=" ",
            upload_max_bytes=0,
        )
    )

    assert report.is_valid is False
    assert (
        "UPLOAD_ALLOWED_EXTENSIONS",
        "must contain at least one extension",
    ) in [(issue.field, issue.message) for issue in report.errors]
    assert (
        "UPLOAD_MAX_BYTES",
        "must be greater than or equal to 1",
    ) in [(issue.field, issue.message) for issue in report.errors]


def test_config_validation_rejects_invalid_background_indexing_settings():
    report = validate_settings(
        _settings(
            indexing_execution_mode="invalid",
            indexing_queue_provider="unsupported",
            indexing_worker_poll_interval_seconds=0,
            indexing_worker_shutdown_timeout_seconds=0,
        )
    )

    assert report.is_valid is False
    issues = [(issue.field, issue.message) for issue in report.errors]
    assert ("INDEXING_EXECUTION_MODE", "unsupported mode: invalid") in issues
    assert ("INDEXING_QUEUE_PROVIDER", "unsupported provider: unsupported") in issues
    assert (
        "INDEXING_WORKER_POLL_INTERVAL_SECONDS",
        "must be greater than 0",
    ) in issues
    assert (
        "INDEXING_WORKER_SHUTDOWN_TIMEOUT_SECONDS",
        "must be greater than 0",
    ) in issues


def test_config_validation_rejects_unknown_deployment_environment():
    report = validate_settings(_settings(deployment_environment="qa"))

    assert report.is_valid is False
    issues = {(issue.field, issue.message) for issue in report.errors}
    assert ("DEPLOYMENT_ENVIRONMENT", "unsupported environment: qa") in issues


def test_config_validation_rejects_unsafe_production_defaults():
    report = validate_settings(
        _settings(
            deployment_environment="production",
            debug=True,
            cors_allow_origins="http://localhost:9900,https://rag.example.com",
            chat_provider="fake",
            embedding_provider="fake",
            vector_store_provider="fake",
            session_store_provider="memory",
            retrieval_audit_store_provider="memory",
            indexing_job_store_provider="memory",
            document_catalog_provider="memory",
            checkpoint_provider="memory",
            ingestion_provider="fake",
        )
    )

    assert report.is_valid is False
    issues = {(issue.field, issue.message) for issue in report.errors}
    assert ("DEBUG", "must be false when DEPLOYMENT_ENVIRONMENT=production") in issues
    assert (
        "CORS_ALLOW_ORIGINS",
        "must not include localhost, 127.0.0.1, or '*' in production",
    ) in issues
    assert ("CHAT_PROVIDER", "fake provider is not allowed in production") in issues
    assert ("EMBEDDING_PROVIDER", "fake provider is not allowed in production") in issues
    assert ("VECTOR_STORE_PROVIDER", "fake provider is not allowed in production") in issues
    assert ("INGESTION_PROVIDER", "fake provider is not allowed in production") in issues
    assert (
        "SESSION_STORE_PROVIDER",
        "memory provider is not allowed in production",
    ) in issues
    assert (
        "RETRIEVAL_AUDIT_STORE_PROVIDER",
        "memory provider is not allowed in production",
    ) in issues
    assert (
        "INDEXING_JOB_STORE_PROVIDER",
        "memory provider is not allowed in production",
    ) in issues
    assert (
        "DOCUMENT_CATALOG_PROVIDER",
        "memory provider is not allowed in production",
    ) in issues
    assert ("CHECKPOINT_PROVIDER", "memory provider is not allowed in production") in issues


def test_config_validation_accepts_hardened_production_settings():
    report = validate_settings(
        _settings(
            deployment_environment="production",
            debug=False,
            cors_allow_origins="https://rag.example.com",
            cors_allow_credentials=True,
            chat_provider="openai_compatible",
            embedding_provider="openai_compatible",
            vector_store_provider="milvus",
            session_store_provider="postgres",
            session_store_postgres_dsn="postgresql://rag:secret@db/ragqs",
            retrieval_audit_store_provider="postgres",
            retrieval_audit_postgres_dsn="postgresql://rag:secret@db/ragqs-audits",
            indexing_job_store_provider="postgres",
            indexing_job_store_postgres_dsn="postgresql://rag:secret@db/ragqs-indexing",
            document_catalog_provider="postgres",
            document_catalog_postgres_dsn="postgresql://rag:secret@db/ragqs-documents",
            checkpoint_provider="postgres",
            checkpoint_postgres_dsn="postgresql://rag:secret@db/ragqs-checkpoints",
            ingestion_provider="vector_index",
            openai_compatible_api_key="sk-compatible",
            openai_compatible_model="compatible-chat",
            openai_compatible_embedding_model="compatible-embedding",
        )
    )

    assert report.is_valid is True
    assert report.errors == []


def test_config_validation_cli_returns_nonzero_and_actionable_output():
    output = StringIO()

    exit_code = config_validation_main(
        [],
        settings=_settings(dashscope_api_key="placeholder"),
        output=output,
    )

    assert exit_code == 1
    assert "DASHSCOPE_API_KEY: must be set to a non-placeholder value" in output.getvalue()


def _settings(**overrides) -> Settings:
    values = {
        "dashscope_api_key": "sk-valid",
        "rag_top_k": 3,
        "chunk_max_size": 800,
        "chunk_overlap": 100,
        "port": 9900,
        "milvus_port": 19530,
        "milvus_timeout": 10000,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_parse_cors_origins_splits_comma_separated_values():
    assert parse_cors_origins("http://localhost:9900, https://rag.example.com ") == [
        "http://localhost:9900",
        "https://rag.example.com",
    ]


def test_build_cors_options_uses_explicit_origins_and_credentials_flag():
    options = build_cors_options(
        _settings(
            cors_allow_origins="http://localhost:9900,https://rag.example.com",
            cors_allow_credentials=True,
        )
    )

    assert options == {
        "allow_origins": ["http://localhost:9900", "https://rag.example.com"],
        "allow_credentials": True,
        "allow_methods": ["*"],
        "allow_headers": ["*"],
    }


def test_vector_database_compose_exposes_optional_ui_profile():
    compose = (ROOT / "vector-database.yml").read_text(encoding="utf-8")

    assert "profiles:" in compose
    assert "attu:" in compose
    assert "- ui" in compose


def test_vector_database_compose_allows_alternate_milvus_host_ports():
    compose = (ROOT / "vector-database.yml").read_text(encoding="utf-8")

    assert "${MILVUS_PORT:-19530}:19530" in compose
    assert "${MILVUS_HEALTH_PORT:-9091}:9091" in compose


def test_start_script_maps_docker_profile_to_compose_profile():
    script = (ROOT / "start.ps1").read_text(encoding="utf-8")

    assert "DockerProfile" in script
    assert '[ValidateSet("core", "ui")]' in script
    assert '"--profile", "ui"' in script


def test_start_script_checks_windows_excluded_milvus_ports():
    script = (ROOT / "start.ps1").read_text(encoding="utf-8")

    assert "Assert-MilvusHostPortAvailable" in script
    assert "netsh interface ipv4 show excludedportrange protocol=tcp" in script
    assert "Set MILVUS_PORT to an available port" in script


def test_main_uses_configured_cors_options():
    main_source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")

    assert "build_cors_options(config)" in main_source
    assert 'allow_origins=["*"]' not in main_source


def test_operations_docs_describe_docker_profiles():
    docs = (ROOT / "docs/operations.md").read_text(encoding="utf-8")

    for phrase in [
        "Docker Profiles",
        "-DockerProfile ui",
        "Attu",
        "core Milvus",
    ]:
        assert phrase in docs


def test_health_preflight_accepts_healthy_dependency_payload():
    from app.operations.health_preflight import validate_health_payload

    summary = validate_health_payload(
        {
            "code": 200,
            "data": {
                "status": "healthy",
                "dependencies": {
                    "app": {"status": "healthy"},
                    "modelProvider": {"status": "healthy"},
                    "embeddingProvider": {"status": "healthy"},
                    "vectorStore": {"status": "healthy"},
                    "sessionStore": {"status": "healthy"},
                },
            },
        },
        http_status=200,
    )

    assert summary.status == "healthy"
    assert summary.dependencies == [
        "app",
        "modelProvider",
        "embeddingProvider",
        "vectorStore",
        "sessionStore",
    ]


def test_health_preflight_rejects_unhealthy_dependencies():
    from app.operations.health_preflight import HealthPreflightError, validate_health_payload

    with pytest.raises(HealthPreflightError, match="vectorStore: disconnected"):
        validate_health_payload(
            {
                "code": 503,
                "data": {
                    "status": "unhealthy",
                    "dependencies": {
                        "app": {"status": "healthy"},
                        "modelProvider": {"status": "healthy"},
                        "embeddingProvider": {"status": "healthy"},
                        "vectorStore": {
                            "status": "unhealthy",
                            "message": "disconnected",
                        },
                        "sessionStore": {"status": "healthy"},
                    },
                },
            },
            http_status=503,
        )


def test_integration_smoke_requires_milvus_vector_store_provider():
    from app.operations.integration_smoke import run_integration_smoke

    report = run_integration_smoke(
        settings=_settings(vector_store_provider="fake"),
        milvus_probe=lambda settings: {"collections": 0},
    )

    assert report.ready is False
    assert ("VECTOR_STORE_PROVIDER", "must be milvus for integration smoke") in [
        (issue.field, issue.message) for issue in report.errors
    ]


def test_integration_smoke_reports_config_and_milvus_checks():
    from app.operations.integration_smoke import run_integration_smoke

    report = run_integration_smoke(
        settings=_settings(),
        milvus_probe=lambda settings: {"collections": 3, "host": settings.milvus_host},
    )

    assert report.ready is True
    assert [(check.name, check.status) for check in report.checks] == [
        ("configuration", "healthy"),
        ("milvus", "healthy"),
    ]
    assert report.checks[1].details["collections"] == 3
    assert report.errors == []


def test_integration_smoke_marks_milvus_failure_unready():
    from app.operations.integration_smoke import IntegrationSmokeError, run_integration_smoke

    def failing_probe(settings):
        raise IntegrationSmokeError("could not connect to milvus")

    report = run_integration_smoke(settings=_settings(), milvus_probe=failing_probe)

    assert report.ready is False
    assert ("MILVUS", "could not connect to milvus") in [
        (issue.field, issue.message) for issue in report.errors
    ]
    assert ("milvus", "unhealthy") in [(check.name, check.status) for check in report.checks]


def test_integration_smoke_can_validate_running_api_health():
    from app.operations.integration_smoke import run_integration_smoke

    calls = []

    def api_health_probe(url: str, timeout_seconds: float):
        calls.append((url, timeout_seconds))
        return {"status": "healthy", "dependencies": ["app", "vectorStore"]}

    report = run_integration_smoke(
        settings=_settings(),
        milvus_probe=lambda settings: {"collections": 0},
        api_url="http://127.0.0.1:9900/health",
        api_health_probe=api_health_probe,
        timeout_seconds=2.5,
    )

    assert report.ready is True
    assert calls == [("http://127.0.0.1:9900/health", 2.5)]
    assert [(check.name, check.status) for check in report.checks] == [
        ("configuration", "healthy"),
        ("milvus", "healthy"),
        ("apiHealth", "healthy"),
    ]


def test_integration_smoke_cli_outputs_json_report(capsys):
    from app.operations.integration_smoke import main

    exit_code = main(
        ["--json"],
        settings=_settings(),
        milvus_probe=lambda settings: {"collections": 1},
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"ready": true' in output
    assert '"milvus"' in output


def test_start_script_runs_health_preflight_for_existing_api():
    script = (ROOT / "start.ps1").read_text(encoding="utf-8")

    assert "Assert-RunningApiHealth" in script
    assert "app.operations.health_preflight" in script


def test_main_lifespan_controls_background_indexing_worker():
    main_source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")

    assert "indexing_execution_mode" in main_source
    assert "get_background_indexing_worker" in main_source
    assert "indexing_worker.start()" in main_source
    assert "indexing_worker.stop(" in main_source


def test_operations_docs_describe_dependency_health_preflight():
    docs = (ROOT / "docs/operations.md").read_text(encoding="utf-8")

    for phrase in [
        "Dependency Health Preflight",
        "scripts/check-api-health.ps1",
        "app.operations.health_preflight",
    ]:
        assert phrase in docs


def test_deployment_docs_describe_hosted_ci_workflow():
    docs = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")

    for phrase in [
        "GitHub Actions",
        ".github/workflows/ci.yml",
        "validate-baseline.ps1 -SkipPreflight",
        "run-postgres-smoke.ps1",
        "evaluation-report",
    ]:
        assert phrase in docs


def test_hosted_ci_workflow_runs_baseline_and_uploads_evaluation_report():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    for phrase in [
        "windows-latest",
        "actions/checkout@v4",
        "actions/setup-python@v5",
        "actions/setup-node@v4",
        "python -m venv .venv",
        "pip install -e \".[dev]\"",
        ".\\scripts\\validate-baseline.ps1 -SkipPreflight",
        ".\\scripts\\run-evaluation.ps1 -ReportPath artifacts\\evaluation-report.json",
        "actions/upload-artifact@v4",
        "artifacts/evaluation-report.json",
    ]:
        assert phrase in workflow


def test_integration_smoke_script_documents_no_stop_default():
    script = (ROOT / "scripts" / "run-integration-smoke.ps1").read_text(encoding="utf-8")

    for phrase in [
        "app.operations.integration_smoke",
        "ApiUrl",
        "Milvus",
        "does not stop Milvus",
    ]:
        assert phrase in script


def test_postgres_smoke_script_documents_non_destructive_checks():
    script = (ROOT / "scripts" / "run-postgres-smoke.ps1").read_text(encoding="utf-8")

    for phrase in [
        "app.operations.postgres_smoke",
        "RequireConfigured",
        "Postgres",
        "does not create, delete, start, stop, or restart databases",
    ]:
        assert phrase in script
