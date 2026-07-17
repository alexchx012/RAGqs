from types import SimpleNamespace

import pytest

from app.config import Settings
from app.operations.config_validation import validate_settings
from app.operations.health import create_default_health_checker
from app.operations.health_preflight import HealthPreflightError, validate_health_payload


def test_health_checker_reports_fake_provider_boundaries_without_real_dependencies():
    checker = create_default_health_checker(
        settings=_settings(
            chat_provider="fake",
            embedding_provider="fake",
            vector_store_provider="fake",
            session_store_provider="memory",
            retrieval_audit_store_provider="memory",
            checkpoint_provider="memory",
            indexing_queue_provider="memory",
            indexing_job_store_provider="memory",
            document_catalog_provider="memory",
        )
    )

    payload, status_code = checker.as_response()

    assert status_code == 200
    dependencies = payload["dependencies"]
    assert dependencies["modelProvider"]["details"]["provider"] == "fake"
    assert dependencies["embeddingProvider"]["details"]["provider"] == "fake"
    assert dependencies["vectorStore"]["details"]["provider"] == "fake"
    assert dependencies["sessionStore"]["details"]["provider"] == "memory"
    assert dependencies["checkpointStore"]["details"]["provider"] == "memory"
    assert dependencies["retrievalAuditStore"]["details"]["provider"] == "memory"
    assert dependencies["indexingQueue"]["details"]["provider"] == "memory"
    assert dependencies["indexingJobStore"]["details"]["provider"] == "memory"
    assert dependencies["documentCatalog"]["details"]["provider"] == "memory"


def test_health_checker_rejects_incomplete_openai_compatible_provider_configuration():
    checker = create_default_health_checker(
        settings=_settings(
            chat_provider="openai_compatible",
            embedding_provider="openai_compatible",
            openai_compatible_api_key="",
            chat_model="",
            openai_compatible_embedding_model="",
        ),
        milvus_manager=SimpleNamespace(health_check=lambda: True),
    )

    payload, status_code = checker.as_response()

    assert status_code == 503
    assert payload["dependencies"]["modelProvider"]["message"] == (
        "OPENAI_COMPATIBLE_API_KEY and CHAT_MODEL must be configured"
    )
    assert payload["dependencies"]["embeddingProvider"]["message"] == (
        "OPENAI_COMPATIBLE_API_KEY and OPENAI_COMPATIBLE_EMBEDDING_MODEL must be configured"
    )


def test_health_checker_rejects_env_example_dashscope_placeholder():
    checker = create_default_health_checker(
        settings=_settings(dashscope_api_key="your-dashscope-api-key"),
        milvus_manager=SimpleNamespace(health_check=lambda: True),
    )

    payload, status_code = checker.as_response()

    assert status_code == 503
    assert payload["dependencies"]["modelProvider"]["message"] == (
        "DASHSCOPE_API_KEY is not configured"
    )
    assert payload["dependencies"]["embeddingProvider"]["message"] == (
        "DASHSCOPE_API_KEY is not configured"
    )


def test_health_checker_uses_grouped_provider_selection_for_runtime_stores():
    settings = SimpleNamespace(
        providers=SimpleNamespace(
            chat="fake",
            embedding="fake",
            vector_store="fake",
            session_store="postgres",
            retrieval_audit_store="postgres",
            ingestion="fake",
            checkpoint="postgres",
        ),
        storage=SimpleNamespace(
            session_store_postgres_dsn="postgresql://rag:secret@db/sessions",
            retrieval_audit_postgres_dsn="postgresql://rag:secret@db/audits",
            checkpoint_postgres_dsn="postgresql://rag:secret@db/checkpoints",
            indexing_queue_provider="postgres",
            indexing_queue_postgres_dsn="postgresql://rag:secret@db/queue",
            indexing_job_store_provider="postgres",
            indexing_job_store_postgres_dsn="postgresql://rag:secret@db/jobs",
            document_catalog_provider="postgres",
            document_catalog_postgres_dsn="postgresql://rag:secret@db/catalog",
        ),
    )
    checker = create_default_health_checker(settings=settings)

    payload, status_code = checker.as_response()

    assert status_code == 200
    dependencies = payload["dependencies"]
    assert dependencies["sessionStore"]["details"]["provider"] == "postgres"
    assert dependencies["retrievalAuditStore"]["details"]["provider"] == "postgres"
    assert dependencies["checkpointStore"]["details"]["provider"] == "postgres"
    assert dependencies["indexingQueue"]["details"]["provider"] == "postgres"
    assert dependencies["indexingJobStore"]["details"]["provider"] == "postgres"
    assert dependencies["documentCatalog"]["details"]["provider"] == "postgres"


