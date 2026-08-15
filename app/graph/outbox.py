"""Scoped outbox façade for `graph_build_completed` terminal events.

The adapter uses the publisher's internal no-token assembly path with the
fixed `knowledge_graph` producer identity, mirroring the ingestion/submission
adapters. Successful events additionally pass the publisher's persisted
activated-receipt verification via the repository-backed verifier.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.engine import Connection

from app.outbox.ports import OutboxPublishCommand, RecipientSelection
from app.outbox.publisher import SUPPORTED_EVENT_SCHEMA_VERSION, SqlAlchemyOutboxPublisher
from app.platform.context import current_context

from .repository import SqlAlchemyGraphRepository


class SqlAlchemyGraphBuildOutboxAdapter:
    def __init__(self, publisher: SqlAlchemyOutboxPublisher) -> None:
        self._publisher = publisher

    def publish_completed(
        self,
        *,
        graph_build_id: str,
        status: str,
        source_revision: int,
        transition_version: int,
        occurred_at: datetime,
        recipient_user_id: str,
        graph_generation_id: str | None = None,
        index_generation_id: str | None = None,
        failure_class: str | None = None,
        connection: Connection,
    ) -> str:
        payload: dict[str, object] = {
            "graph_build_id": graph_build_id,
            "status": status,
            "source_revision": str(source_revision),
            "graph_generation_id": graph_generation_id,
            "index_generation_id": index_generation_id,
            "failure_class": failure_class,
        }
        context = current_context()
        command = OutboxPublishCommand(
            event_id=f"evt_graph_build_completed_{graph_build_id}_{transition_version}",
            caller_principal="knowledge_graph",
            event_type="graph_build_completed",
            schema_version=SUPPORTED_EVENT_SCHEMA_VERSION,
            aggregate_type="graph_build_run",
            aggregate_id=graph_build_id,
            transition_version=transition_version,
            occurred_at=occurred_at,
            payload=payload,
            recipients=(
                RecipientSelection(
                    recipient_user_id=recipient_user_id,
                    recipient_kind="identity",
                    selection_reason="graph_build_run_initiator",
                ),
            ),
            trace_id=context.trace_id if context is not None else None,
        )
        self._publisher._publish_authorized(  # noqa: SLF001 - scoped internal assembly path
            command,
            connection=connection,
            caller="knowledge_graph",
        )
        return command.event_id


class RepositoryActivatedReceiptVerifier:
    """Verifies the persisted activated indexing receipt for success events."""

    def __init__(self, repository: SqlAlchemyGraphRepository) -> None:
        self._repository = repository

    def verify_activated_receipt(
        self,
        *,
        aggregate_id: str,
        graph_generation_id: str,
        connection: Connection,
    ) -> bool:
        return self._repository.verified_activated_receipt(
            aggregate_id=aggregate_id,
            graph_generation_id=graph_generation_id,
            connection=connection,
        )


__all__ = ["RepositoryActivatedReceiptVerifier", "SqlAlchemyGraphBuildOutboxAdapter"]
