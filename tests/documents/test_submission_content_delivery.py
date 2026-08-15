from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import Request

from app.api.v1 import documents
from app.identity.service import AuthPrincipal
from app.platform.storage import ObjectMetadata


def test_submission_content_forces_attachment_delivery_for_active_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"<script>window.parent.postMessage('unsafe', '*')</script>"
    service = SimpleNamespace(
        submission_content=lambda **_kwargs: (
            content,
            ObjectMetadata(content_type="text/html", size_bytes=len(content)),
            "unsafe.html",
        )
    )
    monkeypatch.setattr(documents, "document_service", lambda _request: service)

    response = documents.submission_content(
        submission_id="submission_1",
        request=cast(Request, object()),
        principal=cast(AuthPrincipal, object()),
    )

    assert response.body == content
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"] == "attachment; filename*=UTF-8''unsafe.html"
    assert response.headers["x-content-type-options"] == "nosniff"
