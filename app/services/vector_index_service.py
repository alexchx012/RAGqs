"""向量索引服务"""

from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from app.config import config
from app.ingestion import (
    DocumentLoaderRegistry,
    DocumentMetadataNormalizer,
    IndexingJob,
    IndexingJobStatus,
    IndexingJobStore,
    InMemoryIndexingJobStore,
    PostgresIndexingJobStore,
    SQLiteIndexingJobStore,
)
from app.knowledge.catalog import (
    DEFAULT_SPACE_ID,
    InMemoryKnowledgeCatalog,
    PostgresKnowledgeCatalog,
    SQLiteKnowledgeCatalog,
)
from app.services.document_splitter_service import document_splitter_service
from app.services.vector_store_manager import vector_store_manager


class IndexingResult:
    def __init__(self):
        self.success = False
        self.directory_path = ""
        self.total_files = 0
        self.success_count = 0
        self.fail_count = 0
        self.start_time: datetime | None = None
        self.end_time: datetime | None = None
        self.error_message = ""
        self.failed_files: dict[str, str] = {}
        self.jobs: list[IndexingJob] = []

    def to_dict(self) -> dict[str, Any]:
        duration_ms = 0
        if self.start_time and self.end_time:
            duration_ms = int((self.end_time - self.start_time).total_seconds() * 1000)
        return {
            "success": self.success,
            "directory_path": self.directory_path,
            "total_files": self.total_files,
            "success_count": self.success_count,
            "fail_count": self.fail_count,
            "duration_ms": duration_ms,
            "failed_files": self.failed_files,
            "jobs": [_serialize_indexing_job(job) for job in self.jobs],
        }


