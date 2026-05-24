"""Metadata normalization for indexed documents and chunks."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


class DocumentMetadataNormalizer:
    """Creates stable document and chunk metadata for ingestion."""

    def document_metadata(
        self,
        source_path: str | Path,
        content: str,
        *,
        space_id: str = "default",
    ) -> dict[str, Any]:
        path = Path(source_path).resolve()
        normalized_source_path = path.as_posix()
        extension = path.suffix.lower()
        normalized_space_id = (space_id or "default").strip() or "default"
        document_identity = normalized_source_path.casefold()
        if normalized_space_id != "default":
            document_identity = f"{normalized_space_id}:{document_identity}"

        return {
            "document_id": _sha256(document_identity),
            "space_id": normalized_space_id,
            "source_path": normalized_source_path,
            "content_hash": _sha256(content),
            "extension": extension,
            "file_name": path.name,
            "_source": normalized_source_path,
            "_extension": extension,
            "_file_name": path.name,
        }

    def chunk_metadata(
        self,
        *,
        document_metadata: dict[str, Any],
        chunk_index: int,
        headings: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if chunk_index < 0:
            raise ValueError("chunk_index must be non-negative")

        metadata = dict(document_metadata)
        metadata["chunk_index"] = chunk_index
        metadata["chunk_id"] = f"{document_metadata['document_id']}:{chunk_index:06d}"

        heading_values: list[str] = []
        for key in ("h1", "h2", "h3", "h4"):
            value = (headings or {}).get(key)
            if value:
                metadata[key] = value
                heading_values.append(value)

        metadata["heading_path"] = " > ".join(heading_values)
        return metadata


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
