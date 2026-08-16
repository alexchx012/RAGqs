"""HTTP contract tests for renderer-aware document preview content."""

from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import create_engine, select, update
from sqlalchemy.pool import StaticPool

from app.api.v1.dependencies import current_principal
from app.api.v1.documents import router as documents_router
from app.documents.indexing import NoopIndexingHandoff
from app.documents.preview import PreviewHit, ProcessingReceiptPreviewRenderer
from app.documents.schema import documents_metadata, publications_table
from app.documents.service import DocumentsService, DocumentUpload
from app.identity.service import AuthPrincipal
from app.platform.http_contract import register_exception_handlers
from app.platform.storage import MemoryObjectStore

from .test_commands import _accept


class _Identity:
    def authorize_space(self, *, principal, space_id: str, action: str) -> str:
        assert principal.user_id == "user_1"
        assert space_id == "space_1"
        assert action in {"manage", "contribute", "read"}
        return "manage"


class _MessageHits:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def get_hits(self, principal, message_id: str, document_id: str, document_version_id: str):
        del principal
        self.calls.append((message_id, document_id, document_version_id))
        return (PreviewHit(index=1, summary="Cited page", locator={"page": 1}),)


@pytest.fixture()
def preview_api() -> tuple[TestClient, DocumentsService, AuthPrincipal, _MessageHits]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    documents_metadata.create_all(engine)
    principal = AuthPrincipal(
        user_id="user_1",
        auth_session_id="session_1",
        username="alice",
        role="user",
        department_id=None,
    )
    hits = _MessageHits()
    service = DocumentsService(
        engine,
        now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        object_store=MemoryObjectStore(),
        identity_access=_Identity(),
        indexing_handoff_port=NoopIndexingHandoff(),
        preview_renderer=ProcessingReceiptPreviewRenderer(),
        message_citation_preview_port=hits,
    )
    app = FastAPI()
    app.state.platform_runtime = SimpleNamespace(
        resolve=lambda name: service if name == "documents_service" else None
    )
    register_exception_handlers(app)
    app.include_router(documents_router, prefix="/v1")
    app.dependency_overrides[current_principal] = lambda: principal
    client = TestClient(app)
    yield client, service, principal, hits
    client.close()
    engine.dispose()


def _accepted(
    service: DocumentsService,
    principal: AuthPrincipal,
    *,
    filename: str,
    content: bytes,
    media_kind: str,
    key: str,
) -> dict[str, object]:
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[DocumentUpload(filename=filename, content=content, media_kind=media_kind)],
        idempotency_key=key,
    )
    item = created["items"][0]
    _accept(service, principal, item)
    return item


def _set_processing_summary(
    service: DocumentsService, item: dict[str, object], summary: dict[str, object]
) -> None:
    with service._engine.begin() as connection:
        manifest = connection.execute(
            select(publications_table.c.resource_manifest_json).where(
                publications_table.c.id == item["publication_id"]
            )
        ).scalar_one()
        updated_manifest = dict(manifest)
        updated_manifest["processing_summary"] = summary
        connection.execute(
            update(publications_table)
            .where(publications_table.c.id == item["publication_id"])
            .values(resource_manifest_json=updated_manifest)
        )


