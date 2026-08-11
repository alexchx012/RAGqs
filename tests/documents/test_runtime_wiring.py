from __future__ import annotations

from app.documents.schema import documents_metadata
from app.documents.service import DocumentsDepartmentWorkCheckPort, DocumentsService
from app.identity.schema import identity_metadata
from app.platform.config import load_platform_settings
from app.platform.database import core_metadata
from app.platform.runtime import build_runtime
from app.usage.schema import usage_metadata


class _LifecyclePort:
    pass


def test_runtime_exposes_documents_service_and_department_work_adapter() -> None:
    settings = load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
        }
    )
    runtime = build_runtime(
        settings,
        adapters={
            "public_graph_source_trusted_consumers": {
                "indexing": {"indexer_1"},
                "public_graph": {"graph_1"},
            }
        },
    )
    try:
        engine = runtime.resolve("database_engine")
        core_metadata.create_all(engine)
        identity_metadata.create_all(engine)
        usage_metadata.create_all(engine)
        documents_metadata.create_all(engine)
        assert isinstance(runtime.resolve("documents_service"), DocumentsService)
        assert isinstance(runtime.resolve("department_work_check_port"), DocumentsDepartmentWorkCheckPort)
        assert runtime.resolve("documents_service")._public_graph_source_service is runtime.resolve(
            "public_graph_source_service"
        )
        assert runtime.resolve("outbox_lifecycle") is None
    finally:
        runtime.close()


def test_runtime_injects_scoped_document_redaction_capability_only_into_service() -> None:
    settings = load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
        }
    )
    lifecycle = _LifecyclePort()

    def issue_token(*, deletion_id: str, transaction_id: str) -> str:
        return f"token:{deletion_id}:{transaction_id}"

    runtime = build_runtime(
        settings,
        adapters={
            "document_lifecycle_port": lifecycle,
            "document_lifecycle_capability_provider": issue_token,
        },
    )
    try:
        service = runtime.resolve("documents_service")
        assert service._lifecycle_port is lifecycle
        assert service._capability_token_provider is issue_token
        assert runtime.resolve("document_lifecycle_capability_provider") is None
    finally:
        runtime.close()
