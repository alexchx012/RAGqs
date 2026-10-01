from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, delete, event, select

from app.documents.schema import documents_metadata
from app.indexing import IndexingService
from app.indexing.milvus import _chunk_from_row, _row
from app.indexing.persistence import SqlAlchemyGenerationManager, SqlAlchemyIndexingRepository
from app.indexing.schema import index_chunks_table, indexing_metadata
from app.platform.errors import PlatformError

from .test_contracts import _FailingPublishWriter, _request
from .test_index_backends import FakeMilvus, _chunk, _writer


@pytest.mark.parametrize("payload_bytes", [65535, 65536])
def test_payload_uses_sql_reference_only_above_utf8_capacity(payload_bytes: int) -> None:
    chunk = replace(_chunk(), metadata={"padding": ""})
    base_size = len(
        json.dumps(chunk.to_mapping(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    # Multibyte input makes a character-count threshold observably incorrect.
    padding_bytes = payload_bytes - base_size
    padding = "中" * (padding_bytes // 3) + "x" * (padding_bytes % 3)
    chunk = replace(chunk, metadata={"padding": padding})
    row = _row(
        status="staged",
        attempt_id="attempt_1",
        chunk=chunk,
        vector=(0.1,) * 4,
        content_hash="hash_1",
        fencing_token=1,
    )
    assert len(row["payload"].encode("utf-8")) <= 65535
    payload = json.loads(row["payload"])
    if payload_bytes == 65535:
        assert payload == chunk.to_mapping()
    else:
        assert payload == {
            "payload_ref": "index_chunks",
            "generation_id": "generation_1",
            "publication_id": "publication_1",
            "chunk_id": "chunk_1",
        }


@pytest.mark.parametrize("as_mapping", [False, True])
def test_legacy_full_payload_decodes_without_sql(as_mapping: bool) -> None:
    chunk = _chunk()
    payload = chunk.to_mapping()
    assert _chunk_from_row({"payload": payload if as_mapping else json.dumps(payload)}) == chunk


def test_missing_payload_never_fabricates_chunk_id_as_text() -> None:
    chunk = _chunk()
    row = {key: value for key, value in chunk.to_mapping().items() if key != "text"}
    with pytest.raises(PlatformError) as failure:
        _chunk_from_row(row)
    assert failure.value.code == "retrieval_degradation"


def _long_chunk(chunk_id: str = "chunk_1", length: int = 7000):
    text = "| 姓名 |\n| --- |\n| " + "中" * length + " |"
    return replace(
        _chunk(chunk_id),
        text=text,
        embedding_text=text,
        snippet=text,
        metadata={"parent_id": "parent_1", "parent_text": text},
        locator={"paragraph_start": 0, "paragraph_end": 2},
    )


def _repository(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'chunks.db'}")
    indexing_metadata.create_all(engine)
    return SqlAlchemyIndexingRepository(engine)


def _store(repository, chunks, connection):
    for chunk in chunks:
        repository.record_published_chunks(
            SimpleNamespace(
                expected_generation_id=chunk.generation_id, publication_id=chunk.publication_id
            ),
            (chunk,),
            connection=connection,
        )


def test_reference_batch_over_one_thousand_rows_keeps_exact_identities(tmp_path) -> None:
    repository = _repository(tmp_path)
    chunks = tuple(_chunk(f"chunk_{i}") for i in range(1001))
    other_publication = replace(chunks[0], publication_id="publication_other")
    other_generation = replace(chunks[0], generation_id="generation_other")
    with repository._engine.begin() as connection:
        _store(repository, (*chunks, other_publication, other_generation), connection)
    loaded = repository.load_chunks(
        tuple((chunk.generation_id, chunk.publication_id, chunk.chunk_id) for chunk in chunks)
    )
    assert {chunk.chunk_id for chunk in loaded} == {chunk.chunk_id for chunk in chunks}
    assert len(loaded) == 1001
    assert all(chunk.publication_id == "publication_1" for chunk in loaded)
    assert all(chunk.generation_id == "generation_1" for chunk in loaded)


@pytest.mark.parametrize("length", [2300, 7000])
def test_long_payload_stage_publish_and_new_writer_search(tmp_path, length) -> None:
    repository = _repository(tmp_path)
    client = FakeMilvus()
    writer = _writer(client)
    writer._chunk_repository = repository
    chunks = (_long_chunk(length=length), _long_chunk("chunk_2", length=length))
    staged = writer.stage_chunks("attempt_1", "publication_1", "document_1", "version_1", chunks)
    assert staged.resource_ids == (
        "attempt_1:publication_1:chunk_1",
        "attempt_1:publication_1:chunk_2",
    )
    # Use a fresh adapter and repository for publication; SQL rows belong to the
    # still-open documents publication transaction, not the staging transaction.
    publisher = _writer(client)
    publisher._chunk_repository = SqlAlchemyIndexingRepository(repository._engine)
    with repository._engine.begin() as connection:
        _store(repository, chunks, connection)
        published = publisher.publish_staged(
            "attempt_1",
            "publication_1",
            connection=connection,
            validator=lambda values: values == chunks,
            fencing_token=1,
            expected_generation_id="generation_1",
        )
    assert published.state == "published"
    assert all(
        len(row["payload"].encode("utf-8")) <= 65535 for row in client.rows[writer.collection_name]
    )
    reader = _writer(client)
    reader._chunk_repository = SqlAlchemyIndexingRepository(repository._engine)
    statements = []
    event.listen(
        repository._engine, "before_cursor_execute", lambda *args: statements.append(args[2])
    )
    page = reader.search("中", ["space_1"], 2, None, generation_id="generation_1")
    assert [item["chunk_id"] for item in page.items] == ["chunk_1", "chunk_2"]
    assert [item["score"] for item in page.items] == [0.95, 0.8999999999999999]
    for chunk, item in zip(chunks, page.items, strict=True):
        assert {key: item[key] for key in chunk.to_mapping()} == chunk.to_mapping()
    if length == 7000:
        assert len([query for query in statements if "FROM index_chunks" in query]) == 1


def test_reference_publish_rejects_validator_and_missing_sql(tmp_path) -> None:
    repository = _repository(tmp_path)
    client = FakeMilvus()
    writer = _writer(client)
    writer._chunk_repository = repository
    chunk = _long_chunk()
    writer.stage_chunks("attempt_1", "publication_1", "document_1", "version_1", (chunk,))
    with pytest.raises(PlatformError) as missing:
        writer.publish_staged("attempt_1", "publication_1")
    assert missing.value.code == "retrieval_degradation"
    with repository._engine.begin() as connection:
        _store(repository, (chunk,), connection)
    with pytest.raises(PlatformError) as rejected:
        writer.publish_staged("attempt_1", "publication_1", validator=lambda chunks: False)
    assert rejected.value.code == "index_release_blocked"
    assert writer.search("中", ["space_1"], 2, None).items == ()
    with pytest.raises(PlatformError) as identity:
        writer.publish_staged("attempt_1", "publication_1", fencing_token=2)
    assert identity.value.code == "fence_conflict"
    writer.publish_staged("attempt_1", "publication_1")
    with repository._engine.begin() as connection:
        connection.execute(delete(index_chunks_table))
        _store(
            repository,
            (
                replace(chunk, generation_id="generation_other"),
                replace(chunk, publication_id="publication_other"),
            ),
            connection,
        )
    with pytest.raises(PlatformError) as wrong_identity:
        writer.search("中", ["space_1"], 2, None)
    assert wrong_identity.value.code == "retrieval_degradation"
    # Cleanup and receipts read scalar identity fields without needing the body.
    assert writer.discard_staged("attempt_1", "publication_1").state == "discarded"


def test_service_failure_and_fresh_worker_restage_publish_long_row(tmp_path) -> None:
    repository = _repository(tmp_path)
    documents_metadata.create_all(repository._engine)
    client = FakeMilvus()
    writer = _writer(client)
    writer._chunk_repository = repository
    service = IndexingService(
        dense_writer=writer,
        sparse_provider=_FailingPublishWriter(provider_name="sparse"),
        generation_manager=SqlAlchemyGenerationManager(repository),
    )
    request = _request()
    content = _long_chunk().text
    output = service.process_and_stage(
        request,
        content,
        media_kind="text/plain",
        content_manifest_id="manifest_1",
        content_manifest_hash="manifest_hash_1",
    )
    assert len(output.chunks) == 1
    with repository._engine.connect() as connection:
        assert connection.execute(select(index_chunks_table)).first() is None
    with pytest.raises(PlatformError) as failed:
        with repository._engine.begin() as connection:
            service.publish(request, connection=connection, receipt=output.receipt)
    assert failed.value.code == "indexing_publish_failed"
    with repository._engine.connect() as connection:
        assert connection.execute(select(index_chunks_table)).first() is None
    assert writer.search("中", ["space_1"], 2, None).items == ()

    # The worker's retry path creates a new attempt and re-runs processing. None
    # of the previous service's in-memory staging state survives here.
    retry_repository = SqlAlchemyIndexingRepository(repository._engine)
    retry_writer = _writer(client)
    retry_writer._chunk_repository = retry_repository
    retry = IndexingService(
        dense_writer=retry_writer,
        generation_manager=SqlAlchemyGenerationManager(retry_repository),
    )
    retry_request = replace(request, attempt_id="attempt_retry", fencing_token=2)
    retried = retry.process_and_stage(
        retry_request,
        content,
        media_kind="text/plain",
        content_manifest_id="manifest_1",
        content_manifest_hash="manifest_hash_1",
    )
    with retry_repository._engine.begin() as connection:
        result = retry.publish(retry_request, connection=connection, receipt=retried.receipt)
    assert result["state"] == "published"
    reader = _writer(client)
    reader._chunk_repository = SqlAlchemyIndexingRepository(repository._engine)
    page = reader.search("中", ["space_1"], 2, None, generation_id="generation_initial")
    assert len(page.items) == 1
    assert page.items[0]["text"] == content
    assert page.items[0]["metadata"]["parent_text"] == content
