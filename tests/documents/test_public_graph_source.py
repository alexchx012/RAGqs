from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select

from app.documents.indexing import NoopIndexingHandoff
from app.documents.public_graph import (
    PublicGraphSourceService,
    PublicGraphSourceSnapshot,
)
from app.documents.schema import documents_metadata, public_graph_source_consumers_table
from app.documents.service import DocumentsService, DocumentUpload
from app.identity.service import AuthPrincipal
from app.outbox.ports import DocumentNotificationRedactionReceipt
from app.outbox.publisher import (
    SqlAlchemyOutboxPublisher,
    SqlAlchemyPublicGraphSourceOutboxAdapter,
)
from app.outbox.schema import outbox_delivery_table, outbox_event_table, outbox_metadata
from app.platform.errors import PlatformError
from app.platform.storage import MemoryObjectStore

from .test_commands import _accept


class _PublicIdentity:
    def authorize_space(self, *, principal, space_id: str, action: str) -> str:
        assert principal.user_id == "user_1"
        assert space_id == "public"
        assert action in {"manage", "contribute", "read"}
        return "manage"


class _Lifecycle:
    def redact_document_notifications(self, command, *, connection):
        del connection
        return DocumentNotificationRedactionReceipt(
            operation_id=command.operation_id,
            deletion_id=command.deletion_id,
            state="completed",
            redacted_notification_count=0,
            already_redacted_count=0,
        )


class _RecordingSourceOutbox:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def publish_public_graph_source_change(self, **event) -> str:
        self.events.append(event)
        return f"event-{event['source_revision']}"


@pytest.fixture()
def source():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    documents_metadata.create_all(engine)
    return PublicGraphSourceService(
        engine,
        trusted_consumers={"indexing": {"indexer_1"}, "public_graph": {"graph_1"}},
        outbox_port=_RecordingSourceOutbox(),
    )


def test_public_source_change_emits_a_transactional_outbox_event() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    documents_metadata.create_all(engine)
    outbox = _RecordingSourceOutbox()
    source = PublicGraphSourceService(engine, outbox_port=outbox)

    snapshot = source.record_source_change(
        space_id="public",
        document_id="doc_1",
        change_type="publish",
        publications=[],
    )

    assert len(outbox.events) == 1
    event = outbox.events[0]
    assert event["source_revision"] == snapshot.source_revision
    assert event["source_manifest_id"] == snapshot.source_manifest_id
    assert event["source_manifest_hash"] == snapshot.source_manifest_hash
    assert event["document_id"] == "doc_1"
    assert event["change_type"] == "publish"
    assert event["connection"] is not None


