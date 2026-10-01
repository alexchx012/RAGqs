"""Real PostgreSQL/Milvus coverage for lossless oversized chunk payloads."""

from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, select, text

from app.documents.schema import documents_metadata
from app.indexing import IndexingService
from app.indexing.embedding import EmbeddingConfig, InMemoryEmbeddingProvider
from app.indexing.milvus import HttpMilvusClient, MilvusIndexWriter, _row
from app.indexing.persistence import SqlAlchemyGenerationManager, SqlAlchemyIndexingRepository
from app.indexing.schema import index_chunks_table, indexing_metadata
from app.platform.errors import PlatformError

from .test_contracts import _FailingPublishWriter, _request
from .test_milvus_payload import _long_chunk

pytestmark = pytest.mark.integration


@pytest.fixture
def live_payload_backends():
    postgres_url = os.environ.get("RAGQS_TEST_POSTGRES_URL")
    milvus_uri = os.environ.get("RAG_INDEX_VECTOR_URI")
    if not postgres_url or not milvus_uri:
        pytest.skip("RAGQS_TEST_POSTGRES_URL and RAG_INDEX_VECTOR_URI are required")
    resource_name = f"payload_it_{uuid4().hex}"
    admin = create_engine(postgres_url)
    engine = create_engine(postgres_url, connect_args={"options": f"-csearch_path={resource_name}"})
    clients = []
    collection_name = None
    schema_created = False

    def new_writer():
        nonlocal collection_name
        client = HttpMilvusClient(
            milvus_uri, token=os.environ.get("RAG_INDEX_VECTOR_TOKEN") or None, allow_create=True
        )
        clients.append(client)
        writer = MilvusIndexWriter(
            client,
            InMemoryEmbeddingProvider(
                EmbeddingConfig(
                    base_url="http://unused.invalid",
                    api_key="test",
                    model="fixture",
                    revision="fixture",
                    dimension=4,
                    metric="cosine",
                )
            ),
            collection_prefix=resource_name,
            allow_create_collection=True,
            chunk_repository=SqlAlchemyIndexingRepository(engine),
        )
        collection_name = writer.collection_name
        return writer

    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{resource_name}"'))
        schema_created = True
        documents_metadata.create_all(engine)
        indexing_metadata.create_all(engine)
        yield engine, new_writer
    finally:
        try:
            if clients and collection_name and clients[0].has_collection(collection_name):
                clients[0]._post(
                    "/v2/vectordb/collections/drop", {"collectionName": collection_name}
                )
        finally:
            for client in clients:
                client.close()
            engine.dispose()
            try:
                if schema_created:
                    with admin.begin() as connection:
                        connection.execute(text(f'DROP SCHEMA "{resource_name}" CASCADE'))
            finally:
                admin.dispose()


def _wait_rows(writer, *, status: str, attempt_id: str, count: int):
    """Bounded consistency is eventual; poll reads without replaying writes."""
    deadline = time.monotonic() + 15
    while True:
        rows = writer._load_rows(attempt_id, "publication_1", status)
        if len(rows) == count:
            return rows
        if time.monotonic() >= deadline:
            pytest.fail(f"Milvus {status} rows: expected {count}, got {len(rows)}")
        time.sleep(0.25)


def _wait_search(writer, *, count: int, generation_id: str | None = None):
    deadline = time.monotonic() + 15
    while True:
        page = writer.search("中", ["space_1"], 2, None, generation_id=generation_id)
        if len(page.items) == count:
            return page
        if time.monotonic() >= deadline:
            pytest.fail(f"Milvus search: expected {count}, got {len(page.items)}")
        time.sleep(0.25)


