from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import Engine, select

from app.documents.schema import document_versions_table, documents_table, publications_table

from .models import RetrievalScope

COLD_START_LIBRARY_PROFILE = "cold-start"


@dataclass(frozen=True, slots=True)
class DocumentProfile:
    """Stable processing defaults selected before a document is processed."""

    profile_id: str
    media_category: str
    processing_route: str

    @property
    def config_version(self) -> str:
        return f"document-profile:{self.profile_id}:v1"

    def to_mapping(self) -> dict[str, str]:
        return {
            "id": self.profile_id,
            "media_category": self.media_category,
            "processing_route": self.processing_route,
            "config_version": self.config_version,
        }


def document_profile_for_media_kind(media_kind: str) -> DocumentProfile:
    """Classify normalized input media without inspecting processing outcomes."""

    kind = media_kind.strip().casefold()
    if kind in {"text/csv", "csv"} or kind.endswith("csv"):
        return DocumentProfile("structured-table-csv", "structured_table", "csv-chunking")
    if kind in {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
        "xlsx",
        "excel",
    }:
        return DocumentProfile("structured-table-workbook", "structured_table", "xlsx-chunking")
    if kind in {
        "application/pdf",
        "pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "docx",
        "word",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "ppt",
        "pptx",
        "powerpoint",
        "text/html",
        "html",
        "url",
        "web_url",
    }:
        return DocumentProfile("layout-document", "layout_document", "mineru-pipeline")
    if kind.startswith("image/") or kind in {"image", "图片"}:
        return DocumentProfile("image", "image", "image-vlm")
    if kind in {
        "application/json",
        "json",
        "application/yaml",
        "text/yaml",
        "yaml",
        "application/toml",
        "toml",
        "application/xml",
        "text/xml",
        "xml",
    }:
        return DocumentProfile("structured-data", "structured_data", "structured-chunking")
    if kind in {"text/x-python", "python", "code", "text/javascript", "javascript"}:
        return DocumentProfile("code", "code", "code-chunking")
    return DocumentProfile("text", "text", "text-chunking")


def library_profile_id(profiles: Iterable[DocumentProfile | str]) -> str:
    counts = Counter(
        profile.profile_id if isinstance(profile, DocumentProfile) else str(profile)
        for profile in profiles
    )
    if not counts:
        return COLD_START_LIBRARY_PROFILE
    highest = max(counts.values())
    winners = [profile_id for profile_id, count in counts.items() if count == highest]
    return winners[0] if len(winners) == 1 else COLD_START_LIBRARY_PROFILE


class SqlAlchemyLibraryProfileResolver:
    """Derive a single request-time library profile from active publication facts."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def __call__(self, scope: RetrievalScope) -> str:
        return self.resolve(scope)

    def resolve(self, scope: RetrievalScope) -> str:
        if scope.is_empty:
            return COLD_START_LIBRARY_PROFILE
        statement = (
            select(
                documents_table.c.id,
                documents_table.c.space_id,
                document_versions_table.c.media_kind,
            )
            .select_from(
                documents_table.join(
                    document_versions_table,
                    documents_table.c.active_version_id == document_versions_table.c.id,
                ).join(
                    publications_table,
                    publications_table.c.document_version_id == document_versions_table.c.id,
                )
            )
            .where(
                documents_table.c.lifecycle_status == "active",
                document_versions_table.c.status == "active",
                publications_table.c.status == "active",
                documents_table.c.space_id.in_(scope.space_ids),
            )
            .distinct()
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return library_profile_id(
            document_profile_for_media_kind(str(row["media_kind"] or "text/plain"))
            for row in rows
            if scope.allows(space_id=str(row["space_id"]), document_id=str(row["id"]))
        )


__all__ = [
    "COLD_START_LIBRARY_PROFILE",
    "DocumentProfile",
    "SqlAlchemyLibraryProfileResolver",
    "document_profile_for_media_kind",
    "library_profile_id",
]