def test_public_source_change_persists_one_outbox_fact_without_notification_delivery() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    documents_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    publisher = SqlAlchemyOutboxPublisher(engine, now=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    source = PublicGraphSourceService(
        engine,
        outbox_port=SqlAlchemyPublicGraphSourceOutboxAdapter(publisher),
    )

    snapshot = source.record_source_change(
        space_id="public",
        document_id="doc_1",
        change_type="publish",
        publications=[],
    )

    with engine.connect() as connection:
        event = connection.execute(select(outbox_event_table)).mappings().one()
        assert event["event_type"] == "public_graph_source_changed"
        assert event["transition_version"] == snapshot.source_revision
        assert connection.execute(select(outbox_delivery_table)).all() == []


def test_public_source_revision_is_immutable_and_head_validates(source) -> None:
    recorded = source.record_source_change(
        space_id="public",
        document_id="doc_1",
        change_type="publish",
        publications=[
            {
                "document_id": "doc_1",
                "document_version_id": "version_1",
                "publication_id": "publication_1",
                "content_manifest_id": "manifest_1",
                "content_manifest_hash": "hash_1",
            }
        ],
    )
    assert recorded.source_revision == 1
    loaded = source.get_snapshot(source_revision=1)
    assert isinstance(loaded, PublicGraphSourceSnapshot)
    assert loaded.publications[0]["document_id"] == "doc_1"
    head = source.get_current_head()
    assert (
        source.validate_current_head(
            source_revision=head.source_revision,
            source_manifest_hash=head.source_manifest_hash,
            source_head_fence=head.source_head_fence,
        ).current
        is True
    )

    with pytest.raises(PlatformError) as error:
        source.validate_current_head(
            source_revision=1,
            source_manifest_hash="wrong",
            source_head_fence=head.source_head_fence,
        )
    assert error.value.code == "graph_source_changed"


def test_consumer_ack_is_idempotent_and_discard_is_terminal(source) -> None:
    recorded = source.record_source_change(
        space_id="public",
        document_id="doc_1",
        change_type="publish",
        publications=[],
    )
    held = source.acknowledge_consumption(
        consumer_kind="indexing",
        consumer_id="indexer_1",
        source_revision=recorded.source_revision,
        source_manifest_hash=recorded.source_manifest_hash,
        purpose="stage",
        operation_id="ack_1",
    )
    assert held.state == "held"
    assert (
        source.acknowledge_consumption(
            consumer_kind="indexing",
            consumer_id="indexer_1",
            source_revision=recorded.source_revision,
            source_manifest_hash=recorded.source_manifest_hash,
            purpose="stage",
            operation_id="ack_1",
        )
        == held
    )
    discarded = source.acknowledge_consumption(
        consumer_kind="indexing",
        consumer_id="indexer_1",
        source_revision=recorded.source_revision,
        source_manifest_hash=recorded.source_manifest_hash,
        purpose="discard",
        operation_id="ack_2",
    )
    assert discarded.state == "discarded"


def test_discard_releases_every_hold_for_the_consumer_and_source(source) -> None:
    recorded = source.record_source_change(
        space_id="public",
        document_id="doc_1",
        change_type="publish",
        publications=[],
    )
    source.acknowledge_consumption(
        consumer_kind="indexing",
        consumer_id="indexer_1",
        source_revision=recorded.source_revision,
        source_manifest_hash=recorded.source_manifest_hash,
        purpose="stage",
        operation_id="stage_1",
    )
    source.acknowledge_consumption(
        consumer_kind="indexing",
        consumer_id="indexer_1",
        source_revision=recorded.source_revision,
        source_manifest_hash=recorded.source_manifest_hash,
        purpose="release",
        operation_id="release_1",
        source_head_fence=source.get_current_head().source_head_fence,
    )
    source.acknowledge_consumption(
        consumer_kind="indexing",
        consumer_id="indexer_1",
        source_revision=recorded.source_revision,
        source_manifest_hash=recorded.source_manifest_hash,
        purpose="discard",
        operation_id="discard_1",
    )

    with source._engine.connect() as connection:
        states = (
            connection.execute(
                select(public_graph_source_consumers_table.c.state).where(
                    public_graph_source_consumers_table.c.source_revision
                    == recorded.source_revision
                )
            )
            .scalars()
            .all()
        )
    assert set(states) == {"discarded"}


def test_unknown_consumer_is_rejected_and_stale_release_cannot_activate(source) -> None:
    first = source.record_source_change(
        space_id="public",
        document_id="doc_1",
        change_type="publish",
        publications=[],
    )
    with pytest.raises(PlatformError) as error:
        source.acknowledge_consumption(
            consumer_kind="indexing",
            consumer_id="unknown-indexer",
            source_revision=first.source_revision,
            source_manifest_hash=first.source_manifest_hash,
            purpose="stage",
            operation_id="unknown-stage",
        )
    assert error.value.code == "consumer_not_trusted"

    source.acknowledge_consumption(
        consumer_kind="indexing",
        consumer_id="indexer_1",
        source_revision=first.source_revision,
        source_manifest_hash=first.source_manifest_hash,
        purpose="stage",
        operation_id="stage-before-change",
    )
    first_head = source.get_current_head()
    source.record_source_change(
        space_id="public",
        document_id="doc_2",
        change_type="publish",
        publications=[],
    )

    with pytest.raises(PlatformError) as error:
        source.acknowledge_consumption(
            consumer_kind="indexing",
            consumer_id="indexer_1",
            source_revision=first.source_revision,
            source_manifest_hash=first.source_manifest_hash,
            purpose="release",
            operation_id="release-stale",
            source_head_fence=first_head.source_head_fence,
        )
    assert error.value.code == "graph_source_changed"


def test_public_snapshot_contains_every_current_active_publication() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    documents_metadata.create_all(engine)
    source_outbox = _RecordingSourceOutbox()
    source = PublicGraphSourceService(engine, outbox_port=source_outbox)
    service = DocumentsService(
        engine,
        now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        object_store=MemoryObjectStore(),
        identity_access=_PublicIdentity(),
        indexing_handoff_port=NoopIndexingHandoff(),
        public_graph_source_service=source,
    )
    principal = AuthPrincipal(
        user_id="user_1",
        auth_session_id="session_1",
        username="alice",
        role="user",
        department_id=None,
    )
    first = service.create_initial_upload(
        principal=principal,
        space_id="public",
        files=[DocumentUpload(filename="first.txt", content=b"first", media_kind="text/plain")],
        idempotency_key="upload-1",
    )["items"][0]
    _accept(service, principal, first)
    second = service.create_initial_upload(
        principal=principal,
        space_id="public",
        files=[DocumentUpload(filename="second.txt", content=b"second", media_kind="text/plain")],
        idempotency_key="upload-2",
    )["items"][0]
    _accept(service, principal, second)

    snapshot = source.get_current_head()
    published = source.get_snapshot(
        source_revision=snapshot.source_revision
    )
    assert {item["document_id"] for item in published.publications} == {
        first["document_id"],
        second["document_id"],
    }

    service._lifecycle_port = _Lifecycle()
    service.delete_document(
        principal=principal,
        document_id=second["document_id"],
        expected_version=1,
        idempotency_key="delete-1",
        capability_token="token",
    )
    after_delete = source.get_snapshot(source_revision=source.get_current_head().source_revision)
    assert [item["document_id"] for item in after_delete.publications] == [first["document_id"]]
    assert [event["change_type"] for event in source_outbox.events] == [
        "publish",
        "publish",
        "delete",
    ]
