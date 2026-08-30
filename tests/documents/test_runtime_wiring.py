from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select

import app.evaluation as evaluation_module
from app.documents.preview import ProcessingReceiptPreviewRenderer
from app.documents.read_models import DocumentsRetrievalVisibilityPort
from app.documents.schema import documents_metadata
from app.documents.service import DocumentsDepartmentWorkCheckPort, DocumentsService
from app.evaluation import HttpJudgeProvider
from app.identity.schema import identity_metadata
from app.indexing import (
    IndexingService,
)
from app.outbox.ports import DocumentNotificationRedactionCommand
from app.outbox.schema import outbox_metadata, outbox_redaction_receipt_table
from app.platform.config import PlatformConfigurationError, load_platform_settings
from app.platform.database import core_metadata
from app.platform.runtime import build_runtime
from app.usage.budget import BudgetEffortPolicy, BudgetMeterPolicy, BudgetMeterService
from app.usage.schema import usage_metadata


class _LifecyclePort:
    pass


class _ExplicitDenseWriter:
    provider_name = "configured-dense"

    def stage_chunks(self, *args, **kwargs):
        del args, kwargs

    def publish_staged(self, *args, **kwargs):
        del args, kwargs

    def discard_staged(self, *args, **kwargs):
        del args, kwargs

    def delete_document_version(self, *args, **kwargs):
        del args, kwargs
        return 0

    def delete_document(self, *args, **kwargs):
        del args, kwargs
        return 0

    def search(self, *args, **kwargs):
        del args, kwargs


class _ExplicitSparseProvider:
    provider_name = "configured-sparse"

    def stage_chunks(self, *args, **kwargs):
        del args, kwargs

    def publish_staged(self, *args, **kwargs):
        del args, kwargs

    def discard_staged(self, *args, **kwargs):
        del args, kwargs

    def delete_document_version(self, *args, **kwargs):
        del args, kwargs
        return 0

    def delete_document(self, *args, **kwargs):
        del args, kwargs
        return 0

    def search(self, *args, **kwargs):
        del args, kwargs


class _ExplicitReranker:
    def rerank(self, query, candidates, profile):
        del query, profile
        return tuple(candidates), None


class _ExplicitGraphExtractor:
    """Production 装配要求的显式 graph build extractor 测试替身。"""

    def estimate_primary_model_calls(self, snapshot):
        del snapshot
        return 0

    def extract(self, snapshot, session):
        del snapshot, session


class _ExplicitJudgeProvider:
    """Production 装配要求的显式 evaluation judge provider 测试替身。"""

    def preflight_probe(self) -> None:
        return None

    def judge(self, request):
        del request
        raise AssertionError("test judge must not be invoked during assembly")


def _production_adapters(engine):
    return {
        "database_engine": engine,
        "indexing_dense_writer": _ExplicitDenseWriter(),
        "indexing_sparse_provider": _ExplicitSparseProvider(),
        "indexing_reranker": _ExplicitReranker(),
        "indexing_token_counter": len,
        "indexing_image_ocr": lambda content, context: "ocr",
        "indexing_image_describer": lambda content, context: "description",
        "graph_build_extractor": _ExplicitGraphExtractor(),
        "generation_budget_meter": _budget_meter(engine),
    }


class _FixedClock:
    def now_utc(self, connection=None) -> datetime:
        del connection
        return datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


def _budget_meter(engine) -> BudgetMeterService:
    policy = BudgetMeterPolicy.production(
        efforts={
            effort: BudgetEffortPolicy(
                max_rag_calls=1,
                max_wall_seconds=20,
                max_total_tokens=12000,
                max_estimated_cost_amount=Decimal("1.0000000000"),
                candidate_document_limit=5,
            )
            for effort in ("quick", "think", "deep")
        },
        price_version_id="price-test",
        currency_code="USD",
        cost_estimator=lambda operation, tokens: Decimal("0.001"),
    )
    return BudgetMeterService(engine, _FixedClock(), policy)


