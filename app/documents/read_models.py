from __future__ import annotations

from typing import Any

from sqlalchemy import and_, func, select

from app.platform.errors import PlatformError
from app.platform.storage import StorageKeyError

from .domain import DocumentLifecycle, DocumentVersionState, PublicationState
from .schema import (
    document_versions_table,
    documents_table,
    publications_table,
    upload_batch_items_table,
    upload_batches_table,
)
from .service import DocumentsService, _timestamp


class DocumentReadModels:
    """Read-only projections with the domain visibility gates applied."""

    def __init__(self, service: DocumentsService) -> None:
        self._service = service

    def list_versions(self, *, principal: Any, document_id: str) -> dict[str, Any]:
        with self._service._engine.connect() as connection:
            document = (
                connection.execute(
                    select(documents_table).where(documents_table.c.id == document_id)
                )
                .mappings()
                .one_or_none()
            )
            if document is None:
                raise PlatformError("document_not_found", "Document was not found", {}, 404)
            if document["lifecycle_status"] != DocumentLifecycle.ACTIVE.value:
                raise PlatformError("document_unavailable", "Document is not available", {}, 409)
            self._service._authorize(principal, str(document["space_id"]), "manage")
            versions = (
                connection.execute(
                    select(document_versions_table)
                    .where(document_versions_table.c.document_id == document_id)
                    .order_by(document_versions_table.c.version_number.desc())
                )
                .mappings()
                .all()
            )
        return {
            "document_id": document_id,
            "version": document["version"],
            "active_version_id": document["active_version_id"],
            "items": [
                {
                    "document_version_id": row["id"],
                    "version_number": row["version_number"],
                    "status": row["status"],
                    "created_at": _timestamp(row["created_at_utc"]),
                    "activated_at": (
                        _timestamp(row["activated_at_utc"]) if row["activated_at_utc"] else None
                    ),
                    "terminal_at": (
                        _timestamp(row["terminal_at_utc"]) if row["terminal_at_utc"] else None
                    ),
                    "superseded_at": (
                        _timestamp(row["superseded_at_utc"]) if row["superseded_at_utc"] else None
                    ),
                    "purge_after_at": (
                        _timestamp(row["purge_after_at_utc"]) if row["purge_after_at_utc"] else None
                    ),
                    "purged_at": _timestamp(row["purged_at_utc"]) if row["purged_at_utc"] else None,
                    "restored_from_version_id": row["restored_from_version_id"],
                    "content_available": row["status"]
                    not in {DocumentVersionState.PURGING.value, DocumentVersionState.PURGED.value},
                }
                for row in versions
            ],
        }

    def preview(
        self,
        *,
        principal: Any,
        document_id: str,
        document_version_id: str | None = None,
    ) -> dict[str, Any]:
        document, version, publication = self._visible_version(
            principal=principal,
            document_id=document_id,
            document_version_id=document_version_id,
        )
        del publication
        return {
            "document_id": document["id"],
            "document_version_id": version["id"],
            "name": document["name"],
            "media_kind": version["media_kind"],
            "size_bytes": version["size_bytes"],
            "content_available": True,
        }

    def content(
        self,
        *,
        principal: Any,
        document_id: str,
        document_version_id: str | None = None,
    ) -> tuple[bytes, Any]:
        _, version, _ = self._visible_version(
            principal=principal,
            document_id=document_id,
            document_version_id=document_version_id,
        )
        try:
            return self._service._object_store.get(str(version["original_object_key"]))
        except (StorageKeyError, KeyError) as exc:
            raise PlatformError(
                "document_content_unavailable", "Document content is unavailable", {}, 410
            ) from exc

    def get_upload_batch(self, *, principal: Any, upload_batch_id: str) -> dict[str, Any]:
        with self._service._engine.connect() as connection:
            batch = (
                connection.execute(
                    select(upload_batches_table).where(upload_batches_table.c.id == upload_batch_id)
                )
                .mappings()
                .one_or_none()
            )
            if batch is None:
                raise PlatformError("upload_batch_not_found", "Upload batch was not found", {}, 404)
            self._service._authorize(principal, str(batch["space_id"]), "read")
            rows = connection.execute(
                select(upload_batch_items_table.c.result_state, func.count())
                .where(upload_batch_items_table.c.upload_batch_id == upload_batch_id)
                .group_by(upload_batch_items_table.c.result_state)
            ).all()
        counts = {
            "total_files": 0,
            "pending": 0,
            "running": 0,
            "retry_wait": 0,
            "succeeded": 0,
            "failed": 0,
            "cancelled": 0,
            "dead_letter": 0,
            "rejected": 0,
            "deduplicated": 0,
        }
        for state, count in rows:
            counts[str(state)] = int(count)
            counts["total_files"] += int(count)
        return {
            "upload_batch_id": upload_batch_id,
            "state": self._batch_state(counts),
            "summary": counts,
        }

    @staticmethod
    def _batch_state(counts: dict[str, int]) -> str:
        if counts["running"] or counts["retry_wait"]:
            return "running"
        if counts["pending"]:
            return "pending"
        if counts["succeeded"] + counts["deduplicated"] == counts["total_files"]:
            return "succeeded"
        if counts["succeeded"] or counts["deduplicated"]:
            return "partial"
        return "failed"

    def _visible_version(
        self, *, principal: Any, document_id: str, document_version_id: str | None
    ):
        with self._service._engine.begin() as connection:
            row = (
                connection.execute(
                    select(
                        documents_table,
                        document_versions_table,
                        publications_table.c.id.label("publication_id"),
                        publications_table.c.status.label("publication_status"),
                    )
                    .join(
                        document_versions_table,
                        document_versions_table.c.id == documents_table.c.active_version_id,
                    )
                    .join(
                        publications_table,
                        and_(
                            publications_table.c.document_id == documents_table.c.id,
                            publications_table.c.document_version_id
                            == document_versions_table.c.id,
                            publications_table.c.status == PublicationState.ACTIVE.value,
                        ),
                    )
                    .where(documents_table.c.id == document_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                document = (
                    connection.execute(
                        select(documents_table).where(documents_table.c.id == document_id)
                    )
                    .mappings()
                    .one_or_none()
                )
                if document is not None:
                    if document["lifecycle_status"] == DocumentLifecycle.ACTIVE.value:
                        self._service._authorize(principal, str(document["space_id"]), "read")
                raise PlatformError("document_unavailable", "Document is not available", {}, 404)
            self._service._authorize(principal, str(row["space_id"]), "read")
            if row["lifecycle_status"] != DocumentLifecycle.ACTIVE.value:
                raise PlatformError("document_unavailable", "Document is not available", {}, 404)
            requested = document_version_id or row["active_version_id"]
            if requested != row["active_version_id"]:
                raise PlatformError(
                    "document_version_unavailable", "Document version is not available", {}, 404
                )
            self._service._acquire_read_lease(
                connection,
                document_id=str(row["id"]),
                document_version_id=str(row["active_version_id"]),
                principal_id=str(principal.user_id),
            )
            return row, row, {"id": row["publication_id"], "status": row["publication_status"]}


__all__ = ["DocumentReadModels"]
