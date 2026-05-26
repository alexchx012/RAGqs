"""文件上传接口"""

import inspect
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from loguru import logger
from pydantic import BaseModel

from app.config import config
from app.ingestion import IndexingJob
from app.models.response import envelope_json_response
from app.providers.contracts import IngestionResult
from app.providers.factory import get_default_provider_container
from app.security.uploads import (
    UploadSecurityError,
    UploadSecurityPolicy,
    parse_allowed_extensions,
    secure_upload_payload,
)
from app.services.vector_index_service import vector_index_service

router = APIRouter()

UPLOAD_DIR = Path("./uploads")


class KnowledgeSpaceCreateRequest(BaseModel):
    space_id: str
    name: str | None = None
    description: str = ""


@router.post("/upload")
async def upload_file(file: UploadFile = File(...), space_id: str = "default"):
    """上传文件并自动创建向量索引"""
    try:
        content = await file.read()
        try:
            upload = secure_upload_payload(
                filename=file.filename,
                content=content,
                upload_dir=UPLOAD_DIR,
                policy=_upload_security_policy(),
            )
        except UploadSecurityError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        upload.file_path.parent.mkdir(parents=True, exist_ok=True)
        if upload.file_path.exists():
            upload.file_path.unlink()
        upload.file_path.write_bytes(upload.content)
        logger.info(f"文件上传成功: {upload.file_path}")

        try:
            indexing_job = _call_index_single_file(str(upload.file_path), space_id=space_id)
        except Exception as e:
            logger.error(f"向量索引创建失败: {e}")
            raise HTTPException(status_code=500, detail=f"向量索引创建失败: {e}") from e

        return envelope_json_response(
            {
                "filename": upload.safe_filename,
                "size": len(upload.content),
                "spaceId": indexing_job.space_id,
                "indexing": _serialize_indexing_job(indexing_job),
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"文件上传失败: {e}") from e


@router.get("/knowledge-spaces")
async def list_knowledge_spaces():
    spaces = vector_index_service.list_knowledge_spaces()
    return envelope_json_response({"spaces": [_serialize_knowledge_space(space) for space in spaces]})


@router.post("/knowledge-spaces")
async def create_knowledge_space(request: KnowledgeSpaceCreateRequest):
    space = vector_index_service.document_catalog.ensure_space(
        request.space_id,
        name=request.name,
        description=request.description,
    )
    return envelope_json_response({"space": _serialize_knowledge_space(space)})


@router.get("/knowledge-spaces/{space_id}/documents")
async def list_documents(space_id: str):
    documents = vector_index_service.list_documents(space_id=space_id)
    return envelope_json_response(
        {
            "space_id": space_id,
            "count": len(documents),
            "documents": [_serialize_document_record(document) for document in documents],
        }
    )


@router.get("/knowledge-spaces/{space_id}/documents/{document_id}")
async def get_document(space_id: str, document_id: str):
    document = vector_index_service.get_document(space_id=space_id, document_id=document_id)
    if document is None:
        raise HTTPException(status_code=404, detail=f"文档不存在: {space_id}/{document_id}")
    return envelope_json_response({"document": _serialize_document_record(document)})


@router.delete("/knowledge-spaces/{space_id}/documents/{document_id}")
async def delete_document(space_id: str, document_id: str):
    try:
        document = vector_index_service.delete_document(space_id=space_id, document_id=document_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return envelope_json_response({"document": _serialize_document_record(document)})


@router.post("/knowledge-spaces/{space_id}/documents/{document_id}/rebuild")
async def rebuild_document(space_id: str, document_id: str):
    try:
        job = vector_index_service.rebuild_document(space_id=space_id, document_id=document_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return envelope_json_response({"indexing": _serialize_indexing_job(job, include_source_path=True)})


@router.get("/index-jobs")
async def list_indexing_jobs(
    document_id: str | None = None,
    source_path: str | None = None,
    status: str | None = None,
):
    jobs = vector_index_service.list_indexing_jobs(
        document_id=document_id,
        source_path=source_path,
        status=status,
    )
    return envelope_json_response(
        {
            "count": len(jobs),
            "jobs": [_serialize_indexing_job(job, include_source_path=True) for job in jobs],
        }
    )


@router.get("/index-jobs/{job_id}")
async def get_indexing_job(job_id: str):
    job = vector_index_service.get_indexing_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"索引任务不存在: {job_id}")
    return envelope_json_response({"indexing": _serialize_indexing_job(job, include_source_path=True)})


@router.post("/index-jobs/{job_id}/retry")
async def retry_indexing_job(job_id: str):
    try:
        job = vector_index_service.retry_indexing_job(job_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return envelope_json_response({"indexing": _serialize_indexing_job(job, include_source_path=True)})


def _serialize_indexing_job(job, include_source_path: bool = False) -> dict | None:
    if job is None:
        return None

    status = getattr(job.status, "value", str(job.status))
    data = {
        "job_id": job.job_id,
        "document_id": job.document_id,
        "status": status,
        "total_chunks": job.total_chunks,
        "indexed_chunks": job.indexed_chunks,
        "errors": list(job.errors),
    }
    space_id = getattr(job, "space_id", "default")
    if space_id != "default":
        data["space_id"] = space_id
    if include_source_path:
        data["source_path"] = job.source_path
    return data


def _call_index_single_file(file_path: str, *, space_id: str):
    ingestion_provider = get_default_provider_container().ingestion_provider
    method = ingestion_provider.index_file
    if _accepts_keyword(method, "space_id"):
        result = method(file_path, space_id=space_id)
    else:
        result = method(file_path)
    return _indexing_job_from_ingestion_result(result, file_path=file_path, space_id=space_id)


def _indexing_job_from_ingestion_result(
    result: IngestionResult,
    *,
    file_path: str,
    space_id: str,
):
    if not result.success:
        raise RuntimeError(result.error_message or "indexing failed")

    indexing_job = result.metadata.get("indexing_job") or result.metadata.get("job")
    if indexing_job is not None:
        return indexing_job

    document_id = str(result.metadata.get("document_id") or Path(file_path).stem)
    job_id = result.metadata.get("job_id")
    job = IndexingJob.create(
        document_id=document_id,
        source_path=file_path,
        space_id=space_id,
        job_id=str(job_id) if job_id else None,
    )
    job.start()
    total_chunks = int(result.metadata.get("total_chunks", result.document_count))
    indexed_chunks = int(result.metadata.get("indexed_chunks", total_chunks))
    job.complete(total_chunks=total_chunks, indexed_chunks=indexed_chunks)
    return job


def _upload_security_policy() -> UploadSecurityPolicy:
    return build_upload_security_policy(config)


def build_upload_security_policy(settings=config) -> UploadSecurityPolicy:
    upload_config = getattr(settings, "upload", settings)
    return UploadSecurityPolicy(
        allowed_extensions=parse_allowed_extensions(
            _settings_group_value(
                upload_config,
                settings,
                "allowed_extensions",
                "upload_allowed_extensions",
                "txt,md,csv,html,htm,json",
            )
        ),
        max_bytes=_settings_group_value(
            upload_config,
            settings,
            "max_bytes",
            "upload_max_bytes",
            10 * 1024 * 1024,
        ),
        prompt_injection_scan_enabled=_settings_group_value(
            upload_config,
            settings,
            "prompt_injection_scan_enabled",
            "upload_prompt_injection_scan_enabled",
            True,
        ),
    )


def _settings_group_value(group, settings, group_name: str, flat_name: str, default):
    if group is not None and hasattr(group, group_name):
        return getattr(group, group_name)
    return getattr(settings, flat_name, default)


def _accepts_keyword(method, keyword: str) -> bool:
    parameters = inspect.signature(method).parameters.values()
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD or parameter.name == keyword
        for parameter in parameters
    )


def _serialize_knowledge_space(space) -> dict:
    return {
        "space_id": space.space_id,
        "name": space.name,
        "description": getattr(space, "description", ""),
    }


def _serialize_document_record(document) -> dict:
    status = getattr(document.status, "value", str(document.status))
    return {
        "document_id": document.document_id,
        "space_id": document.space_id,
        "source_path": document.source_path,
        "file_name": document.file_name,
        "status": status,
        "latest_job_id": document.latest_job_id,
        "total_chunks": document.total_chunks,
        "indexed_chunks": document.indexed_chunks,
        "errors": list(getattr(document, "errors", []) or []),
    }
