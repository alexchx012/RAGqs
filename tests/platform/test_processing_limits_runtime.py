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
        assert processor._xlsx_merged_cells_max == 11
        assert isinstance(documents, DocumentsService)
        assert documents._max_upload_bytes == 9
        assert documents._cleanup_max_attempts == 2
    finally:
        runtime.close()