def test_preview_returns_renderer_metadata_and_message_hits(preview_api) -> None:
    client, service, principal, hits = preview_api
    item = _accepted(
        service,
        principal,
        filename="guide.pdf",
        content=b"test",
        media_kind="application/pdf",
        key="pdf-upload-1",
    )
    _set_processing_summary(
        service,
        item,
        {"has_text_layer": True, "page_count": 1, "tree": {}},
    )

    response = client.get(
        f"/v1/documents/{item['document_id']}/preview",
        params={"message_id": "message_1"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["document_version_id"] == item["document_version_id"]
    assert body["media_kind"] == "pdf"
    assert body["content_url"].endswith(f"document_version_id={item['document_version_id']}")
    assert body["has_text_layer"] is True
    assert body["page_count"] == 1
    assert body["hits"][0]["locator"]["page"] == 1
    assert hits.calls == [("message_1", item["document_id"], item["document_version_id"])]


def test_pdf_content_honors_single_byte_ranges_and_head(preview_api) -> None:
    client, service, principal, _ = preview_api
    item = _accepted(
        service,
        principal,
        filename="guide.pdf",
        content=b"test",
        media_kind="application/pdf",
        key="pdf-upload-1",
    )
    url = f"/v1/documents/{item['document_id']}/content"
    params = {"document_version_id": item["document_version_id"]}

    partial = client.get(url, params=params, headers={"Range": "bytes=0-3"})
    suffix = client.get(url, params=params, headers={"Range": "bytes=-2"})
    open_ended = client.get(url, params=params, headers={"Range": "bytes=2-"})
    invalid = client.get(url, params=params, headers={"Range": "bytes=4-5"})
    multiple = client.get(url, params=params, headers={"Range": "bytes=0-1,2-3"})
    head = client.head(url, params=params, headers={"Range": "bytes=0-3"})

    assert partial.status_code == 206
    assert partial.headers["accept-ranges"] == "bytes"
    assert partial.headers["content-range"] == "bytes 0-3/4"
    assert partial.headers["content-length"] == "4"
    assert partial.content == b"test"
    assert suffix.status_code == 206
    assert suffix.headers["content-range"] == "bytes 2-3/4"
    assert suffix.content == b"st"
    assert open_ended.status_code == 206
    assert open_ended.headers["content-range"] == "bytes 2-3/4"
    assert open_ended.content == b"st"
    assert invalid.status_code == 416
    assert invalid.headers["content-range"] == "bytes */4"
    assert multiple.status_code == 416
    assert multiple.headers["content-range"] == "bytes */4"
    assert head.status_code == 206
    assert head.headers["content-range"] == "bytes 0-3/4"
    assert head.headers["content-length"] == "4"
    assert head.content == b""


def test_content_keeps_text_readable_and_images_raw(preview_api) -> None:
    client, service, principal, _ = preview_api
    text = _accepted(
        service,
        principal,
        filename="guide.txt",
        content=b"hello",
        media_kind="text/plain",
        key="text-upload-1",
    )
    image = _accepted(
        service,
        principal,
        filename="diagram.png",
        content=b"image bytes",
        media_kind="image/png",
        key="image-upload-1",
    )

    text_response = client.get(
        f"/v1/documents/{text['document_id']}/content",
        params={"document_version_id": text["document_version_id"]},
    )
    image_response = client.get(
        f"/v1/documents/{image['document_id']}/content",
        params={"document_version_id": image["document_version_id"]},
    )

    assert text_response.headers["content-type"].startswith("text/plain")
    assert text_response.text == "hello"
    assert image_response.headers["content-type"] == "image/png"
    assert image_response.content == b"image bytes"


def test_content_returns_structured_word_and_selected_sheets(preview_api) -> None:
    client, service, principal, _ = preview_api
    word = _accepted(
        service,
        principal,
        filename="guide.docx",
        content=b"original Word source",
        media_kind="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        key="word-upload-1",
    )
    _set_processing_summary(
        service,
        word,
        {
            "tree": {
                "tree_indexed": True,
                "sections": [{"path": ["Policy"], "paragraphs": ["Persisted paragraph"]}],
            }
        },
    )

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Sheet1"
    worksheet.append(["A"])
    worksheet.append(["B"])
    xlsx_bytes = BytesIO()
    workbook.save(xlsx_bytes)
    spreadsheet = _accepted(
        service,
        principal,
        filename="grid.xlsx",
        content=xlsx_bytes.getvalue(),
        media_kind="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="xlsx-upload-1",
    )
    _set_processing_summary(
        service,
        spreadsheet,
        {
            "sheet_manifest": [{"sheet": "Sheet1"}],
            "row_groups": [{"sheet": "Sheet1", "start": 1, "end": 2}],
        },
    )
    csv = _accepted(
        service,
        principal,
        filename="grid.csv",
        content=b"name\nAda\n",
        media_kind="text/csv",
        key="csv-upload-1",
    )

    word_response = client.get(
        f"/v1/documents/{word['document_id']}/content",
        params={"document_version_id": word["document_version_id"]},
    )
    xlsx_response = client.get(
        f"/v1/documents/{spreadsheet['document_id']}/content",
        params={"document_version_id": spreadsheet["document_version_id"], "sheet": "Sheet1"},
    )
    csv_response = client.get(
        f"/v1/documents/{csv['document_id']}/content",
        params={"document_version_id": csv["document_version_id"], "sheet": "CSV"},
    )

    assert word_response.headers["content-type"].startswith("application/json")
    assert word_response.json() == {
        "sections": [{"path": ["Policy"], "paragraphs": ["Persisted paragraph"]}]
    }
    assert xlsx_response.headers["content-type"].startswith("application/json")
    assert xlsx_response.json() == {
        "sheet": "Sheet1",
        "row_count": 2,
        "rows": [["A"], ["B"]],
    }
    assert csv_response.headers["content-type"].startswith("application/json")
    assert csv_response.json() == {
        "sheet": "CSV",
        "row_count": 2,
        "rows": [["name"], ["Ada"]],
    }
