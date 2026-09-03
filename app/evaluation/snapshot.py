"""Read-only snapshot of committed chat facts for shadow-evaluation sampling (A9)."""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine

from app.chat.schema import (
    chat_ab_pair_table,
    chat_ab_vote_table,
    chat_conversation_table,
    chat_generation_event_table,
    chat_generation_table,
    chat_message_feedback_table,
    chat_message_table,
)

_MINIMAL_SIGNAL_KEYS = frozenset(
    {
        "weak_has_citation",
        "weak_citation_clicks",
        "weak_feedback_up",
        "weak_feedback_down",
        "weak_ab_vote_count",
    }
)


def _content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class SqlAlchemyChatFactsSnapshot:
    """Collects minimal, ACL-protected real-question samples from chat facts.

    Reads committed ``chat_message`` (role=user) rows scoped by the conversation
    ``scope_json`` and aggregates weak signals (citations / feedback / A/B) for
    those questions. Never writes back to any chat table.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def collect_samples(
        self,
        connection: Connection,
        *,
        space_id: str,
        limit: int,
    ) -> tuple[dict[str, Any], ...]:
        conversations = (
            connection.execute(
                select(chat_conversation_table).where(
                    chat_conversation_table.c.scope_json.is_not(None)
                )
            )
            .mappings()
            .all()
        )
        scoped_conversation_ids: list[str] = []
        for conversation in conversations:
            scope = conversation["scope_json"] or {}
            space_ids = scope.get("space_ids")
            if isinstance(space_ids, list) and space_id in {str(item) for item in space_ids}:
                scoped_conversation_ids.append(str(conversation["id"]))
        if not scoped_conversation_ids:
            return ()

        messages = (
            connection.execute(
                select(chat_message_table)
                .where(
                    chat_message_table.c.conversation_id.in_(scoped_conversation_ids),
                    chat_message_table.c.role == "user",
                )
                .order_by(chat_message_table.c.created_at_utc)
                .limit(limit)
            )
            .mappings()
            .all()
        )
        samples: list[dict[str, Any]] = []
        for position, message in enumerate(messages, start=1):
            question = str(message["content"])
            question_hash = _content_hash(question)
            weak_signals = self._weak_signals(
                connection, message_id=str(message["id"]), space_id=space_id
            )
            samples.append(
                {
                    "item_id": str(message["id"]),
                    "position": position,
                    "question_text": question,
                    "question_hash": question_hash,
                    "evidence_hash": _content_hash(question),
                    "weak_signals": {
                        key: value
                        for key, value in weak_signals.items()
                        if key in _MINIMAL_SIGNAL_KEYS
                    },
                    "source_ref": str(message["id"]),
                }
            )
        return tuple(samples)

    def _weak_signals(
        self, connection: Connection, *, message_id: str, space_id: str
    ) -> dict[str, Any]:
        assistant = (
            connection.execute(
                select(
                    chat_message_table.c.id.label("assistant_message_id"),
                    chat_message_table.c.citations_json,
                    chat_generation_table.c.id.label("generation_id"),
                )
                .join(
                    chat_generation_table,
                    chat_generation_table.c.message_id == chat_message_table.c.id,
                )
                .where(
                    chat_generation_table.c.user_message_id == message_id,
                    chat_generation_table.c.status == "completed",
                    chat_message_table.c.role == "assistant",
                )
                .order_by(chat_generation_table.c.attempt_number.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        signals: dict[str, Any] = {
            "weak_has_citation": bool(assistant and (assistant["citations_json"] or None)),
            "weak_citation_clicks": 0,
            "weak_feedback_up": 0,
            "weak_feedback_down": 0,
            "weak_ab_vote_count": 0,
        }
        if assistant is not None:
            feedback = (
                connection.execute(
                    select(chat_message_feedback_table.c.vote).where(
                        chat_message_feedback_table.c.message_id
                        == assistant["assistant_message_id"]
                    )
                )
                .scalars()
                .all()
            )
            signals["weak_feedback_up"] = sum(1 for vote in feedback if str(vote) == "up")
            signals["weak_feedback_down"] = sum(1 for vote in feedback if str(vote) == "down")
            signals["weak_citation_clicks"] = len(
                connection.execute(
                    select(chat_generation_event_table.c.event_seq).where(
                        chat_generation_event_table.c.generation_id == assistant["generation_id"],
                        chat_generation_event_table.c.event_type == "citation_click",
                    )
                ).all()
            )
            pair_id = connection.execute(
                select(chat_ab_pair_table.c.pair_id).where(
                    chat_ab_pair_table.c.generation_id == assistant["generation_id"],
                    # A/B pairs and votes stay isolated per space (A3).
                    chat_ab_pair_table.c.space_id == space_id,
                )
            ).scalar_one_or_none()
            if pair_id is not None:
                signals["weak_ab_vote_count"] = len(
                    connection.execute(
                        select(chat_ab_vote_table.c.pair_id).where(
                            chat_ab_vote_table.c.pair_id == pair_id
                        )
                    ).all()
                )
        return signals


__all__ = ["SqlAlchemyChatFactsSnapshot"]