class VectorIndexService:
    """向量索引服务"""

    def __init__(
        self,
        upload_path: str = "./uploads",
        document_splitter=None,
        vector_store=None,
        loader_registry: DocumentLoaderRegistry | None = None,
        metadata_normalizer: DocumentMetadataNormalizer | None = None,
        job_store: IndexingJobStore | None = None,
        document_catalog: InMemoryKnowledgeCatalog | None = None,
        settings=None,
    ):
        self.settings = settings or config
        self.upload_path = upload_path
        self.document_splitter = document_splitter or document_splitter_service
        self.vector_store = vector_store or vector_store_manager
        self.loader_registry = loader_registry or DocumentLoaderRegistry.default()
        self.metadata_normalizer = metadata_normalizer or DocumentMetadataNormalizer()
        self.job_store = job_store or _build_default_job_store(self.settings)
        self.document_catalog = document_catalog or _build_default_document_catalog(self.settings)

    def index_directory(
        self,
        directory_path: str | None = None,
        *,
        space_id: str = DEFAULT_SPACE_ID,
    ) -> IndexingResult:
        result = IndexingResult()
        result.start_time = datetime.now()
        try:
            target_path = directory_path if directory_path else self.upload_path
            dir_path = Path(target_path).resolve()
            if not dir_path.exists() or not dir_path.is_dir():
                raise ValueError(f"目录不存在: {target_path}")
            result.directory_path = str(dir_path)
            files = sorted(list(dir_path.glob("*.txt")) + list(dir_path.glob("*.md")))
            if not files:
                result.total_files = 0
                result.success = True
                result.end_time = datetime.now()
                return result
            result.total_files = len(files)
            for file_path in files:
                normalized_file_path = file_path.resolve().as_posix()
                try:
                    job = self.index_single_file(str(file_path), space_id=space_id)
                    result.jobs.append(job)
                    result.success_count += 1
                except Exception as e:
                    result.fail_count += 1
                    result.failed_files[normalized_file_path] = str(e)
                    failed_job = self._latest_job_for_source(normalized_file_path)
                    if failed_job is not None:
                        result.jobs.append(failed_job)
            result.success = result.fail_count == 0
            result.end_time = datetime.now()
            return result
        except Exception as e:
            result.success = False
            result.error_message = str(e)
            result.end_time = datetime.now()
            return result

    def create_pending_indexing_job(
        self,
        file_path: str,
        *,
        space_id: str = DEFAULT_SPACE_ID,
    ) -> IndexingJob:
        _, _, normalized_path, document_metadata = self._load_document_metadata(
            file_path,
            space_id=space_id,
        )
        self.document_catalog.ensure_space(document_metadata["space_id"])
        job = IndexingJob.create(
            document_id=document_metadata["document_id"],
            source_path=normalized_path,
            space_id=document_metadata["space_id"],
        )
        self.job_store.save(job)
        return job

    def index_single_file(self, file_path: str, *, space_id: str = DEFAULT_SPACE_ID) -> IndexingJob:
        job = self.create_pending_indexing_job(file_path, space_id=space_id)
        return self.run_indexing_job(job.job_id)

    def run_indexing_job(self, job_id: str) -> IndexingJob:
        job = self.get_indexing_job(job_id)
        if job is None:
            raise ValueError(f"索引任务不存在: {job_id}")
        if job.status is not IndexingJobStatus.PENDING:
            raise ValueError(f"索引任务状态不可执行: {job.status.value}")

        logger.info(f"开始索引文件: {job.source_path}")
        job.start()
        self.job_store.save(job)

        try:
            _, content, normalized_path, document_metadata = self._load_document_metadata(
                job.source_path,
                space_id=job.space_id,
            )
            if document_metadata["document_id"] != job.document_id:
                raise ValueError("索引任务 document_id 与文件元数据不匹配")

            self._delete_existing_document_chunks(
                document_id=document_metadata["document_id"],
                source_path=normalized_path,
            )
            documents = self.document_splitter.split_document(content, normalized_path)
            for index, document in enumerate(documents):
                headings = {
                    key: document.metadata[key]
                    for key in ("h1", "h2", "h3", "h4")
                    if document.metadata.get(key)
                }
                document.metadata = self.metadata_normalizer.chunk_metadata(
                    document_metadata=document_metadata,
                    chunk_index=index,
                    headings=headings,
                )

            if documents:
                self.vector_store.add_documents(documents)
                logger.info(f"文件索引完成: {job.source_path}, 共 {len(documents)} 个分片")

            job.complete(total_chunks=len(documents), indexed_chunks=len(documents))
            self.job_store.save(job)
            self.document_catalog.upsert_from_job(job)
            return job
        except Exception as e:
            job.complete(total_chunks=0, indexed_chunks=0, errors=[str(e)])
            self.job_store.save(job)
            self.document_catalog.upsert_from_job(job)
            raise

    def _load_document_metadata(
        self,
        file_path: str,
        *,
        space_id: str,
    ) -> tuple[Path, str, str, dict[str, Any]]:
        path = Path(file_path).resolve()
        if not path.exists() or not path.is_file():
            raise ValueError(f"文件不存在: {file_path}")
        loaded_documents = self.loader_registry.load(path)
        content = "\n\n".join(document.page_content for document in loaded_documents)
        loaded_metadata = loaded_documents[0].metadata if loaded_documents else {}
        normalized_path = loaded_metadata.get("source_path", path.as_posix())
        document_metadata = self.metadata_normalizer.document_metadata(
            normalized_path,
            content,
            space_id=space_id,
        )
        return path, content, normalized_path, document_metadata

    def get_indexing_job(self, job_id: str) -> IndexingJob | None:
        return self.job_store.get(job_id)

    def list_indexing_jobs(
        self,
        *,
        document_id: str | None = None,
        source_path: str | None = None,
        status: str | None = None,
    ) -> list[IndexingJob]:
        return self.job_store.list(document_id=document_id, source_path=source_path, status=status)

    def retry_indexing_job(self, job_id: str) -> IndexingJob:
        job = self.get_indexing_job(job_id)
        if job is None:
            raise ValueError(f"索引任务不存在: {job_id}")
        if job.status not in {IndexingJobStatus.FAILED, IndexingJobStatus.PARTIAL}:
            raise ValueError(f"索引任务状态不可重试: {job.status.value}")
        return self.index_single_file(job.source_path, space_id=job.space_id)

    def list_knowledge_spaces(self):
        return self.document_catalog.list_spaces()

    def list_documents(self, space_id: str = DEFAULT_SPACE_ID):
        return self.document_catalog.list_documents(space_id=space_id)

    def get_document(self, space_id: str, document_id: str):
        return self.document_catalog.get_document(space_id=space_id, document_id=document_id)

    def delete_document(self, space_id: str, document_id: str):
        record = self.document_catalog.get_document(space_id=space_id, document_id=document_id)
        if record is None:
            raise ValueError(f"文档不存在: {space_id}/{document_id}")
        self._delete_existing_document_chunks(
            document_id=document_id,
            source_path=record.source_path,
        )
        return self.document_catalog.mark_deleted(space_id=space_id, document_id=document_id)

    def rebuild_document(self, space_id: str, document_id: str) -> IndexingJob:
        record = self.document_catalog.get_document(space_id=space_id, document_id=document_id)
        if record is None:
            raise ValueError(f"文档不存在: {space_id}/{document_id}")
        return self.index_single_file(record.source_path, space_id=space_id)

    def _latest_job_for_source(self, source_path: str) -> IndexingJob | None:
        jobs = self.job_store.list(source_path=source_path)
        return jobs[-1] if jobs else None

    def _delete_existing_document_chunks(self, *, document_id: str, source_path: str) -> int:
        deleted_by_document_id = self.vector_store.delete_by_document_id(document_id)
        deleted_by_source = self.vector_store.delete_by_source(source_path)
        return deleted_by_document_id + deleted_by_source