def test_health_preflight_requires_runtime_data_boundaries():
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
                    "checkpointStore": {"status": "healthy"},
                    "retrievalAuditStore": {"status": "healthy"},
                    "indexingQueue": {"status": "healthy"},
                    "indexingJobStore": {"status": "healthy"},
                    "documentCatalog": {"status": "healthy"},
                },
            },
        },
        http_status=200,
    )

    assert summary.dependencies == [
        "app",
        "modelProvider",
        "embeddingProvider",
        "vectorStore",
        "sessionStore",
        "checkpointStore",
        "retrievalAuditStore",
        "indexingQueue",
        "indexingJobStore",
        "documentCatalog",
    ]


def test_health_preflight_rejects_missing_runtime_data_boundary():
    with pytest.raises(HealthPreflightError, match="checkpointStore: missing"):
        validate_health_payload(
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


def test_production_validation_requires_auth_and_runtime_controls():
    report = validate_settings(
        Settings(
            _env_file=None,
            deployment_environment="production",
            debug=False,
            cors_allow_origins="https://rag.example.com",
            chat_provider="openai_compatible",
            embedding_provider="openai_compatible",
            vector_store_provider="milvus",
            session_store_provider="postgres",
            session_store_postgres_dsn="postgresql://rag:secret@db/ragqs",
            retrieval_audit_store_provider="postgres",
            retrieval_audit_postgres_dsn="postgresql://rag:secret@db/ragqs-audits",
            indexing_queue_provider="postgres",
            indexing_queue_postgres_dsn="postgresql://rag:secret@db/ragqs-queue",
            indexing_job_store_provider="postgres",
            indexing_job_store_postgres_dsn="postgresql://rag:secret@db/ragqs-indexing",
            document_catalog_provider="postgres",
            document_catalog_postgres_dsn="postgresql://rag:secret@db/ragqs-documents",
            checkpoint_provider="postgres",
            checkpoint_postgres_dsn="postgresql://rag:secret@db/ragqs-checkpoints",
            ingestion_provider="vector_index",
            openai_compatible_api_key="sk-compatible",
            chat_model="compatible-chat",
            openai_compatible_embedding_model="compatible-embedding",
            auth_enabled=False,
            runtime_controls_enabled=False,
        )
    )

    assert report.is_valid is False
    issues = {(issue.field, issue.message) for issue in report.errors}
    assert (
        "AUTH_ENABLED",
        "must be true when DEPLOYMENT_ENVIRONMENT=production",
    ) in issues
    assert (
        "RUNTIME_CONTROLS_ENABLED",
        "must be true when DEPLOYMENT_ENVIRONMENT=production",
    ) in issues


def _settings(**overrides):
    values = {
        "dashscope_api_key": "sk-valid",
        "chat_provider": "dashscope",
        "embedding_provider": "dashscope",
        "vector_store_provider": "milvus",
        "session_store_provider": "sqlite",
        "retrieval_audit_store_provider": "sqlite",
        "indexing_queue_provider": "sqlite",
        "indexing_job_store_provider": "sqlite",
        "document_catalog_provider": "sqlite",
        "checkpoint_provider": "sqlite",
        "session_store_sqlite_path": "data/sessions.sqlite3",
        "retrieval_audit_sqlite_path": "data/retrieval-audits.sqlite3",
        "indexing_queue_sqlite_path": "data/indexing-queue.sqlite3",
        "indexing_job_store_sqlite_path": "data/indexing-jobs.sqlite3",
        "document_catalog_sqlite_path": "data/document-catalog.sqlite3",
        "checkpoint_sqlite_path": "data/checkpoints.sqlite3",
        "openai_compatible_api_key": "",
        "chat_model": "test-chat-model",
        "openai_compatible_embedding_model": "",
        "milvus_host": "localhost",
        "milvus_port": 19530,
    }
    values.update(overrides)
    return SimpleNamespace(**values)