def _production_settings(*, judge_base_url: str | None, judge_api_key: str | None):
    values = {
        "RAG_PLATFORM_PROFILE": "production",
        "RAG_DATABASE_URL": "postgresql+psycopg://app:secret@db/rag",
        "RAG_OBJECT_STORAGE_ENDPOINT": "https://objects.example.test",
        "RAG_OBJECT_STORAGE_BUCKET": "rag-prod",
        "RAG_PROVIDER_NAME": "openai-compatible",
        "RAG_PROVIDER_API_KEY": "provider-secret",
        "RAG_BUSINESS_TIMEZONE": "UTC",
        "RAG_AUTH_SECRET_KEY": "auth-secret-that-is-long-enough",
        "RAG_AUTH_ALLOWED_ORIGINS": "https://app.example.test",
        "RAG_AUTH_ADMIN_ROSTER": "admin",
        "RAG_BACKUP_TARGET_NAMESPACE": "ragqs-test-backups",
        "USER_DELETION_ARCHIVE_DIR": os.environ.get(
            "TEST_USER_DELETION_ARCHIVE_DIR", tempfile.mkdtemp(prefix="rag-archive-")
        ),
    }
    if judge_base_url is not None:
        values["RAG_EVALUATION_JUDGE_BASE_URL"] = judge_base_url
    if judge_api_key is not None:
        values["RAG_EVALUATION_JUDGE_API_KEY"] = judge_api_key
    return load_platform_settings(values)


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
        core_metadata.create_all(engine)

        documents_metadata.create_all(engine)
        assert isinstance(runtime.resolve("documents_service"), DocumentsService)
        assert isinstance(
            runtime.resolve("department_work_check_port"), DocumentsDepartmentWorkCheckPort
        )
        assert runtime.resolve("documents_service")._public_graph_source_service is runtime.resolve(
            "public_graph_source_service"
        )
        assert isinstance(runtime.resolve("indexing_service"), IndexingService)
        assert runtime.resolve("indexing_service").retrieval._identity is runtime.resolve(
            "identity_access"
        )
        assert isinstance(
            runtime.resolve("indexing_visibility_facts"), DocumentsRetrievalVisibilityPort
        )
        assert runtime.resolve("documents_service")._indexing_handoff_port is runtime.resolve(
            "indexing_service"
        )
        assert isinstance(
            runtime.resolve("document_preview_renderer"), ProcessingReceiptPreviewRenderer
        )
        assert runtime.resolve("documents_service")._preview_renderer is runtime.resolve(
            "document_preview_renderer"
        )
        assert runtime.resolve(
            "documents_service"
        )._message_citation_preview_port is runtime.resolve("message_citation_preview_port")
        assert runtime.resolve("outbox_lifecycle") is None
    finally:
        runtime.close()


def test_runtime_wires_indexing_processor_ports_and_object_store() -> None:
    settings = load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
        }
    )

    def mineru(content: bytes) -> dict[str, object]:
        del content
        return {"text": "parsed"}

    def describe(content: bytes, context: dict[str, object]) -> str:
        del content, context
        return "description"

    def ocr(content: bytes, context: dict[str, object]) -> str:
        del content, context
        return "ocr"

    runtime = build_runtime(
        settings,
        adapters={
            "indexing_mineru": mineru,
            "indexing_image_describer": describe,
            "indexing_image_ocr": ocr,
        },
    )
    try:
        service = runtime.resolve("indexing_service")
        assert service.processor._mineru is mineru
        assert service.processor._image_describer is describe
        assert service.processor._image_ocr is ocr
        assert service._object_store is runtime.resolve("object_store")
    finally:
        runtime.close()


def test_production_runtime_requires_explicit_retrieval_backends() -> None:
    settings = load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "production",
            "RAG_DATABASE_URL": "postgresql+psycopg://app:secret@db/rag",
            "RAG_OBJECT_STORAGE_ENDPOINT": "https://objects.example.test",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-prod",
            "RAG_PROVIDER_NAME": "openai-compatible",
            "RAG_PROVIDER_API_KEY": "provider-secret",
            "RAG_EVALUATION_JUDGE_BASE_URL": "https://judge.example.test/v1",
            "RAG_EVALUATION_JUDGE_API_KEY": "judge-secret",
            "RAG_BUSINESS_TIMEZONE": "UTC",
            "RAG_AUTH_SECRET_KEY": "auth-secret-that-is-long-enough",
            "RAG_AUTH_ALLOWED_ORIGINS": "https://app.example.test",
            "RAG_AUTH_ADMIN_ROSTER": "admin",
            "RAG_BACKUP_TARGET_NAMESPACE": "ragqs-test-backups",
            "USER_DELETION_ARCHIVE_DIR": tempfile.mkdtemp(prefix="rag-archive-"),
        }
    )

    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    try:
        build_runtime(settings, adapters={"database_engine": engine})
    except RuntimeError as error:
        assert "indexing" in str(error)
    else:
        raise AssertionError("production runtime accepted implicit indexing test adapters")

    runtime = build_runtime(
        settings,
        adapters={
            "database_engine": engine,
            "indexing_dense_writer": _ExplicitDenseWriter(),
            "indexing_sparse_provider": _ExplicitSparseProvider(),
            "indexing_reranker": _ExplicitReranker(),
            "indexing_token_counter": len,
            "indexing_image_ocr": lambda content, context: "ocr",
            "indexing_image_describer": lambda content, context: "description",
            "graph_build_extractor": _ExplicitGraphExtractor(),
            "judge_provider": _ExplicitJudgeProvider(),
            "generation_budget_meter": _budget_meter(engine),
        },
    )
    runtime.close()


