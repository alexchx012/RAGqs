"""Value objects and rendering projection for document previews."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from app.platform.storage import ObjectMetadata


@dataclass(frozen=True, slots=True)
class PreviewMetadata:
    has_text_layer: bool
    tree_indexed: bool
    page_count: int | None
    sheets: tuple[Mapping[str, Any], ...] | None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "has_text_layer": self.has_text_layer,
            "tree_indexed": self.tree_indexed,
            "page_count": self.page_count,
            "sheets": [dict(sheet) for sheet in self.sheets] if self.sheets is not None else None,
        }


@dataclass(frozen=True, slots=True)
class PreviewHit:
    index: int
    summary: str
    locator: Mapping[str, Any]
    snippet: str | None = None

    def to_mapping(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "index": self.index,
            "summary": self.summary,
            "locator": dict(self.locator),
        }
        if self.snippet:
            value["snippet"] = self.snippet
        return value


@dataclass(frozen=True, slots=True)
class PreviewContent:
    body: bytes
    metadata: ObjectMetadata


class PreviewRendererPort(Protocol):
    def metadata(self, *, processing_summary: Mapping[str, Any]) -> PreviewMetadata: ...


class ProcessingReceiptPreviewRenderer:
    """Projects only persisted receipt facts needed by the browser preview."""

    def metadata(self, *, processing_summary: Mapping[str, Any]) -> PreviewMetadata:
        page_count = processing_summary.get("page_count")
        if isinstance(page_count, bool) or not isinstance(page_count, int) or page_count < 1:
            page_count = None
        tree = processing_summary.get("tree")
        tree_indexed = bool(tree.get("tree_indexed")) if isinstance(tree, Mapping) else False
        sheet_manifest = processing_summary.get("sheet_manifest")
        row_groups = processing_summary.get("row_groups")
        row_counts: dict[str, int] = {}
        if isinstance(row_groups, list):
            for group in row_groups:
                if not isinstance(group, Mapping):
                    continue
                sheet = group.get("sheet")
                end = group.get("end")
                if isinstance(sheet, str) and sheet and isinstance(end, int) and not isinstance(end, bool):
                    row_counts[sheet] = max(row_counts.get(sheet, 0), end)
        sheets: list[dict[str, Any]] = []
        if isinstance(sheet_manifest, list):
            for item in sheet_manifest:
                if not isinstance(item, Mapping):
                    continue
                sheet = item.get("sheet")
                if isinstance(sheet, str) and sheet:
                    sheets.append({"name": sheet, "row_count": row_counts.get(sheet, 0)})
        return PreviewMetadata(
            has_text_layer=bool(processing_summary.get("has_text_layer")),
            tree_indexed=tree_indexed,
            page_count=page_count,
            sheets=tuple(sheets) if sheets else None,
        )
