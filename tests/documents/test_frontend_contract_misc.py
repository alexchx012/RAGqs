"""fix-frontend-contract-misc：恢复 409、单文件上传 415/422 与 Cookie Secure 契约。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from app.documents.schema import document_versions_table
from app.documents.service import DocumentUpload
from app.platform.errors import PlatformError

from .test_commands import _accept, _upload

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _superseded_version(service, principal) -> dict:
    original = service.create_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="contract-misc-upload-1",
    )["items"][0]
    _accept(service, principal, original)
    replacement = service.replace_version(
        principal=principal,
        document_id=original["document_id"],
        expected_version=2,
        file=_upload(content=b"replacement"),
        idempotency_key="contract-misc-replace-1",
    )
    _accept(service, principal, replacement)
    return original


def test_restore_purged_source_returns_409_with_details(service, principal) -> None:
    original = _superseded_version(service, principal)
    with service._engine.begin() as connection:
        connection.execute(
            update(document_versions_table)
            .where(document_versions_table.c.id == original["document_version_id"])
            .values(purge_after_at_utc=NOW - timedelta(days=1))
        )
    with pytest.raises(PlatformError) as error:
        service.restore_version(
            principal=principal,
            document_id=original["document_id"],
            document_version_id=original["document_version_id"],
            expected_version=4,
            idempotency_key="contract-misc-restore-1",
        )
    assert error.value.code == "document_version_purged"
    assert error.value.status_code == 409
    assert error.value.details["document_version_id"] == original["document_version_id"]


def test_replace_version_rejects_unsupported_media_with_415(service, principal) -> None:
    original = _superseded_version(service, principal)
    with pytest.raises(PlatformError) as error:
        service.replace_version(
            principal=principal,
            document_id=original["document_id"],
            expected_version=2,
            file=DocumentUpload(filename="movie.mkv", content=b"x", media_kind="video/x-matroska"),
            idempotency_key="contract-misc-unsupported-1",
        )
    assert error.value.code == "unsupported_media_type"
    assert error.value.status_code == 415
    assert error.value.details["file"] == "movie.mkv"


def test_replace_version_rejects_media_mismatch_with_422(service, principal) -> None:
    original = _superseded_version(service, principal)
    with pytest.raises(PlatformError) as error:
        service.replace_version(
            principal=principal,
            document_id=original["document_id"],
            expected_version=2,
            file=_upload(name="report.pdf", content=b"x"),
            idempotency_key="contract-misc-mismatch-1",
        )
    assert error.value.code == "upload_media_mismatch"
    assert error.value.status_code == 422


def test_replace_version_still_accepts_supported_media(service, principal) -> None:
    original = _superseded_version(service, principal)
    result = service.replace_version(
        principal=principal,
        document_id=original["document_id"],
        expected_version=4,
        file=_upload(name="notes.txt", content=b"md notes"),
        idempotency_key="contract-misc-ok-1",
    )
    assert result["status"] == "pending"