def test_production_runtime_auto_assembles_configured_http_judge(monkeypatch) -> None:
    settings = _production_settings(
        judge_base_url="https://judge.example.test/v1",
        judge_api_key="judge-api-secret",
    )
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    preflight_providers = []

    def verify_startup(self) -> None:
        preflight_providers.append(self._provider)

    monkeypatch.setattr(evaluation_module.JudgePreflight, "verify_startup", verify_startup)
    runtime = build_runtime(settings, adapters=_production_adapters(engine))
    try:
        judge_provider = runtime.resolve("judge_provider")

        assert isinstance(judge_provider, HttpJudgeProvider)
        assert judge_provider._usage_submission is runtime.resolve("evaluation_usage_submission")
        assert judge_provider._configuration.provider == settings.evaluation.judge_provider
        assert judge_provider._configuration.model == settings.evaluation.judge_model
        assert judge_provider._configuration.mode == settings.evaluation.judge_mode
        assert (
            judge_provider._configuration.credential_ref == settings.evaluation.judge_credential_ref
        )
        assert preflight_providers == [judge_provider]
    finally:
        runtime.close()


def test_production_runtime_closes_auto_assembled_judge_when_preflight_fails(monkeypatch) -> None:
    settings = _production_settings(
        judge_base_url="https://judge.example.test/v1",
        judge_api_key="judge-api-secret",
    )
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    preflight_providers = []

    def fail_preflight(self) -> None:
        preflight_providers.append(self._provider)
        raise RuntimeError("preflight failed")

    monkeypatch.setattr(evaluation_module.JudgePreflight, "verify_startup", fail_preflight)

    try:
        build_runtime(settings, adapters=_production_adapters(engine))
    except RuntimeError as error:
        assert str(error) == "preflight failed"
    else:
        raise AssertionError("configured judge preflight unexpectedly succeeded")

    assert len(preflight_providers) == 1
    assert isinstance(preflight_providers[0], HttpJudgeProvider)
    assert preflight_providers[0]._client.is_closed


def test_production_settings_reject_missing_judge_configuration() -> None:
    # A1 fail-closed: missing judge configuration rejects startup at load time
    # instead of degrading to an unavailable judge provider.
    with pytest.raises(
        PlatformConfigurationError,
        match="production evaluation judge configuration is incomplete",
    ):
        load_platform_settings(_production_settings(judge_base_url=None, judge_api_key=None))


def test_runtime_injects_document_lifecycle_port_into_service() -> None:
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

    runtime = build_runtime(
        settings,
        adapters={"document_lifecycle_port": lifecycle},
    )
    try:
        service = runtime.resolve("documents_service")
        assert service._lifecycle_port is lifecycle
    finally:
        runtime.close()


def test_runtime_wires_default_document_lifecycle_gateway() -> None:
    settings = load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
        }
    )
    runtime = build_runtime(settings)
    try:
        engine = runtime.resolve("database_engine")
        outbox_metadata.create_all(engine)
        service = runtime.resolve("documents_service")

        assert service._lifecycle_port is not None
        with engine.begin() as connection:
            receipt = service._lifecycle_port.redact_document_notifications(
                DocumentNotificationRedactionCommand(
                    operation_id="op_runtime_document_redaction",
                    caller_principal="user_1",
                    deletion_id="deletion_1",
                    document_id="document_1",
                    document_version_ids=("version_1",),
                    reason="document_pending_delete",
                    transaction_id="tx_1",
                    mode="inline",
                    canonical_input_fingerprint="not-authoritative",
                ),
                connection=connection,
            )

        assert receipt.state == "completed"
        with engine.connect() as connection:
            receipt_row = connection.execute(
                select(outbox_redaction_receipt_table.c.operation_id)
            ).scalar_one()
        assert receipt_row == "op_runtime_document_redaction"
    finally:
        runtime.close()
