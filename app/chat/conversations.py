"""Conversation and conversation-group ownership, search and mutations."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import and_, select, update
from sqlalchemy.engine import Connection, Engine

from app.platform.errors import PlatformError

from .read_models import conversation_detail
from .schema import (
    chat_ab_candidate_table,
    chat_ab_pair_table,
    chat_ab_vote_table,
    chat_conversation_group_table,
    chat_conversation_table,
    chat_generation_event_table,
    chat_generation_execution_table,
    chat_generation_table,
    chat_message_feedback_table,
    chat_message_table,
    chat_subscription_lease_table,
)


def _conversation_id() -> str:
    return f"conv_{uuid.uuid4().hex}"


def _group_id() -> str:
    return f"grp_{uuid.uuid4().hex}"


class ConversationService:
    def __init__(self, engine: Engine, *, now: Any = None) -> None:
        self._engine = engine
        self._now = now

    def _current_time(self, connection: Connection) -> datetime:
        if self._now is not None:
            value = (
                self._now.now_utc(connection)
                if callable(self._now.now_utc)
                else self._now(connection)
            )
            return (
                value
                if value.tzinfo is not None
                else value.replace(tzinfo=datetime.now().astimezone().tzinfo)
            )
        return datetime.now().astimezone()

    def get_conversation_detail(self, *, conversation_id: str, user_id: str) -> dict[str, Any]:
        with self._engine.connect() as connection:
            return conversation_detail(connection, conversation_id=conversation_id, user_id=user_id)

    def _require_owned_conversation(
        self, connection: Connection, *, conversation_id: str, user_id: str
    ) -> Mapping[str, Any]:
        row = (
            connection.execute(
                select(chat_conversation_table).where(
                    chat_conversation_table.c.id == conversation_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None or str(row["owner_user_id"]) != user_id:
            raise PlatformError("conversation_not_found", "Conversation was not found", {}, 404)
        return dict(row)

    def _require_owned_group(
        self, connection: Connection, *, group_id: str, user_id: str
    ) -> Mapping[str, Any]:
        row = (
            connection.execute(
                select(chat_conversation_group_table).where(
                    chat_conversation_group_table.c.id == group_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None or str(row["owner_user_id"]) != user_id:
            raise PlatformError(
                "conversation_group_not_found", "Conversation group was not found", {}, 404
            )
        return dict(row)

    def list_conversations(
        self, *, user_id: str, query: str | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        with self._engine.begin() as connection:
            statement = select(chat_conversation_table).where(
                chat_conversation_table.c.owner_user_id == user_id
            )
            if query:
                statement = statement.where(chat_conversation_table.c.title.ilike(f"%{query}%"))
            rows = (
                connection.execute(
                    statement.order_by(chat_conversation_table.c.last_active_at_utc.desc())
                )
                .mappings()
                .all()
            )
            groups = (
                connection.execute(
                    select(chat_conversation_group_table)
                    .where(chat_conversation_group_table.c.owner_user_id == user_id)
                    .order_by(chat_conversation_group_table.c.created_at_utc)
                )
                .mappings()
                .all()
            )
            return {
                "items": [self._summary(row) for row in rows],
                "groups": [{"id": row["id"], "name": row["name"]} for row in groups],
            }

    def create_conversation(self, *, user_id: str) -> dict[str, Any]:
        with self._engine.begin() as connection:
            now = self._current_time(connection)
            row = {
                "id": _conversation_id(),
                "owner_user_id": user_id,
                "title": "",
                "pinned": False,
                "group_id": None,
                "effort_level": "quick",
                "scope_json": {},
                "last_active_at_utc": now,
                "created_at_utc": now,
                "updated_at_utc": now,
            }
            connection.execute(chat_conversation_table.insert().values(**row))
            return self._summary(row)

    def patch_conversation(
        self,
        *,
        user_id: str,
        conversation_id: str,
        title: Any = None,
        pinned: Any = None,
        group_id: Any = None,
    ) -> dict[str, Any]:
        with self._engine.begin() as connection:
            existing = self._require_owned_conversation(
                connection, conversation_id=conversation_id, user_id=user_id
            )
            now = self._current_time(connection)
            values: dict[str, Any] = {"updated_at_utc": now}
            if title is not None:
                if not isinstance(title, str) or not title.strip():
                    raise PlatformError(
                        "validation_error",
                        "title must be a non-empty string",
                        {"field": "title"},
                        422,
                    )
                if len(title) > 512:
                    raise PlatformError(
                        "validation_error", "title is too long", {"field": "title"}, 422
                    )
                values["title"] = title.strip()
            if pinned is not None:
                if not isinstance(pinned, bool):
                    raise PlatformError(
                        "validation_error", "pinned must be a boolean", {"field": "pinned"}, 422
                    )
                values["pinned"] = pinned
            if group_id is not None:
                if not isinstance(group_id, str) or not group_id.strip():
                    raise PlatformError(
                        "validation_error",
                        "group_id must be a string or null",
                        {"field": "group_id"},
                        422,
                    )
                self._require_owned_group(connection, group_id=group_id, user_id=user_id)
                values["group_id"] = group_id
            elif group_id == "":
                values["group_id"] = None
            connection.execute(
                update(chat_conversation_table)
                .where(chat_conversation_table.c.id == conversation_id)
                .values(**values)
            )
            updated = {**existing, **values}
            return self._summary(updated)

    def delete_conversation(self, *, user_id: str, conversation_id: str) -> None:
        with self._engine.begin() as connection:
            self._require_owned_conversation(
                connection, conversation_id=conversation_id, user_id=user_id
            )
            generation_ids = [
                str(row["id"])
                for row in connection.execute(
                    select(chat_generation_table.c.id).where(
                        chat_generation_table.c.conversation_id == conversation_id
                    )
                ).mappings()
            ]
            for generation_id in generation_ids:
                connection.execute(
                    chat_subscription_lease_table.delete().where(
                        chat_subscription_lease_table.c.generation_id == generation_id
                    )
                )
                connection.execute(
                    chat_generation_event_table.delete().where(
                        chat_generation_event_table.c.generation_id == generation_id
                    )
                )
                connection.execute(
                    chat_generation_execution_table.delete().where(
                        chat_generation_execution_table.c.generation_id == generation_id
                    )
                )
            message_ids = [
                str(row["id"])
                for row in connection.execute(
                    select(chat_message_table.c.id).where(
                        chat_message_table.c.conversation_id == conversation_id
                    )
                ).mappings()
            ]
            for message_id in message_ids:
                connection.execute(
                    chat_message_feedback_table.delete().where(
                        chat_message_feedback_table.c.message_id == message_id
                    )
                )
                pair_ids = [
                    str(row["pair_id"])
                    for row in connection.execute(
                        select(chat_ab_pair_table.c.pair_id).where(
                            chat_ab_pair_table.c.message_id == message_id
                        )
                    ).mappings()
                ]
                for pair_id in pair_ids:
                    connection.execute(
                        chat_ab_vote_table.delete().where(chat_ab_vote_table.c.pair_id == pair_id)
                    )
                    connection.execute(
                        chat_ab_candidate_table.delete().where(
                            chat_ab_candidate_table.c.pair_id == pair_id
                        )
                    )
                    connection.execute(
                        chat_ab_pair_table.delete().where(chat_ab_pair_table.c.pair_id == pair_id)
                    )
            connection.execute(
                chat_generation_table.delete().where(
                    chat_generation_table.c.conversation_id == conversation_id
                )
            )
            connection.execute(
                chat_message_table.delete().where(
                    chat_message_table.c.conversation_id == conversation_id
                )
            )
            connection.execute(
                chat_conversation_table.delete().where(
                    chat_conversation_table.c.id == conversation_id
                )
            )

    def create_group(self, *, user_id: str, name: str) -> dict[str, Any]:
        with self._engine.begin() as connection:
            self._validate_group_name(name)
            now = self._current_time(connection)
            row = {
                "id": _group_id(),
                "owner_user_id": user_id,
                "name": name.strip(),
                "created_at_utc": now,
                "updated_at_utc": now,
            }
            connection.execute(chat_conversation_group_table.insert().values(**row))
            return {"id": row["id"], "name": row["name"]}

    def patch_group(self, *, user_id: str, group_id: str, name: str) -> dict[str, Any]:
        with self._engine.begin() as connection:
            self._validate_group_name(name)
            self._require_owned_group(connection, group_id=group_id, user_id=user_id)
            now = self._current_time(connection)
            connection.execute(
                update(chat_conversation_group_table)
                .where(chat_conversation_group_table.c.id == group_id)
                .values(name=name.strip(), updated_at_utc=now)
            )
            return {"id": group_id, "name": name.strip()}

    def delete_group(self, *, user_id: str, group_id: str) -> None:
        with self._engine.begin() as connection:
            self._require_owned_group(connection, group_id=group_id, user_id=user_id)
            now = self._current_time(connection)
            connection.execute(
                update(chat_conversation_table)
                .where(
                    and_(
                        chat_conversation_table.c.group_id == group_id,
                        chat_conversation_table.c.owner_user_id == user_id,
                    )
                )
                .values(group_id=None, updated_at_utc=now)
            )
            connection.execute(
                chat_conversation_group_table.delete().where(
                    chat_conversation_group_table.c.id == group_id
                )
            )

    @staticmethod
    def _validate_group_name(name: Any) -> None:
        if not isinstance(name, str) or not name.strip():
            raise PlatformError(
                "validation_error", "name must be a non-empty string", {"field": "name"}, 422
            )
        if len(name) > 256:
            raise PlatformError("validation_error", "name is too long", {"field": "name"}, 422)

    @staticmethod
    def _summary(row: Any) -> dict[str, Any]:
        from .models import datetime_to_rfc3339

        return {
            "id": row["id"],
            "title": row["title"],
            "pinned": bool(row["pinned"]),
            "group_id": row["group_id"],
            "last_active_at": datetime_to_rfc3339(row["last_active_at_utc"]),
        }
