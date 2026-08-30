"""fix-indexing-parsing：OCR 阈值、图片页数折算、稀疏精确匹配抽样、provider 占位清理。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint

from alembic import command
from alembic.config import Config
from app.chat.schema import chat_generation_execution_table, chat_generation_table
from app.documents.indexing import IndexStagingRequest
from app.documents.service import _metered_pages
from app.indexing import (
    ContentProcessor,
    DocumentVisibilityFact,
    GenerationManager,
    InMemoryIndexWriter,
    InMemorySparseIndexProvider,
    RetrievalProfile,
    RetrievalScope,
    RetrievalService,
)
from app.indexing.models import IndexChunk
from app.indexing.retrieval import SPARSE_EXACT_MATCH_ROUTE


def _request(
    *, generation: str = "generation_initial", attempt_id: str = "attempt_1"
) -> IndexStagingRequest:
    return IndexStagingRequest(
        job_id="job_1",
        attempt_id=attempt_id,
        fencing_token=1,
        publication_id="publication_1",
        document_id="document_1",
        document_version_id="version_1",
        space_id="space_1",
        operation="initial",
        base_active_version_id=None,
        expected_generation_id=generation,
        index_revision_at_start=0,
        object_manifest_ref="manifest_1",
        processing_config_snapshot={},
        authorization_fence={"actor_id": "user_1"},
        input_manifest_hash="manifest_hash_1",
        processing_profile_version="profile_1",
    )


# --- A1: OCR 置信度阈值 ---


def _pdf_output(processor: ContentProcessor, confidence: float) -> dict:
    output = ContentProcessor(
        mineru=lambda content: {
            "text": "# First\nfirst\n\n# Second\nsecond",
            "page_count": 2,
            "ocr_confidence": confidence,
        },
        ocr_confidence_threshold=processor._ocr_confidence_threshold,
    ).process(
        _request(),
        b"pdf",
        media_kind="application/pdf",
        content_manifest_id="manifest_1",
        content_manifest_hash="manifest_hash_1",
    )
    return dict(output.receipt.processing_summary["ocr"])


def test_ocr_threshold_defaults_to_ninety_and_flags_low_confidence() -> None:
    assert ContentProcessor()._ocr_confidence_threshold == 0.9
    ocr = _pdf_output(ContentProcessor(), 0.5)
    assert ocr["low_confidence"] is True
    assert ocr["fact"] == {"confidence": 0.5, "page": 1, "region": []}
    assert ocr["threshold"] == 0.9


def test_ocr_threshold_is_configurable() -> None:
    processor = ContentProcessor(ocr_confidence_threshold=0.5)
    below = _pdf_output(processor, 0.6)
    above = _pdf_output(processor, 0.4)
    assert below["low_confidence"] is False
    assert below["threshold"] == 0.5
    assert above["low_confidence"] is True


def test_ocr_threshold_settings_default_and_env_override() -> None:
    from app.platform.config import load_platform_settings

    base = {
        "RAG_PLATFORM_PROFILE": "development",
        "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
        "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
        "RAG_PROVIDER_NAME": "fake",
        "RAG_BUSINESS_TIMEZONE": "Asia/Shanghai",
    }
    default_settings = load_platform_settings(environ=dict(base))
    assert default_settings.index.ocr_confidence_threshold == 0.9

    overridden = load_platform_settings(
        environ={**base, "RAG_INDEX_OCR_CONFIDENCE_THRESHOLD": "0.75"}
    )
    assert overridden.index.ocr_confidence_threshold == 0.75


# --- A2: 图片 1 张 = 1 页的计量折算 ---


def test_metered_pages_folds_images_in_at_one_page_each() -> None:
    # 单图：page_count 0 + image_count 1 → 1 页。
    assert _metered_pages(page_count=0, image_count=1) == 1
    # 多图：3 张图片 → 3 页。
    assert _metered_pages(page_count=0, image_count=3) == 3
    # 混合文档：2 页文本 + 2 张图片 → 4 页。
    assert _metered_pages(page_count=2, image_count=2) == 4


def test_metered_pages_ignores_invalid_counts() -> None:
    assert _metered_pages(page_count=True, image_count="2") == 0
    assert _metered_pages(page_count=-1, image_count=-5) == 0
    assert _metered_pages(page_count=None, image_count=None) == 0


def test_publication_quota_debit_folds_image_count_into_pages() -> None:
    from app.documents.service import DocumentsService

    class _Calendar:
        def lock_or_verify(self, connection):
            del connection
            return object()

    class _Quota:
        def __init__(self) -> None:
            self.calendar = _Calendar()
            self.recorded = []

        def record(self, connection, **values) -> str:
            del connection
            self.recorded.append(values)
            return "debit_1"

    quota = _Quota()
    service = DocumentsService.__new__(DocumentsService)
    service._quota_service = quota
    result = service._record_publication_quota(
        None,
        job={
            "id": "job_1",
            "created_by_user_id": "user_1",
            "quota_role_snapshot": "user",
            "quota_department_id_snapshot": None,
            "quota_exempt_reason": None,
            "replay_generation": 0,
        },
        publication={"id": "publication_1"},
        document={"space_id": "space_1"},
        receipt={
            "page_count": 2,
            "processing_summary": {
                "page_count": 2,
                "image_count": 3,
                "processing_list": {
                    "processing_list_id": "processing_list_1",
                    "frozen": True,
                },
            },
        },
        published_at=datetime(2026, 8, 22, tzinfo=UTC),
    )
    assert result["pages"] == 5
    assert result["processing_list_id"] == "processing_list_1"
    assert result["quota_debit_id"] == "debit_1"
    assert quota.recorded[-1]["pages"] == 5


def test_publication_quota_debit_rejects_zero_pages_for_image_only_receipt() -> None:
    from app.documents.service import DocumentsService

    service = DocumentsService.__new__(DocumentsService)
    service._quota_service = None
    assert (
        service._record_publication_quota(
            None,
            job={
                "id": "job_1",
                "created_by_user_id": "user_1",
                "quota_role_snapshot": "user",
                "quota_department_id_snapshot": None,
                "quota_exempt_reason": None,
                "replay_generation": 0,
            },
            publication={"id": "publication_1"},
            document={"space_id": "space_1"},
            receipt={"processing_summary": {"page_count": 0, "image_count": 0}},
            published_at=datetime(2026, 8, 22, tzinfo=UTC),
        )
        is None
    )


# --- A3: 稀疏检索精确匹配抽样 ---


class _Metrics:
    def __init__(self, rate: float = 1.0, *, fail: bool = False) -> None:
        self.success_sample_rate = rate
        self.samples: list[object] = []
        self._fail = fail

    def record(self, sample) -> None:
        if self._fail:
            raise RuntimeError("metrics unavailable")
        self.samples.append(sample)


def _exact_chunk(chunk_id: str, text: str) -> IndexChunk:
    return IndexChunk(
        chunk_id=chunk_id,
        generation_id="generation_initial",
        publication_id="publication_1",
        document_id=f"document_{chunk_id}",
        document_version_id="version_1",
        space_id="space_1",
        text=text,
        embedding_text=text,
        locator={},
        snippet=None,
        media_kind="text/plain",
        manifest_hash="manifest_1",
    )


def _facts(candidate: IndexChunk, principal: object) -> DocumentVisibilityFact:
    del principal
    return DocumentVisibilityFact(
        candidate.document_id,
        candidate.space_id,
        "active",
        candidate.document_version_id,
        candidate.publication_id,
        "active",
        candidate.manifest_hash,
        True,
    )


def _publish(provider: InMemoryIndexWriter, chunk: IndexChunk, attempt_id: str) -> None:
    provider.stage_chunks(
        attempt_id,
        chunk.publication_id,
        chunk.document_id,
        chunk.document_version_id,
        [chunk],
    )
    provider.publish_staged(attempt_id, chunk.publication_id)


def _search(metrics) -> tuple:
    sparse = InMemorySparseIndexProvider(provider_name="sparse")
    _publish(sparse, _exact_chunk("match", "RAGQS-1001"), "attempt_1")
    _publish(sparse, _exact_chunk("partial", "RAGQS-1001 appendix notes"), "attempt_2")
    service = RetrievalService(
        GenerationManager(),
        [sparse],
        identity_access=lambda principal: RetrievalScope(frozenset({"space_1"})),
        visibility_facts=_facts,
        exact_match_metrics=metrics,
    )
    result = service.search(
        "ragqs-1001",
        principal="user_1",
        profile=RetrievalProfile(top_k=2, candidate_limit=2, retrieval_context_items_per_space=2),
    )
    return result, metrics.samples


def test_sparse_exact_match_is_sampled_into_observability_read_path() -> None:
    result, samples = _search(_Metrics(rate=1.0))
    # 检索结果不受抽样影响。
    assert {hit.chunk.chunk_id for hit in result.hits} == {"match", "partial"}
    sampled = [s for s in samples if s.route_template == SPARSE_EXACT_MATCH_ROUTE]
    assert len(sampled) == 1
    sample = sampled[0]
    assert sample.sample_weight == 1.0


def test_sparse_exact_match_sampling_skips_when_rate_zero() -> None:
    _result, samples = _search(_Metrics(rate=0.0))
    assert [s for s in samples if s.route_template == SPARSE_EXACT_MATCH_ROUTE] == []


def test_sparse_exact_match_sampling_never_breaks_retrieval() -> None:
    result, samples = _search(_Metrics(rate=1.0, fail=True))
    assert len(result.hits) == 2
    assert samples == []


# --- A4: provider_reconciling 占位清理 ---


def test_execution_status_constraint_drops_reconciliation_placeholder() -> None:
    constraints = {
        constraint.name: str(constraint.sqltext)
        for constraint in chat_generation_execution_table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    text = constraints["ck_chat_generation_execution_status"]
    # 现行契约：provider_reconciling 是合法执行状态（worker 恢复流权威实现）。
    assert "'provider_reconciling'" in text
    assert "'expired'" in text


def test_generation_schema_has_no_reconciliation_state_columns() -> None:
    assert "provider_reconciliation_state" not in {
        column.name for column in chat_generation_table.columns
    }
    assert "provider_reconciliation_state" not in {
        column.name for column in chat_generation_execution_table.columns
    }


def test_worker_recovery_row_has_no_reconciliation_state() -> None:
    import inspect

    from app.chat import worker

    source = inspect.getsource(worker)
    # 占位列已被移除；provider_reconciling 状态本身是现行恢复流的一部分。
    assert "provider_reconciliation_state" not in source


def test_migrations_head_rejects_reconciliation_placeholder_status(
    tmp_path: Path,
) -> None:
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.exc import IntegrityError

    database_url = f"sqlite:///{tmp_path / 'chat-reconcile.sqlite3'}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    try:
        columns = {
            item["name"] for item in inspect(engine).get_columns("chat_generation_execution")
        }
        assert "provider_reconciliation_state" not in columns
        with engine.begin() as connection:
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        "INSERT INTO chat_generation_execution ("
                        "execution_id, generation_id, execution_attempt_number, status, "
                        "lease_owner, lease_expires_at_utc, heartbeat_at_utc, fencing_token, "
                        "checkpoint_version, checkpoint_json, next_attempt_at_utc, "
                        "last_error_classification, created_at_utc, updated_at_utc) "
                        "VALUES ('exec_1', 'gen_1', 1, 'provider_reconciling', NULL, NULL, "
                        "NULL, 1, 0, NULL, NULL, NULL, '2026-08-22T00:00:00+00:00', "
                        "'2026-08-22T00:00:00+00:00')"
                    )
                )
    finally:
        engine.dispose()
