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
from app.chat.preview import SqlAlchemyMessageCitationPreviewAdapter
from app.chat.schema import chat_conversation_table, chat_message_table, chat_metadata
from app.documents.indexing import NoopIndexingHandoff
from app.documents.preview import PreviewHit, ProcessingReceiptPreviewRenderer
from app.documents.schema import documents_metadata, publications_table
from app.documents.service import DocumentsService, DocumentUpload
from app.identity.service import AuthPrincipal
from app.platform.database import core_metadata
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
    core_metadata.create_all(engine)

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
    service: DocumentsService, item: dict[str, object], summary: dict[str, object] | None
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
        content=b"%PDF-1.7 test",
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


def test_preview_without_processing_summary_keeps_the_rich_wire_shape(preview_api) -> None:
    client, service, principal, _ = preview_api
    item = _accepted(
        service,
        principal,
        filename="guide.txt",
        content=b"%PDF-1.7 test",
        media_kind="text/plain",
        key="text-upload-1",
    )
    _set_processing_summary(service, item, None)

    response = client.get(f"/v1/documents/{item['document_id']}/preview")

    assert response.status_code == 200
    body = response.json()
    assert body["has_text_layer"] is False
    assert body["tree_indexed"] is False
    assert body["page_count"] is None
    assert body["sheets"] is None
    assert body["hits"] == []
    assert body["content_url"] == (
        f"/v1/documents/{item['document_id']}/content?"
        f"document_version_id={item['document_version_id']}"
    )


def test_preview_uses_the_real_owned_message_adapter_and_hides_foreign_messages(
    preview_api,
) -> None:
    client, service, principal, _ = preview_api
    item = _accepted(
        service,
        principal,
        filename="guide.pdf",
        content=b"%PDF-1.7 test",
        media_kind="application/pdf",
        key="pdf-upload-real-message-1",
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    chat_metadata.create_all(service._engine)
    with service._engine.begin() as connection:
        for owner, conversation_id, message_id in (
            ("user_1", "conversation_owned", "message_owned"),
            ("user_2", "conversation_foreign", "message_foreign"),
        ):
            connection.execute(
                chat_conversation_table.insert().values(
                    id=conversation_id,
                    owner_user_id=owner,
                    title="Preview citations",
                    pinned=False,
                    group_id=None,
                    effort_level="quick",
                    scope_json={},
                    last_active_at_utc=now,
                    created_at_utc=now,
                    updated_at_utc=now,
                )
            )
            connection.execute(
                chat_message_table.insert().values(
                    id=message_id,
                    conversation_id=conversation_id,
                    owner_user_id=owner,
                    role="assistant",
                    content="answer",
                    answer_mode="grounded",
                    effort_level="quick",
                    generation_id=f"generation_{message_id}",
                    root_generation_id=f"generation_{message_id}",
                    retry_of_generation_id=None,
                    attempt_number=1,
                    status="completed",
                    stop_reason=None,
                    notices_json=[],
                    citations_json=[
                        {
                            "document_id": item["document_id"],
                            "document_version_id": item["document_version_id"],
                            "locator": {"page": 1, "span": "0:4"},
                            "snippet": "Selected source",
                        },
                        {
                            "document_id": item["document_id"],
                            "document_version_id": "version_not_selected",
                            "locator": {"page": 2},
                            "snippet": "Wrong version",
                        },
                    ],
                    created_at_utc=now,
                    updated_at_utc=now,
                )
            )
    service._message_citation_preview_port = SqlAlchemyMessageCitationPreviewAdapter(
        service._engine
    )

    owned = client.get(
        f"/v1/documents/{item['document_id']}/preview",
        params={"message_id": "message_owned"},
    )
    foreign = client.get(
        f"/v1/documents/{item['document_id']}/preview",
        params={"message_id": "message_foreign"},
    )

    assert owned.status_code == 200
    assert owned.json()["hits"] == [
        {
            "index": 1,
            "summary": "Selected source",
            "locator": {"page": 1, "span": {"start": 0, "end": 4}},
            "snippet": "Selected source",
        }
    ]
    assert foreign.status_code == 404
    assert foreign.json()["error"]["code"] == "message_not_found"


def test_pdf_content_honors_single_byte_ranges_and_head(preview_api) -> None:
    client, service, principal, _ = preview_api
    item = _accepted(
        service,
        principal,
        filename="guide.pdf",
        content=b"%PDF-1.7 test",
        media_kind="application/pdf",
        key="pdf-upload-1",
    )
    url = f"/v1/documents/{item['document_id']}/content"
    params = {"document_version_id": item["document_version_id"]}

    partial = client.get(url, params=params, headers={"Range": "bytes=0-3"})
    suffix = client.get(url, params=params, headers={"Range": "bytes=-2"})
    open_ended = client.get(url, params=params, headers={"Range": "bytes=2-"})
    invalid = client.get(url, params=params, headers={"Range": "bytes=20-25"})
    multiple = client.get(url, params=params, headers={"Range": "bytes=0-1,2-3"})
    head = client.head(url, params=params, headers={"Range": "bytes=0-3"})

    assert partial.status_code == 206
    assert partial.headers["accept-ranges"] == "bytes"
    assert partial.headers["content-range"] == "bytes 0-3/13"
    assert partial.headers["content-length"] == "4"
    assert partial.content == b"%PDF"
    assert suffix.status_code == 206
    assert suffix.headers["content-range"] == "bytes 11-12/13"
    assert suffix.content == b"st"
    assert open_ended.status_code == 206
    assert open_ended.headers["content-range"] == "bytes 2-12/13"
    assert open_ended.content == b"DF-1.7 test"
    assert invalid.status_code == 416
    assert invalid.headers["content-range"] == "bytes */13"
    assert multiple.status_code == 416
    assert multiple.headers["content-range"] == "bytes */13"
    assert head.status_code == 206
    assert head.headers["content-range"] == "bytes 0-3/13"
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
        content=b"\x89PNG\r\n\x1a\n image bytes",
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
    assert image_response.content == b"\x89PNG\r\n\x1a\n image bytes"


def test_content_returns_structured_word_and_selected_sheets(preview_api) -> None:
    client, service, principal, _ = preview_api
    word = _accepted(
        service,
        principal,
        filename="guide.docx",
        content=b"PK original Word source",
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


def test_head_non_raw_content_returns_405_without_object_read(preview_api) -> None:
    client, service, principal, _ = preview_api
    item = _accepted(
        service,
        principal,
        filename="notes.txt",
        content=b"plain text",
        media_kind="text/plain",
        key="head-txt-1",
    )

    original_store = service._object_store
    get_calls = {"count": 0}

    class _CountingStore:
        def get(self, key: str):
            get_calls["count"] += 1
            return original_store.get(key)

        def __getattr__(self, name: str):
            return getattr(original_store, name)

    service._object_store = _CountingStore()  # type: ignore[assignment]
    try:
        response = client.head(f"/v1/documents/{item['document_id']}/content")
    finally:
        service._object_store = original_store  # type: ignore[assignment]

    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert get_calls["count"] == 0
