"""Versioned documents-ingestion HTTP contract."""

from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Header, Path, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from app.documents.service import DocumentsService, DocumentUpload
from app.identity.service import AuthPrincipal
from app.platform.errors import PlatformError

from .dependencies import current_principal
from .document_models import ExpectedVersionRequest, SubmissionRejectRequest

router = APIRouter(tags=["documents"])


def document_service(request: Request) -> DocumentsService:
    service = request.app.state.platform_runtime.resolve("documents_service")
    if not isinstance(service, DocumentsService):
        raise RuntimeError("documents service is not configured")
    return service


def _key(value: str | None) -> str:
    if not value or not value.strip():
        raise PlatformError("validation_error", "Idempotency-Key is required", {}, 422)
    return value.strip()


async def _upload(file: UploadFile) -> DocumentUpload:
    return DocumentUpload(
        filename=file.filename or "",
        content=await file.read(),
        media_kind=file.content_type or "application/octet-stream",
    )


@router.post("/spaces/{space_id}/documents", status_code=202)
async def upload_documents(
    space_id: Annotated[str, Path(min_length=1, max_length=128)],
    files: Annotated[list[UploadFile], File(...)],
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    idempotency_key: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    service = document_service(request)
    key = _key(idempotency_key)
    uploads = [await _upload(file) for file in files]
    permission = "manage"
    if service._identity_access is not None:
        try:
            permission = service._identity_access.authorize_space(
                principal=principal, space_id=space_id, action="manage"
            )
        except PlatformError as error:
            if error.code != "space_action_forbidden":
                raise
            permission = service._identity_access.authorize_space(
                principal=principal, space_id=space_id, action="contribute"
            )
    if permission == "contribute":
        items = [
            service.create_submission(
                principal=principal,
                space_id=space_id,
                file=file,
                idempotency_key=f"{key}:{index}",
            )
            for index, file in enumerate(uploads)
        ]
        return {"items": items}
    return service.create_initial_upload(
        principal=principal, space_id=space_id, files=uploads, idempotency_key=key
    )


@router.get("/spaces/{space_id}/documents")
def list_documents(
    space_id: Annotated[str, Path(min_length=1, max_length=128)],
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    q: Annotated[str | None, Query(max_length=256)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, object]:
    return document_service(request).list_documents(
        principal=principal, space_id=space_id, q=q, page=page, page_size=page_size
    )


@router.post("/documents/{document_id}/versions", status_code=202)
async def replace_document_version(
    document_id: Annotated[str, Path(min_length=1, max_length=128)],
    file: Annotated[UploadFile, File(...)],
    expected_version: Annotated[int, Form(ge=1)],
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    idempotency_key: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    result = document_service(request).replace_version(
        principal=principal,
        document_id=document_id,
        expected_version=expected_version,
        file=await _upload(file),
        idempotency_key=_key(idempotency_key),
    )
    if result.get("deduplicated"):
        return JSONResponse(result, status_code=200)  # type: ignore[return-value]
    return result


@router.get("/documents/{document_id}/versions")
def list_versions(
    document_id: Annotated[str, Path(min_length=1, max_length=128)],
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
) -> dict[str, object]:
    return document_service(request).list_versions(principal=principal, document_id=document_id)


@router.post("/documents/{document_id}/versions/{document_version_id}/restore", status_code=202)
def restore_document_version(
    document_id: Annotated[str, Path(min_length=1, max_length=128)],
    document_version_id: Annotated[str, Path(min_length=1, max_length=128)],
    body: ExpectedVersionRequest,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    idempotency_key: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    return document_service(request).restore_version(
        principal=principal,
        document_id=document_id,
        document_version_id=document_version_id,
        expected_version=body.expected_version,
        idempotency_key=_key(idempotency_key),
    )


@router.post("/documents/{document_id}/reindex", status_code=202)
def reindex_document(
    document_id: Annotated[str, Path(min_length=1, max_length=128)],
    body: ExpectedVersionRequest,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    idempotency_key: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    return document_service(request).reindex(
        principal=principal,
        document_id=document_id,
        expected_version=body.expected_version,
        idempotency_key=_key(idempotency_key),
    )


@router.delete("/documents/{document_id}", status_code=202)
def delete_document(
    document_id: Annotated[str, Path(min_length=1, max_length=128)],
    body: ExpectedVersionRequest,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    idempotency_key: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    return document_service(request).delete_document(
        principal=principal,
        document_id=document_id,
        expected_version=body.expected_version,
        idempotency_key=_key(idempotency_key),
    )


@router.get("/documents/{document_id}/preview")
def preview_document(
    document_id: Annotated[str, Path(min_length=1, max_length=128)],
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    document_version_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
) -> dict[str, object]:
    return document_service(request).preview(
        principal=principal, document_id=document_id, document_version_id=document_version_id
    )


@router.get("/documents/{document_id}/content")
def content_document(
    document_id: Annotated[str, Path(min_length=1, max_length=128)],
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    document_version_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
) -> Response:
    content, metadata = document_service(request).content(
        principal=principal, document_id=document_id, document_version_id=document_version_id
    )
    return Response(content=content, media_type=metadata.content_type)


@router.get("/upload-batches/{upload_batch_id}")
def upload_batch(
    upload_batch_id: Annotated[str, Path(min_length=1, max_length=128)],
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
) -> dict[str, object]:
    return document_service(request).get_upload_batch(
        principal=principal, upload_batch_id=upload_batch_id
    )


@router.get("/ingestion-jobs")
def list_ingestion_jobs(
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    space_id: Annotated[str | None, Query(max_length=128)] = None,
) -> dict[str, object]:
    return document_service(request).list_jobs(principal=principal, limit=limit, space_id=space_id)


@router.post("/ingestion-jobs/{job_id}/cancel")
def cancel_ingestion_job(
    job_id: Annotated[str, Path(min_length=1, max_length=128)],
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
) -> dict[str, object]:
    return document_service(request).cancel_job(principal=principal, job_id=job_id)


@router.post("/ingestion-jobs/{job_id}/replay", status_code=202)
def replay_ingestion_job(
    job_id: Annotated[str, Path(min_length=1, max_length=128)],
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    idempotency_key: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    return document_service(request).replay_job(
        principal=principal, job_id=job_id, idempotency_key=_key(idempotency_key)
    )


@router.get("/submissions")
def list_submissions(
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    status: Annotated[str | None, Query()] = None,
) -> dict[str, object]:
    return document_service(request).list_submissions(principal=principal, status=status)


@router.get("/submissions/{submission_id}/content")
def submission_content(
    submission_id: Annotated[str, Path(min_length=1, max_length=128)],
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
) -> Response:
    content, _metadata, filename = document_service(request).submission_content(
        principal=principal, submission_id=submission_id
    )
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/submissions/{submission_id}/withdraw")
def withdraw_submission(
    submission_id: Annotated[str, Path(min_length=1, max_length=128)],
    body: ExpectedVersionRequest,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    idempotency_key: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    return document_service(request).withdraw_submission(
        principal=principal,
        submission_id=submission_id,
        expected_version=body.expected_version,
        idempotency_key=_key(idempotency_key),
    )


@router.delete("/submissions/{submission_id}", status_code=204)
def delete_submission(
    submission_id: Annotated[str, Path(min_length=1, max_length=128)],
    body: ExpectedVersionRequest,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    idempotency_key: Annotated[str | None, Header()] = None,
) -> Response:
    document_service(request).delete_submission(
        principal=principal,
        submission_id=submission_id,
        expected_version=body.expected_version,
        idempotency_key=_key(idempotency_key),
    )
    return Response(status_code=204)


@router.get("/approvals/submissions")
def approval_submissions(
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
) -> dict[str, object]:
    return document_service(request).list_approval_submissions(principal=principal)


@router.post("/approvals/submissions/{submission_id}/approve", status_code=202)
def approve_submission(
    submission_id: Annotated[str, Path(min_length=1, max_length=128)],
    body: ExpectedVersionRequest,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    idempotency_key: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    return document_service(request).approve_submission(
        principal=principal,
        submission_id=submission_id,
        expected_version=body.expected_version,
        idempotency_key=_key(idempotency_key),
    )


@router.post("/approvals/submissions/{submission_id}/reject")
def reject_submission(
    submission_id: Annotated[str, Path(min_length=1, max_length=128)],
    body: SubmissionRejectRequest,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    idempotency_key: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    return document_service(request).reject_submission(
        principal=principal,
        submission_id=submission_id,
        expected_version=body.expected_version,
        idempotency_key=_key(idempotency_key),
        reason=body.reason,
    )


__all__ = ["document_service", "router"]