@pytest.mark.parametrize("length", [2300, 7000])
def test_real_long_row_failure_rollback_and_fresh_service_retry(live_payload_backends, length):
    engine, new_writer = live_payload_backends
    writer = new_writer()
    service = IndexingService(
        dense_writer=writer,
        sparse_provider=_FailingPublishWriter(provider_name="sparse"),
        generation_manager=SqlAlchemyGenerationManager(SqlAlchemyIndexingRepository(engine)),
    )
    request = _request()
    content = _long_chunk(length=length).text
    output = service.process_and_stage(
        request,
        content,
        media_kind="text/plain",
        content_manifest_id="manifest_1",
        content_manifest_hash="manifest_hash_1",
    )
    assert len(output.chunks) == 1
    assert output.chunks[0].text == content
    rows = _wait_rows(writer, status="staged", attempt_id=request.attempt_id, count=1)
    assert len(rows[0]["payload"].encode("utf-8")) <= 65535
    payload = json.loads(rows[0]["payload"])
    assert (payload.get("payload_ref") == "index_chunks") == (length == 7000)
    with engine.connect() as connection:
        assert connection.execute(select(index_chunks_table)).first() is None
    with pytest.raises(PlatformError) as failed:
        with engine.begin() as connection:
            service.publish(request, connection=connection, receipt=output.receipt)
    assert failed.value.code == "indexing_publish_failed"
    with engine.connect() as connection:
        assert connection.execute(select(index_chunks_table)).first() is None
    _wait_rows(writer, status="published", attempt_id=request.attempt_id, count=0)
    assert _wait_search(writer, count=0).items == ()

    retry_writer = new_writer()
    retry = IndexingService(
        dense_writer=retry_writer,
        generation_manager=SqlAlchemyGenerationManager(SqlAlchemyIndexingRepository(engine)),
    )
    retry_request = replace(request, attempt_id="attempt_retry", fencing_token=2)
    retried = retry.process_and_stage(
        retry_request,
        content,
        media_kind="text/plain",
        content_manifest_id="manifest_1",
        content_manifest_hash="manifest_hash_1",
    )
    _wait_rows(retry_writer, status="staged", attempt_id=retry_request.attempt_id, count=1)
    validated = []

    def validator(chunks):
        validated.append(chunks)
        return chunks == retried.chunks

    with engine.begin() as connection:
        result = retry.publish(
            retry_request, connection=connection, receipt=retried.receipt, validator=validator
        )
    assert result["state"] == "published"
    assert validated and all(chunks == retried.chunks for chunks in validated)
    published = _wait_rows(
        retry_writer, status="published", attempt_id=retry_request.attempt_id, count=1
    )
    assert len(published[0]["payload"].encode("utf-8")) <= 65535
    reader = new_writer()
    page = _wait_search(reader, count=1, generation_id="generation_initial")
    assert len(page.items) == 1
    for key, value in retried.chunks[0].to_mapping().items():
        assert page.items[0][key] == value
    assert page.items[0]["metadata"]["parent_text"] == content
    assert page.items[0]["score"] >= 0

    with engine.begin() as connection:
        connection.execute(delete(index_chunks_table))
    if length == 7000:
        with pytest.raises(PlatformError) as missing:
            new_writer().search("中", ["space_1"], 2, None)
        assert missing.value.code == "retrieval_degradation"
    else:
        assert new_writer().search("中", ["space_1"], 2, None).items[0]["text"] == content


def test_real_legacy_ascii_inline_payload_without_sql(live_payload_backends):
    _engine, new_writer = live_payload_backends
    writer = new_writer()
    writer.ensure_collection()
    chunk = _long_chunk(length=100)
    legacy_row = _row(
        status="published",
        attempt_id="legacy_attempt",
        chunk=chunk,
        vector=(0.1,) * 4,
        content_hash="legacy_hash",
        fencing_token=1,
    )
    legacy_row["payload"] = json.dumps(chunk.to_mapping(), ensure_ascii=True)
    assert len(legacy_row["payload"].encode("utf-8")) < 65535
    writer._client.insert(writer.collection_name, (legacy_row,))
    _wait_rows(writer, status="published", attempt_id="legacy_attempt", count=1)
    page = _wait_search(new_writer(), count=1, generation_id="generation_1")
    assert len(page.items) == 1
    for key, value in chunk.to_mapping().items():
        assert page.items[0][key] == value
