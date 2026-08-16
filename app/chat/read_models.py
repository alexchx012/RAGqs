"""Conversation detail read model with assistant state, feedback and A/B projection."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Connection

from app.platform.errors import PlatformError

from .models import datetime_to_rfc3339
from .schema import (
    chat_ab_candidate_table,
    chat_ab_pair_table,
    chat_conversation_table,
    chat_message_feedback_table,
    chat_message_table,
)


def conversation_detail(
    connection: Connection, *, conversation_id: str, user_id: str
) -> dict[str, Any]:
    conversation = (
        connection.execute(
            select(chat_conversation_table).where(chat_conversation_table.c.id == conversation_id)
        )
        .mappings()
        .one_or_none()
    )
    if conversation is None or str(conversation["owner_user_id"]) != user_id:
        raise PlatformError("conversation_not_found", "Conversation was not found", {}, 404)
    messages = (
        connection.execute(
            select(chat_message_table)
            .where(chat_message_table.c.conversation_id == conversation_id)
            .order_by(chat_message_table.c.created_at_utc)
        )
        .mappings()
        .all()
    )
    message_ids = [str(row["id"]) for row in messages]
    feedback_rows = {
        str(row["message_id"]): dict(row)
        for row in connection.execute(
            select(chat_message_feedback_table).where(
                chat_message_feedback_table.c.message_id.in_(message_ids),
                chat_message_feedback_table.c.voter_user_id == user_id,
            )
        )
        .mappings()
        .all()
    }
    pair_rows = {
        str(row["message_id"]): dict(row)
        for row in connection.execute(
            select(chat_ab_pair_table).where(chat_ab_pair_table.c.message_id.in_(message_ids))
        )
        .mappings()
        .all()
    }
    published_candidates: dict[str, dict[int, Mapping[str, Any]]] = {}
    pair_ids = [str(row["pair_id"]) for row in pair_rows.values()]
    if pair_ids:
        for row in connection.execute(
            select(chat_ab_candidate_table).where(chat_ab_candidate_table.c.pair_id.in_(pair_ids))
        ).mappings():
            by_pair = published_candidates.setdefault(str(row["pair_id"]), {})
            by_pair[int(row["candidate"])] = dict(row)
    return {
        "id": str(conversation["id"]),
        "title": str(conversation["title"]),
        "effort_level": str(conversation["effort_level"]),
        "scope": dict(conversation["scope_json"]),
        "messages": [
            _message_projection(
                dict(row),
                feedback=feedback_rows.get(str(row["id"])),
                pair=pair_rows.get(str(row["id"])),
                candidates=_candidates_for(published_candidates, pair_rows.get(str(row["id"]))),
            )
            for row in messages
        ],
    }


def _candidates_for(
    published: Mapping[str, Mapping[int, Mapping[str, Any]]],
    pair: Mapping[str, Any] | None,
) -> Mapping[int, Mapping[str, Any]] | None:
    if pair is None:
        return None
    return published.get(str(pair["pair_id"])) or {}


def _message_projection(
    row: Mapping[str, Any],
    *,
    feedback: Mapping[str, Any] | None,
    pair: Mapping[str, Any] | None,
    candidates: Mapping[int, Mapping[str, Any]] | None,
) -> dict[str, Any]:
    if str(row["role"]) == "user":
        return {
            "id": str(row["id"]),
            "role": "user",
            "content": str(row["content"]),
            "created_at": datetime_to_rfc3339(row["created_at_utc"]),
        }
    ab: Any = _ab_projection(pair, candidates)
    if ab is None and pair is not None and str(pair["status"]) == "expired":
        candidates = candidates or {}
        candidate = candidates.get(0)
        if candidate is not None and str(candidate["status"]) == "published":
            content = str(candidate["content"])
            answer_mode = str(candidate["answer_mode"])
            row = {**row, "content": content, "answer_mode": answer_mode}
    feedback_value: Any = None
    if ab is None or bool(ab.get("voted")):
        if feedback is not None:
            feedback_value = {"vote": str(feedback["vote"])}
            if feedback.get("down_reason") is not None:
                feedback_value["down_reason"] = str(feedback["down_reason"])
    value: dict[str, Any] = {
        "id": str(row["id"]),
        "role": "assistant",
        "content": str(row["content"]),
        "answer_mode": row["answer_mode"],
        "effort_level": row["effort_level"],
        "generation_id": row["generation_id"],
        "root_generation_id": row["root_generation_id"],
        "retry_of_generation_id": row["retry_of_generation_id"],
        "attempt_number": row["attempt_number"],
        "status": row["status"],
        "stop_reason": row["stop_reason"],
        "notices": list(row["notices_json"] or []),
        "citations": list(row["citations_json"] or []),
        "feedback": feedback_value,
        "ab": ab,
        "created_at": datetime_to_rfc3339(row["created_at_utc"]),
    }
    return value


def _ab_projection(
    pair: Mapping[str, Any] | None,
    candidates: Mapping[int, Mapping[str, Any]] | None,
) -> Any:
    if pair is None:
        return None
    status = str(pair["status"])
    if status == "pending":
        published = sorted(
            int(candidate)
            for candidate, row in (candidates or {}).items()
            if str(row["status"]) == "published"
        )
        return {
            "pair_id": str(pair["pair_id"]),
            "status": "pending",
            "voted": False,
            "choice": None,
            "candidates": published,
        }
    if status == "open":
        return {
            "pair_id": str(pair["pair_id"]),
            "status": "open",
            "voted": False,
            "choice": None,
            "candidates": [0, 1],
        }
    if status == "voted":
        return {
            "pair_id": str(pair["pair_id"]),
            "status": "voted",
            "voted": True,
            "choice": str(pair["choice"]),
            "candidates": None,
        }
    return None


__all__ = ["conversation_detail"]
