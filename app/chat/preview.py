"""Message-owned citation projection used by document previews."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.engine import Engine

from app.documents.preview import PreviewHit
from app.platform.errors import PlatformError

from .schema import chat_conversation_table, chat_message_table


class MessageCitationPreviewPort(Protocol):
    def get_hits(
        self, principal: Any, message_id: str, document_id: str, document_version_id: str
    ) -> tuple[PreviewHit, ...]: ...


class SqlAlchemyMessageCitationPreviewAdapter:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get_hits(
        self, principal: Any, message_id: str, document_id: str, document_version_id: str
    ) -> tuple[PreviewHit, ...]:
        with self._engine.connect() as connection:
            message = (
                connection.execute(
                    select(chat_message_table.c.owner_user_id, chat_message_table.c.citations_json)
                    .join(
                        chat_conversation_table,
                        chat_conversation_table.c.id == chat_message_table.c.conversation_id,
                    )
                    .where(
                        chat_message_table.c.id == message_id,
                        chat_message_table.c.owner_user_id == str(principal.user_id),
                        chat_conversation_table.c.owner_user_id == str(principal.user_id),
                    )
                )
                .mappings()
                .one_or_none()
            )
        if message is None:
            raise PlatformError("message_not_found", "Message was not found", {}, 404)
        hits: list[PreviewHit] = []
        for citation in message["citations_json"] or []:
            if not isinstance(citation, Mapping):
                continue
            if (
                str(citation.get("document_id")) != document_id
                or str(citation.get("document_version_id")) != document_version_id
            ):
                continue
            locator = _preview_locator(citation.get("locator"))
            snippet = citation.get("snippet")
            clean_snippet = snippet.strip() if isinstance(snippet, str) and snippet.strip() else None
            hits.append(
                PreviewHit(
                    index=len(hits) + 1,
                    summary=clean_snippet or _summary_from_locator(locator),
                    locator=locator,
                    snippet=clean_snippet,
                )
            )
        return tuple(hits)


def _preview_locator(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    page = _positive_int(value.get("page"))
    if page is not None:
        result: dict[str, Any] = {"page": page}
        span = _normalized_span(value.get("span"))
        if span is not None:
            result["span"] = span
        return result
    section_path = value.get("section_path")
    if isinstance(section_path, (list, tuple)) and section_path:
        path = [str(part) for part in section_path if str(part)]
        if path:
            result = {"section_path": path}
            paragraph = _positive_int(value.get("paragraph"))
            if paragraph is not None:
                result["paragraph"] = paragraph
            return result
    sheet = value.get("sheet")
    a1_range = value.get("a1_range")
    if isinstance(sheet, str) and sheet and isinstance(a1_range, str) and a1_range:
        return {"sheet": sheet, "a1_range": a1_range}
    return {}


def _summary_from_locator(locator: Mapping[str, Any]) -> str:
    if "page" in locator:
        return f"Page {locator['page']}"
    if "section_path" in locator:
        return " / ".join(locator["section_path"])
    if "sheet" in locator:
        return f"Sheet {locator['sheet']}, range {locator['a1_range']}"
    return "Document citation"


def _normalized_span(value: Any) -> dict[str, int] | None:
    if isinstance(value, Mapping):
        start = _nonnegative_int(value.get("start"))
        end = _nonnegative_int(value.get("end"))
    elif isinstance(value, str) and ":" in value:
        start_text, end_text = value.split(":", maxsplit=1)
        start = _nonnegative_int(start_text)
        end = _nonnegative_int(end_text)
    else:
        return None
    if start is None or end is None or end <= start:
        return None
    return {"start": start, "end": end}


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _nonnegative_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