def _build_default_job_store(settings) -> IndexingJobStore:
    provider = _settings_group_value(
        settings,
        "storage",
        "indexing_job_store_provider",
        "sqlite",
    )
    normalized_provider = str(provider).strip().lower().replace("-", "_")
    if normalized_provider == "sqlite":
        return SQLiteIndexingJobStore(
            _settings_group_value(
                settings,
                "storage",
                "indexing_job_store_sqlite_path",
                "data/indexing-jobs.sqlite3",
            )
        )
    if normalized_provider == "postgres":
        return PostgresIndexingJobStore(
            _settings_group_value(
                settings,
                "storage",
                "indexing_job_store_postgres_dsn",
                "",
            )
        )
    return InMemoryIndexingJobStore()


def _build_default_document_catalog(settings):
    provider = _settings_group_value(
        settings,
        "storage",
        "document_catalog_provider",
        "sqlite",
    )
    normalized_provider = str(provider).strip().lower().replace("-", "_")
    if normalized_provider == "sqlite":
        return SQLiteKnowledgeCatalog(
            _settings_group_value(
                settings,
                "storage",
                "document_catalog_sqlite_path",
                "data/document-catalog.sqlite3",
            )
        )
    if normalized_provider == "postgres":
        return PostgresKnowledgeCatalog(
            _settings_group_value(
                settings,
                "storage",
                "document_catalog_postgres_dsn",
                "",
            )
        )
    return InMemoryKnowledgeCatalog()


def _settings_group_value(settings, group_name: str, field_name: str, default):
    group = getattr(settings, group_name, None)
    if group is not None and hasattr(group, field_name):
        return getattr(group, field_name)
    return getattr(settings, field_name, default)


vector_index_service = VectorIndexService()


def _serialize_indexing_job(job: IndexingJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "document_id": job.document_id,
        "space_id": job.space_id,
        "source_path": job.source_path,
        "status": job.status.value,
        "total_chunks": job.total_chunks,
        "indexed_chunks": job.indexed_chunks,
        "errors": list(job.errors),
    }
