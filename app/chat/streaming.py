"""Event-first SSE streaming with Last-Event-ID replay and subscription leases."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from sqlalchemy.engine import Engine

from app.platform.errors import PlatformError

from .events import list_events_after
from .leases import (
    DEFAULT_DISCONNECT_GRACE_SECONDS,
    create_lease,
    invalidate_lease,
    renew_lease,
)
from .models import StoredEvent, sse_comment, sse_frame, terminal_event_type
from .ports import ChatAuthorizationPort
from .schema import chat_generation_table


class GenerationStreamService:
    """Authorizes, replays and live-streams one generation's committed events."""

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Any,
        authorization: ChatAuthorizationPort,
        poll_seconds: float = 0.2,
        idle_backoff_seconds: float = 0.5,
        empty_polls_before_backoff: int = 5,
        lease_seconds: int = 90,
        heartbeat_seconds: int = 30,
        disconnect_grace_seconds: int = DEFAULT_DISCONNECT_GRACE_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._authorization = authorization
        self._poll_seconds = poll_seconds
        self._idle_backoff_seconds = idle_backoff_seconds
        self._empty_polls_before_backoff = empty_polls_before_backoff
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._disconnect_grace_seconds = disconnect_grace_seconds
        self._sleep = sleep

    def authorize(self, *, principal: Any, generation_id: str) -> None:
        """Pre-flight authorization for SSE subscription routes.

        Running the same checks that ``_open`` performs before the streaming
        response starts keeps 401/404 failures in the standard error-envelope
        contract instead of breaking an already-committed 200 stream (A13).
        """

        with self._engine.connect() as connection:
            self._authorization.verify_active(connection, principal)
            generation = (
                connection.execute(select_generation_statement(generation_id))
                .mappings()
                .one_or_none()
            )
            if generation is None or str(generation["owner_user_id"]) != str(principal.user_id):
                raise PlatformError("generation_not_found", "Generation was not found", {}, 404)

    async def stream(
        self,
        *,
        principal: Any,
        generation_id: str,
        last_event_id: int = 0,
    ) -> AsyncIterator[str]:
        lease_token = ""
        last_seq = last_event_id
        last_heartbeat = time.monotonic()
        empty_polls = 0
        try:
            replay, lease_token = await asyncio.to_thread(
                self._open, principal, generation_id, last_seq
            )
            for event in replay:
                last_seq = max(last_seq, event.seq)
                yield sse_frame(event.event_type, event.data, event.seq)
            if replay and terminal_event_type(replay[-1].event_type):
                return
            while True:
                # Consecutive empty polls back off to the idle interval to keep
                # long quiet streams off the fast polling cadence; any new
                # event restores it (A36).
                await self._sleep(
                    self._idle_backoff_seconds
                    if empty_polls >= self._empty_polls_before_backoff
                    else self._poll_seconds
                )
                events = await asyncio.to_thread(self._read_events, generation_id, last_seq)
                for event in events:
                    last_seq = max(last_seq, event.seq)
                    yield sse_frame(event.event_type, event.data, event.seq)
                if events and terminal_event_type(events[-1].event_type):
                    return
                empty_polls = 0 if events else empty_polls + 1
                if time.monotonic() - last_heartbeat >= self._heartbeat_seconds:
                    if not await asyncio.to_thread(self._renew_lease, lease_token):
                        return
                    yield sse_comment("keep-alive")
                    last_heartbeat = time.monotonic()
        finally:
            if lease_token:
                await asyncio.to_thread(self._release_lease, lease_token)

    def _open(
        self, principal: Any, generation_id: str, last_seq: int
    ) -> tuple[list[StoredEvent], str]:
        with self._engine.begin() as connection:
            self._authorization.verify_active(connection, principal)
            generation = (
                connection.execute(select_generation_statement(generation_id))
                .mappings()
                .one_or_none()
            )
            if generation is None or str(generation["owner_user_id"]) != str(principal.user_id):
                raise PlatformError("generation_not_found", "Generation was not found", {}, 404)
            now = self._now(connection)
            lease = create_lease(
                connection,
                generation_id=generation_id,
                auth_session_id=str(principal.auth_session_id),
                now=now,
                lease_seconds=self._lease_seconds,
            )
            replay = list_events_after(connection, generation_id=generation_id, after_seq=last_seq)
            return replay, str(lease["lease_token"])

    def _read_events(self, generation_id: str, after_seq: int) -> list[StoredEvent]:
        with self._engine.connect() as connection:
            return list_events_after(connection, generation_id=generation_id, after_seq=after_seq)

    def _renew_lease(self, lease_token: str) -> bool:
        with self._engine.begin() as connection:
            return renew_lease(
                connection,
                lease_token=lease_token,
                now=self._now(connection),
                lease_seconds=self._lease_seconds,
            )

    def _release_lease(self, lease_token: str) -> None:
        with self._engine.begin() as connection:
            invalidate_lease(
                connection,
                lease_token=lease_token,
                now=self._now(connection),
                grace_seconds=self._disconnect_grace_seconds,
            )

    def _now(self, connection: Any) -> Any:
        return self._clock.now_utc(connection)


def select_generation_statement(generation_id: str) -> Any:
    from sqlalchemy import select

    return select(chat_generation_table).where(chat_generation_table.c.id == generation_id)


__all__ = ["GenerationStreamService"]
