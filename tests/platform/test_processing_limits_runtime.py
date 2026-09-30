from __future__ import annotations

from app.documents.service import DocumentsService
from app.indexing import ContentProcessor
from app.platform.config import load_platform_settings
from app.platform.runtime import build_runtime


def test_runtime_injects_documents_and_processing_limits() -> None:
    settings = load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
            "RAG_DOCUMENTS_UPLOAD_MAX_BYTES": "9",
            "RAG_DOCUMENTS_CLEANUP_MAX_ATTEMPTS": "2",
            "RAG_INDEX_TEXT_CHUNK_MAX_CHARS": "7",
            "RAG_INDEX_XLSX_MERGED_CELLS_MAX": "11",
        }
    )
    runtime = build_runtime(settings)
    try:
        processor = runtime.resolve("indexing_processor")
        documents = runtime.resolve("documents_service")

        assert isinstance(processor, ContentProcessor)
        assert processor._text_chunk_max_chars == 7
        # 未设置 env 时取父子切块的配置默认值：子块目标 640、上限 1600、父块 2560。
        assert processor._text_chunk_target_chars == 640
        assert processor._text_child_chunk_max_chars == 1600
        assert processor._text_parent_chunk_target_chars == 2560
        assert processor._xlsx_merged_cells_max == 11
        assert isinstance(documents, DocumentsService)
        assert documents._max_upload_bytes == 9
        assert documents._cleanup_max_attempts == 2
    finally:
        runtime.close()


def test_runtime_wires_chunk_packing_target_from_env() -> None:
    settings = load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
            "RAG_INDEX_TEXT_CHUNK_TARGET_CHARS": "9",
            "RAG_INDEX_TEXT_CHILD_CHUNK_MAX_CHARS": "12",
            "RAG_INDEX_TEXT_PARENT_CHUNK_TARGET_CHARS": "40",
        }
    )
    runtime = build_runtime(settings)
    try:
        processor = runtime.resolve("indexing_processor")

        assert isinstance(processor, ContentProcessor)
        assert processor._text_chunk_target_chars == 9
        assert processor._text_child_chunk_max_chars == 12
        assert processor._text_parent_chunk_target_chars == 40
    finally:
        runtime.close()
