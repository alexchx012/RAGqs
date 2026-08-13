"""Scoped outbox façade for `calibration_window_suggested` events (A27)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.engine import Connection

from app.outbox.ports import OutboxPublishCommand
from app.outbox.publisher import SUPPORTED_EVENT_SCHEMA_VERSION, SqlAlchemyOutboxPublisher
from app.platform.context import current_context


class SqlAlchemyCalibrationOutboxAdapter:
    def __init__(self, publisher: SqlAlchemyOutboxPublisher) -> None:
        self._publisher = publisher

    def publish_suggested(
        self,
        *,
        suggestion_id: str,
        transition_version: int,
        occurred_at: datetime,
        connection: Connection,
    ) -> str:
        context = current_context()
        command = OutboxPublishCommand(
            event_id=f"evt_calibration_window_suggested_{suggestion_id}_{transition_version}",
            caller_principal="calibration",
            event_type="calibration_window_suggested",
            schema_version=SUPPORTED_EVENT_SCHEMA_VERSION,
            aggregate_type="calibration_window_suggestion",
            aggregate_id=suggestion_id,
            transition_version=transition_version,
            occurred_at=occurred_at,
            payload={"calibration_window_suggestion_id": suggestion_id},
            recipients=(),
            trace_id=context.trace_id if context is not None else None,
        )
        self._publisher._publish_authorized(  # noqa: SLF001 - scoped internal assembly path
            command,
            connection=connection,
            caller="calibration",
        )
        return command.event_id


__all__ = ["SqlAlchemyCalibrationOutboxAdapter"]
