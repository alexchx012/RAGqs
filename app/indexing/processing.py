from __future__ import annotations

import ast
import csv
import hashlib
import io
import json
import re
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol
from xml.etree import ElementTree

from app.documents.indexing import IndexProcessingReceipt, IndexStagingRequest
from app.platform.errors import PlatformError

from .models import IndexChunk


class CompressionPort(Protocol):
    def compress(self, text: str, *, context: Mapping[str, Any]) -> str: ...


class IdentityCompression:
    def compress(self, text: str, *, context: Mapping[str, Any]) -> str:
        del context
        return text.strip()


@dataclass(frozen=True, slots=True)
class OCRSamplePlan:
    page_count: int
    pages: tuple[int, ...]

    @classmethod
    def for_page_count(cls, page_count: int) -> OCRSamplePlan:
        pages: tuple[int, ...]
        if page_count < 1:
            return cls(0, ())
        if page_count <= 10:
            pages = (1, page_count) if page_count > 1 else (1,)
        elif page_count <= 50:
            pages = (1, 2, max(1, page_count // 2), max(1, page_count // 2 + 1), page_count)
        elif page_count <= 200:
            pages = (
                tuple(range(1, 6))
                + tuple(max(1, page_count // 2 - 1 + offset) for offset in range(3))
                + tuple(range(max(1, page_count - 1), page_count + 1))
            )
        else:
            pages = (
                tuple(range(1, 9))
                + tuple(max(1, page_count // 2 - 1 + offset) for offset in range(3))
                + tuple(range(max(1, page_count - 2), page_count + 1))
            )
        return cls(page_count, tuple(dict.fromkeys(pages)))


@dataclass(frozen=True, slots=True)
class ProcessingOutput:
    chunks: tuple[IndexChunk, ...]
    receipt: IndexProcessingReceipt


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _text(value: bytes | str) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


def _heading(line: str) -> bool:
    stripped = line.strip()
    return bool(
        re.match(r"^(?:#{1,6}\s+|(?:\d+\.){1,4}\s+)[^\s].*$", stripped)
        or (stripped and len(stripped) <= 96 and stripped == stripped.title())
        or (
            stripped
            and len(stripped) <= 96
            and stripped.isupper()
            and any(character.isalpha() for character in stripped)
        )
    )


def _sections(text: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    path: list[str] = []
    body: list[str] = []
    saw_heading = False
    placeholder_number = 0

    def flush() -> None:
        nonlocal body, placeholder_number
        value = "\n".join(body).strip()
        if not value:
            return
        section_path = " / ".join(path)
        if not section_path and saw_heading:
            placeholder_number += 1
            section_path = f"Unsectioned {placeholder_number}"
        sections.append((section_path, value))
        body = []

    for line in text.splitlines():
        if _heading(line):
            flush()
            title = re.sub(r"^\s*(?:#+\s*|(?:\d+\.){1,4}\s+)", "", line).strip()
            level = len(line) - len(line.lstrip("#")) if line.lstrip().startswith("#") else 1
            path = path[: max(0, level - 1)] + [title]
            saw_heading = True
        elif line.strip():
            body.append(line.rstrip())
    flush()
    return sections


def _category_column(rows: Sequence[Sequence[str]], width: int) -> int:
    for column in range(width):
        values = [row[column].strip() for row in rows if column < len(row) and row[column].strip()]
        if values and len(set(values)) < len(values):
            return column
    return 0


def _split_rows(
    rows: Sequence[Sequence[str]], width: int, *, category_column: int | None = None
) -> list[tuple[int, int]]:
    if not rows:
        return []
    size = 30 if width < 20 else 20
    category = _category_column(rows, width) if category_column is None else category_column
    groups: list[tuple[int, int]] = []
    start = 0
    while start < len(rows):
        end = min(len(rows), start + size)
        if end < len(rows) and width and rows[end - 1] and rows[end]:
            value = rows[end - 1][category].strip() if category < len(rows[end - 1]) else ""
            while (
                end > start + 1
                and value
                and category < len(rows[end])
                and rows[end][category].strip() == value
            ):
                end -= 1
        groups.append((start, end))
        start = end
    return groups


def _column_name(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result or "A"


def _flatten(value: Any, prefix: str = "") -> list[tuple[str, str]]:
    if isinstance(value, Mapping):
        result: list[tuple[str, str]] = []
        for key, child in value.items():
            result.extend(_flatten(child, f"{prefix}.{key}" if prefix else str(key)))
        return result
    if isinstance(value, list):
        return [
            (f"{prefix}[{index}]", json.dumps(item, ensure_ascii=False, sort_keys=True))
            for index, item in enumerate(value)
        ]
    return [(prefix, str(value))]


class ContentProcessor:
    """Deterministic format routing and derivation boundary.

    Expensive OCR/VLM/CR work is injected through ports; the default adapter is
    intentionally only suitable for development and contract tests.
    """

    def __init__(
        self,
        *,
        compressor: CompressionPort | None = None,
        mineru: Callable[[bytes], Mapping[str, Any]] | None = None,
        image_describer: Callable[[bytes, Mapping[str, Any]], str] | None = None,
        image_ocr: Callable[[bytes, Mapping[str, Any]], str] | None = None,
    ) -> None:
        self._compressor = compressor or IdentityCompression()
        self._mineru = mineru
        self._image_describer = image_describer
        self._image_ocr = image_ocr

    def process(
        self,
        request: IndexStagingRequest,
        content: bytes | str,
        *,
        media_kind: str,
        content_manifest_id: str,
        content_manifest_hash: str,
        processing_config_version: str | None = None,
        model_version: str = "none",
        prompt_version: str = "none",
        page_count: int = 0,
        has_text_layer: bool | None = None,
        image_context: Mapping[str, Any] | None = None,
        image_ocr_text: str | None = None,
        decorative_image: bool = False,
    ) -> ProcessingOutput:
        raw = content if isinstance(content, bytes) else content.encode("utf-8")
        kind = media_kind.strip().casefold()
        profile = processing_config_version or request.processing_profile_version or "default"
        if not content_manifest_id or not content_manifest_hash:
            raise PlatformError(
                "validation_error", "content manifest identity is required", {}, 422
            )
        if request.input_manifest_hash != content_manifest_hash:
            raise PlatformError(
                "processing_receipt_conflict",
                "content manifest does not match the staging request",
                {},
                409,
            )
        if kind in {"text/plain", "text/markdown", "txt", "md", "markdown"}:
            chunks, summary, degradations = self._text_chunks(
                request, _text(content), media_kind, content_manifest_hash
            )
        elif kind in {"text/csv", "csv"} or kind.endswith("csv"):
            chunks, summary, degradations = self._csv_chunks(
                request, _text(content), content_manifest_hash
            )
        elif kind in {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-excel",
            "xlsx",
            "excel",
        }:
            chunks, summary, degradations = self._xlsx_chunks(request, raw, content_manifest_hash)
        elif kind in {
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
            chunks, summary, degradations = self._structured_chunks(
                request, _text(content), kind, content_manifest_hash
            )
        elif kind in {"text/x-python", "python", "code", "text/javascript", "javascript"}:
            chunks, summary, degradations = self._code_chunks(
                request, _text(content), content_manifest_hash
            )
        elif kind in {
            "application/pdf",
            "pdf",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "docx",
            "word",
        }:
            if self._mineru is None:
                raise PlatformError(
                    "processor_unavailable", "MinerU processor is not configured", {}, 503
                )
            parsed = self._mineru(raw)
            parsed_text = str(parsed.get("text", ""))
            if not parsed_text.strip():
                raise PlatformError("processing_failed", "MinerU returned no text", {}, 422)
            chunks, summary, degradations = self._text_chunks(
                request, parsed_text, media_kind, content_manifest_hash
            )
            page_count = int(parsed.get("page_count", page_count or 0))
            summary["page_count"] = page_count
            parsed_text_layer = parsed.get("has_text_layer")
            has_text_layer = (
                bool(parsed_text_layer) if parsed_text_layer is not None else bool(has_text_layer)
            )
            ocr = dict(summary.get("ocr", {}))
            confidence = parsed.get("ocr_confidence")
            if confidence is not None:
                ocr["confidence"] = float(confidence)
                ocr["low_confidence"] = float(confidence) < 0.9
                ocr["fact"] = {"threshold": 0.9, "confidence": float(confidence)}
            local_confidence = parsed.get("ocr_confidence_by_page", {})
            if isinstance(local_confidence, Mapping):
                sample = OCRSamplePlan.for_page_count(page_count)
                sampled = {
                    str(page): float(local_confidence[str(page)])
                    for page in sample.pages
                    if str(page) in local_confidence
                }
                if sampled:
                    lowest = min(sampled.values())
                    ocr["local_confidence"] = sampled
                    ocr["low_confidence"] = lowest < 0.9
                    ocr["fact"] = {
                        "threshold": 0.9,
                        "sampled_pages": sampled,
                        "lowest_confidence": lowest,
                    }
            summary["ocr"] = ocr
            parsed_chunks = parsed.get("chunks", ())
            locations = tuple(parsed_chunks) if isinstance(parsed_chunks, Sequence) else ()
            if kind in {"application/pdf", "pdf"}:
                chunks = [
                    replace(
                        chunk,
                        locator=(
                            {
                                "page": int(locations[index].get("page", 1)),
                                **(
                                    {"span": str(locations[index]["span"])}
                                    if has_text_layer and locations[index].get("span") is not None
                                    else {}
                                ),
                            }
                            if index < len(locations) and isinstance(locations[index], Mapping)
                            else {"page": min(index + 1, max(page_count, 1))}
                        ),
                        snippet=chunk.snippet if has_text_layer else None,
                    )
                    for index, chunk in enumerate(chunks)
                ]
            else:
                tree = dict(summary.get("tree") or {})
                sections: list[dict[str, list[str]]] = []
                for chunk in chunks:
                    section_path = str(chunk.metadata.get("section_path") or "")
                    sections.append(
                        {
                            "path": [part for part in section_path.split(" / ") if part],
                            "paragraphs": [chunk.text],
                        }
                    )
                tree["sections"] = sections
                summary["tree"] = tree
                chunks = [replace(chunk, snippet=None) for chunk in chunks]
        elif kind.startswith("image/") or kind in {"image", "图片"}:
            context = {
                "media_kind": media_kind,
                "decorative": decorative_image,
                **dict(image_context or {}),
                "ocr_text": image_ocr_text or "",
            }
            if not context["ocr_text"] and self._image_ocr is not None:
                context["ocr_text"] = self._image_ocr(raw, context)
            chunks, summary, degradations = self._image_chunks(
                request,
                raw,
                content_manifest_hash,
                context=context,
            )
        else:
            raise PlatformError("unsupported_media_kind", "Media kind is not supported", {}, 422)
        summary = dict(summary)
        summary["media_kind"] = media_kind
        summary["processing_profile_version"] = profile
        summary.setdefault("page_count", page_count)
        summary.setdefault(
            "has_text_layer",
            has_text_layer if has_text_layer is not None else kind not in {"image", "图片"},
        )
        summary.setdefault("sheet_manifest", [])
        summary.setdefault("row_groups", [])
        summary.setdefault("failure_reason", None)
        if page_count:
            sample = OCRSamplePlan.for_page_count(page_count)
            summary.setdefault("ocr", {})
            summary["ocr"] = {**dict(summary["ocr"]), "sample_pages": list(sample.pages)}
        stage_resources = tuple(
            {
                "backend_kind": "index_chunk",
                "resource_id": f"{request.attempt_id}:{request.publication_id}:{chunk.chunk_id}",
                "attempt_id": request.attempt_id,
                "publication_id": request.publication_id,
                "fencing_token": request.fencing_token,
                "document_id": request.document_id,
                "document_version_id": request.document_version_id,
                "generation_id": request.expected_generation_id,
                "space_id": request.space_id,
            }
            for chunk in chunks
        )
        configured_models = {
            name: str(request.processing_config_snapshot[name])
            for name in (
                "parser",
                "ocr_model",
                "vlm_model",
                "embedding_model",
                "reranker_model",
                "cr_model",
            )
            if request.processing_config_snapshot.get(name) is not None
        }
        configured_prompts = {
            name: str(request.processing_config_snapshot[name])
            for name in ("prompt", "ocr_prompt", "vlm_prompt", "cr_prompt")
            if request.processing_config_snapshot.get(name) is not None
        }
        receipt = IndexProcessingReceipt(
            job_id=request.job_id,
            attempt_id=request.attempt_id,
            fencing_token=request.fencing_token,
            publication_id=request.publication_id,
            document_id=request.document_id,
            document_version_id=request.document_version_id,
            input_content_hash=_sha256(raw),
            stage_resources=stage_resources,
            processing_config_version=profile,
            generation_id=request.expected_generation_id,
            authorization_fence=request.authorization_fence,
            model_version=model_version,
            prompt_version=prompt_version,
            processing_summary=summary,
            locator_snippet_integrity={
                "locators_valid": all(bool(chunk.locator is not None) for chunk in chunks),
                "snippets_valid": all(chunk.snippet is not None for chunk in chunks),
                "chunk_manifest_hash": content_manifest_hash,
            },
            index_component_results={
                "dense": {"state": "staged"},
                "sparse": {"state": "staged"},
                "tree": {
                    "state": "staged" if summary.get("tree", {}).get("tree_indexed") else "disabled"
                },
                "public_graph": {"state": "disabled"},
            },
            content_manifest_id=content_manifest_id,
            content_manifest_hash=content_manifest_hash,
            failure=None,
            degradations=tuple(degradations),
            ocr_low_confidence=bool(summary.get("ocr", {}).get("low_confidence", False)),
            ocr_low_confidence_fact=summary.get("ocr", {}).get("fact"),
            input_manifest_hash=request.input_manifest_hash,
            processing_profile_version=request.processing_profile_version,
            stage_resource_ids=tuple(str(item["resource_id"]) for item in stage_resources),
            model_versions={"primary": model_version, **configured_models},
            prompt_versions={"primary": prompt_version, **configured_prompts},
            space_id=request.space_id,
            operation=request.operation,
            base_active_version_id=request.base_active_version_id,
            index_revision_at_start=request.index_revision_at_start,
            object_manifest_ref=request.object_manifest_ref,
            processing_config_snapshot=dict(request.processing_config_snapshot),
        )
        receipt.validate_against(request)
        return ProcessingOutput(tuple(chunks), receipt)

    def _text_chunks(
        self,
        request: IndexStagingRequest,
        text: str,
        media_kind: str,
        manifest_hash: str,
    ) -> tuple[list[IndexChunk], Mapping[str, Any], tuple[Mapping[str, Any], ...]]:
        sections = _sections(text)
        tree_indexed = bool(sections and any(path for path, _ in sections))
        if not sections and text.strip():
            sections = [("", text.strip())]
        chunks: list[IndexChunk] = []
        for index, (path, body) in enumerate(sections, start=1):
            compressed = self._compressor.compress(body, context={"section_path": path})
            chunks.append(
                IndexChunk(
                    chunk_id=f"chunk_{index}",
                    generation_id=request.expected_generation_id,
                    publication_id=request.publication_id,
                    document_id=request.document_id,
                    document_version_id=request.document_version_id,
                    space_id=request.space_id,
                    text=body,
                    embedding_text=compressed,
                    sparse_text=(f"{path}\n{body}" if path else body),
                    locator={"section_path": path} if path else {},
                    snippet=body[:500],
                    media_kind=media_kind,
                    manifest_hash=manifest_hash,
                    metadata={"section_path": path, "cr_unit": "chunk"},
                )
            )
        return (
            chunks,
            {
                "chunk_count": len(chunks),
                "page_count": 0,
                "image_count": 0,
                "table_count": 0,
                "ocr": {"sample_strategy": "head_middle_tail", "low_confidence": False},
                "tree": {
                    "tree_indexed": tree_indexed,
                    "tree_reason": "structure_signal" if tree_indexed else "no_structure",
                    "section_count": len(sections),
                },
                "cr": {"applied": bool(chunks), "unit": "chunk"},
            },
            (),
        )

    def _csv_chunks(
        self,
        request: IndexStagingRequest,
        text: str,
        manifest_hash: str,
    ) -> tuple[list[IndexChunk], Mapping[str, Any], tuple[Mapping[str, Any], ...]]:
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        if not rows or not rows[0]:
            raise PlatformError("table_parse_failed", "CSV has no header", {}, 422)
        headers = [str(item).strip() for item in rows[0]]
        data = rows[1:]
        ranges = _split_rows(data, len(headers))
        chunks: list[IndexChunk] = []
        for index, (start, end) in enumerate(ranges, start=1):
            values = data[start:end]
            lines = [
                " | ".join(
                    f"{header}: {row[col] if col < len(row) else ''}"
                    for col, header in enumerate(headers)
                )
                for row in values
            ]
            body = "\n".join(lines)
            chunks.append(
                IndexChunk(
                    chunk_id=f"chunk_{index}",
                    generation_id=request.expected_generation_id,
                    publication_id=request.publication_id,
                    document_id=request.document_id,
                    document_version_id=request.document_version_id,
                    space_id=request.space_id,
                    text=body,
                    embedding_text=body,
                    sparse_text=body,
                    locator={
                        "sheet": "CSV",
                        "a1_range": f"A{start + 2}:{chr(65 + min(len(headers), 26) - 1)}{end + 1}",
                    },
                    snippet=None,
                    media_kind="text/csv",
                    manifest_hash=manifest_hash,
                    metadata={
                        "headers": headers,
                        "row_start": start + 2,
                        "row_end": end + 1,
                        "table": True,
                    },
                )
            )
        return (
            chunks,
            {
                "chunk_count": len(chunks),
                "page_count": 0,
                "image_count": 0,
                "table_count": len(chunks),
                "ocr": {},
                "tree": {"tree_indexed": False, "tree_reason": "table"},
                "cr": {"applied": False, "unit": "table_header"},
                "sheet_count": 1,
                "headers": headers,
                "sheet_manifest": [{"sheet": "CSV", "headers": headers, "row_count": len(rows)}],
                "row_groups": [
                    {"sheet": "CSV", "start": start + 2, "end": end + 1} for start, end in ranges
                ],
            },
            (),
        )

    def _xlsx_chunks(
        self,
        request: IndexStagingRequest,
        content: bytes,
        manifest_hash: str,
    ) -> tuple[list[IndexChunk], Mapping[str, Any], tuple[Mapping[str, Any], ...]]:
        try:
            from openpyxl import load_workbook  # type: ignore[import-untyped]

            workbook = load_workbook(io.BytesIO(content), read_only=False, data_only=True)
        except Exception as exc:
            raise PlatformError(
                "table_parse_failed", "Excel workbook could not be parsed", {}, 422
            ) from exc
        chunks: list[IndexChunk] = []
        table_count = 0
        sheet_count = len(workbook.sheetnames)
        sheet_manifest: list[Mapping[str, Any]] = []
        row_groups: list[Mapping[str, Any]] = []
        try:
            for worksheet in workbook.worksheets:
                rows = [
                    (row_number, ["" if value is None else str(value) for value in row])
                    for row_number, row in enumerate(worksheet.iter_rows(values_only=True), start=1)
                ]
                row_count = len(rows)
                merged_ranges = [str(value) for value in worksheet.merged_cells.ranges]
                horizontally_merged_rows: set[int] = set()
                for merged in worksheet.merged_cells.ranges:
                    value = worksheet.cell(merged.min_row, merged.min_col).value
                    if merged.min_row == merged.max_row and merged.min_col < merged.max_col:
                        horizontally_merged_rows.add(merged.min_row)
                    for row in range(merged.min_row, merged.max_row + 1):
                        for column in range(merged.min_col, merged.max_col + 1):
                            if row - 1 < len(rows) and column - 1 < len(rows[row - 1][1]):
                                rows[row - 1][1][column - 1] = "" if value is None else str(value)
                rows = [
                    (row_number, row)
                    for row_number, row in rows
                    if any(cell.strip() for cell in row)
                ]
                if not rows or not rows[0]:
                    raise PlatformError("table_parse_failed", "Excel sheet has no header", {}, 422)
                headers = rows[0][1]
                sheet_manifest.append(
                    {
                        "sheet": worksheet.title,
                        "headers": headers,
                        "merged_ranges": merged_ranges,
                        "row_count": row_count,
                    }
                )
                data = rows[1:]
                for start, end in _split_rows([row for _, row in data], len(headers)):
                    values = data[start:end]
                    start_row = values[0][0]
                    end_row = values[-1][0]
                    total_rows = [
                        row_number
                        for row_number, _ in values
                        if row_number in horizontally_merged_rows
                    ]
                    body = "\n".join(
                        " | ".join(
                            f"{header}: {row[index] if index < len(row) else ''}"
                            for index, header in enumerate(headers)
                        )
                        for _, row in values
                    )
                    if not body.strip():
                        continue
                    table_count += 1
                    row_groups.append(
                        {
                            "sheet": worksheet.title,
                            "start": start_row,
                            "end": end_row,
                            "block": len(row_groups) + 1,
                            "total_rows": total_rows,
                        }
                    )
                    chunks.append(
                        IndexChunk(
                            chunk_id=f"chunk_{len(chunks) + 1}",
                            generation_id=request.expected_generation_id,
                            publication_id=request.publication_id,
                            document_id=request.document_id,
                            document_version_id=request.document_version_id,
                            space_id=request.space_id,
                            text=body,
                            embedding_text=body,
                            sparse_text=body,
                            locator={
                                "sheet": worksheet.title,
                                "a1_range": (f"A{start_row}:{_column_name(len(headers))}{end_row}"),
                            },
                            snippet=None,
                            media_kind="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            manifest_hash=manifest_hash,
                            metadata={
                                "headers": headers,
                                "row_start": start_row,
                                "row_end": end_row,
                                "table": True,
                                "merged_ranges": merged_ranges,
                                "total_rows": total_rows,
                            },
                        )
                    )
        finally:
            workbook.close()
        return (
            chunks,
            {
                "chunk_count": len(chunks),
                "page_count": 0,
                "image_count": 0,
                "table_count": table_count,
                "sheet_count": sheet_count,
                "ocr": {},
                "tree": {"tree_indexed": False, "tree_reason": "table"},
                "cr": {"applied": False, "unit": "table_header"},
                "sheet_manifest": sheet_manifest,
                "row_groups": row_groups,
            },
            (),
        )

    def _structured_chunks(
        self,
        request: IndexStagingRequest,
        text: str,
        kind: str,
        manifest_hash: str,
    ) -> tuple[list[IndexChunk], Mapping[str, Any], tuple[Mapping[str, Any], ...]]:
        try:
            if kind in {"application/json", "json"}:
                value = json.loads(text)
            elif kind in {"application/toml", "toml"}:
                value = tomllib.loads(text)
            elif kind in {"application/xml", "text/xml", "xml"}:
                root = ElementTree.fromstring(text)
                value = {root.tag: {child.tag: child.text or "" for child in root}}
            else:
                value = self._simple_yaml(text)
        except (
            ValueError,
            TypeError,
            json.JSONDecodeError,
            tomllib.TOMLDecodeError,
            ElementTree.ParseError,
        ) as exc:
            raise PlatformError(
                "structured_parse_failed", "Structured input could not be parsed", {}, 422
            ) from exc
        if isinstance(value, list) and value and all(isinstance(item, Mapping) for item in value):
            headers = sorted({str(key) for item in value for key in item})
            rows = [[str(item.get(header, "")) for header in headers] for item in value]
            return self._table_chunks(
                request,
                headers,
                rows,
                manifest_hash,
                sheet="structured",
                media_kind=kind,
            )
        if isinstance(value, Mapping) and any(
            key in value for key in ("$schema", "schema", "type", "properties")
        ):
            return self._code_chunks(request, text, manifest_hash)
        pairs = _flatten(value)
        body = "\n".join(f"{key}: {item}" for key, item in pairs)
        large = len(body.split()) > 500
        chunk = IndexChunk(
            chunk_id="chunk_1",
            generation_id=request.expected_generation_id,
            publication_id=request.publication_id,
            document_id=request.document_id,
            document_version_id=request.document_version_id,
            space_id=request.space_id,
            text=body or "{}",
            embedding_text=(
                self._compressor.compress(body, context={"kind": "structured"}) if large else body
            )
            or "{}",
            sparse_text=body or "{}",
            locator={},
            snippet=body[:500] or "{}",
            media_kind=kind,
            manifest_hash=manifest_hash,
            metadata={
                "keys": [key for key, _ in pairs],
                "cr_unit": "chunk" if large else "key_path",
            },
        )
        return (
            [chunk],
            {
                "chunk_count": 1,
                "page_count": 0,
                "image_count": 0,
                "table_count": 0,
                "ocr": {},
                "tree": {"tree_indexed": False, "tree_reason": "structured"},
                "cr": {"applied": large, "unit": "chunk" if large else "key_path"},
            },
            (),
        )

    def _table_chunks(
        self,
        request: IndexStagingRequest,
        headers: Sequence[str],
        rows: Sequence[Sequence[str]],
        manifest_hash: str,
        *,
        sheet: str,
        media_kind: str,
    ) -> tuple[list[IndexChunk], Mapping[str, Any], tuple[Mapping[str, Any], ...]]:
        ranges = _split_rows(rows, len(headers))
        chunks: list[IndexChunk] = []
        for number, (start, end) in enumerate(ranges, start=1):
            body = "\n".join(
                " | ".join(
                    f"{header}: {row[column] if column < len(row) else ''}"
                    for column, header in enumerate(headers)
                )
                for row in rows[start:end]
            )
            chunks.append(
                IndexChunk(
                    chunk_id=f"chunk_{number}",
                    generation_id=request.expected_generation_id,
                    publication_id=request.publication_id,
                    document_id=request.document_id,
                    document_version_id=request.document_version_id,
                    space_id=request.space_id,
                    text=body,
                    embedding_text=body,
                    sparse_text=body,
                    locator={
                        "sheet": sheet,
                        "a1_range": f"A{start + 2}:{_column_name(len(headers))}{end + 1}",
                    },
                    snippet=None,
                    media_kind=media_kind,
                    manifest_hash=manifest_hash,
                    metadata={"headers": list(headers), "table": True},
                )
            )
        return (
            chunks,
            {
                "chunk_count": len(chunks),
                "page_count": 0,
                "image_count": 0,
                "table_count": len(chunks),
                "ocr": {},
                "tree": {"tree_indexed": False, "tree_reason": "structured_table"},
                "cr": {"applied": False, "unit": "table_header"},
                "sheet_manifest": [{"sheet": sheet, "headers": list(headers)}],
                "row_groups": [
                    {"sheet": sheet, "start": start + 2, "end": end + 1} for start, end in ranges
                ],
            },
            (),
        )

    @staticmethod
    def _simple_yaml(text: str) -> Mapping[str, Any]:
        result: dict[str, Any] = {}
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
                continue
            key, value = line.split(":", 1)
            result[key.strip()] = value.strip().strip("'\"")
        return result

    def _code_chunks(
        self,
        request: IndexStagingRequest,
        text: str,
        manifest_hash: str,
    ) -> tuple[list[IndexChunk], Mapping[str, Any], tuple[Mapping[str, Any], ...]]:
        symbols: list[tuple[str, str]] = []
        try:
            tree = ast.parse(text)
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    lines = text.splitlines()[node.lineno - 1 : node.end_lineno]
                    symbols.append((node.name, "\n".join(lines)))
        except SyntaxError:
            symbols = []
        if not symbols and ("function " in text or "class " in text):
            matches = list(
                re.finditer(
                    r"^(?:export\s+)?(?:async\s+)?(?:function|class)\s+([A-Za-z_$][\w$]*)",
                    text,
                    re.MULTILINE,
                )
            )
            for index, match in enumerate(matches):
                end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
                symbols.append((match.group(1), text[match.start() : end].strip()))
        if not symbols:
            symbols = [("module", text.strip())]
        chunks = [
            IndexChunk(
                chunk_id=f"chunk_{index}",
                generation_id=request.expected_generation_id,
                publication_id=request.publication_id,
                document_id=request.document_id,
                document_version_id=request.document_version_id,
                space_id=request.space_id,
                text=body,
                embedding_text=self._compressor.compress(body, context={"symbol": name}),
                sparse_text=body,
                locator={"section_path": name},
                snippet=body[:500],
                media_kind="code",
                manifest_hash=manifest_hash,
                metadata={"symbol": name, "cr_unit": "symbol"},
            )
            for index, (name, body) in enumerate(symbols, start=1)
            if body.strip()
        ]
        return (
            chunks,
            {
                "chunk_count": len(chunks),
                "page_count": 0,
                "image_count": 0,
                "table_count": 0,
                "ocr": {},
                "tree": {"tree_indexed": False, "tree_reason": "code"},
                "cr": {"applied": True, "unit": "symbol"},
            },
            (),
        )

    def _image_chunks(
        self,
        request: IndexStagingRequest,
        content: bytes,
        manifest_hash: str,
        *,
        context: Mapping[str, Any],
    ) -> tuple[list[IndexChunk], Mapping[str, Any], tuple[Mapping[str, Any], ...]]:
        if context.get("decorative"):
            return (
                [],
                {
                    "chunk_count": 0,
                    "page_count": 0,
                    "image_count": 1,
                    "table_count": 0,
                    "ocr": {"applied": False},
                    "tree": {"tree_indexed": False, "tree_reason": "decorative_image"},
                    "cr": {"applied": False, "unit": "image"},
                },
                ({"code": "image_not_indexable", "reason": "decorative"},),
            )
        description = (
            self._image_describer(content, context).strip()
            if self._image_describer is not None
            else ""
        )
        caption = str(context.get("caption", "")).strip()
        ocr_text = str(context.get("ocr_text", "")).strip()
        index_text = "\n".join(part for part in (caption, description, ocr_text) if part)
        if not index_text:
            return (
                [],
                {
                    "chunk_count": 0,
                    "page_count": 0,
                    "image_count": 1,
                    "table_count": 0,
                    "ocr": {"applied": bool(ocr_text)},
                    "tree": {"tree_indexed": False, "tree_reason": "image"},
                    "cr": {"applied": False, "unit": "image"},
                },
                ({"code": "image_not_indexable", "reason": "no_usable_text"},),
            )
        return (
            [
                IndexChunk(
                    chunk_id="chunk_1",
                    generation_id=request.expected_generation_id,
                    publication_id=request.publication_id,
                    document_id=request.document_id,
                    document_version_id=request.document_version_id,
                    space_id=request.space_id,
                    text=index_text,
                    embedding_text=index_text,
                    sparse_text=index_text,
                    locator={},
                    snippet=None,
                    media_kind="image",
                    manifest_hash=manifest_hash,
                    metadata={
                        "image_description": bool(description),
                        "reverse_links": list(context.get("reverse_links", ())),
                    },
                )
            ],
            {
                "chunk_count": 1,
                "page_count": 0,
                "image_count": 1,
                "table_count": 0,
                "ocr": {"applied": bool(ocr_text)},
                "tree": {"tree_indexed": False, "tree_reason": "image"},
                "cr": {"applied": False, "unit": "image_description"},
            },
            (),
        )


__all__ = ["ContentProcessor", "IdentityCompression", "OCRSamplePlan", "ProcessingOutput"]
