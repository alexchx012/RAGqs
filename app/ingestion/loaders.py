"""Document loader interfaces and local file implementations."""

from __future__ import annotations

import csv
import json
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol

from langchain_core.documents import Document


class DocumentLoader(Protocol):
    """Loads a source into LangChain documents."""

    extensions: tuple[str, ...]

    def supports(self, path: str | Path) -> bool:
        """Return whether this loader can load the path."""

    def load(self, path: str | Path) -> list[Document]:
        """Load documents from a path."""


class TextDocumentLoader:
    """UTF-8 text file loader."""

    extensions = (".txt",)

    def supports(self, path: str | Path) -> bool:
        return Path(path).suffix.lower() in self.extensions

    def load(self, path: str | Path) -> list[Document]:
        file_path = Path(path)
        if not file_path.exists() or not file_path.is_file():
            raise ValueError(f"Document file does not exist: {path}")

        content = file_path.read_text(encoding="utf-8")
        metadata = _base_file_metadata(file_path)
        return [Document(page_content=content, metadata=metadata)]


class MarkdownDocumentLoader(TextDocumentLoader):
    """UTF-8 Markdown file loader."""

    extensions = (".md",)


class CSVDocumentLoader:
    """UTF-8 CSV loader that emits one document per row."""

    extensions = (".csv",)

    def supports(self, path: str | Path) -> bool:
        return Path(path).suffix.lower() in self.extensions

    def load(self, path: str | Path) -> list[Document]:
        file_path = _ensure_file(path)
        metadata = _base_file_metadata(file_path)
        with file_path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))

        if not rows:
            return [Document(page_content="", metadata={**metadata, "row_count": 0})]

        headers = [_normalize_header(value, index) for index, value in enumerate(rows[0])]
        documents: list[Document] = []
        for row_index, row in enumerate(rows[1:], start=1):
            row_metadata = {**metadata, "row_number": row_index}
            documents.append(
                Document(
                    page_content=_format_tabular_row(headers, row),
                    metadata=row_metadata,
                )
            )
        return documents


class HTMLDocumentLoader:
    """UTF-8 HTML loader that extracts visible text content."""

    extensions = (".html", ".htm")

    def supports(self, path: str | Path) -> bool:
        return Path(path).suffix.lower() in self.extensions

    def load(self, path: str | Path) -> list[Document]:
        file_path = _ensure_file(path)
        parser = _VisibleTextHTMLParser()
        parser.feed(file_path.read_text(encoding="utf-8"))
        content = "\n".join(parser.visible_text())
        metadata = _base_file_metadata(file_path)
        if parser.title:
            metadata["title"] = parser.title
        return [Document(page_content=content, metadata=metadata)]


class JSONDocumentLoader:
    """UTF-8 JSON loader that emits one document per top-level record."""

    extensions = (".json",)

    def supports(self, path: str | Path) -> bool:
        return Path(path).suffix.lower() in self.extensions

    def load(self, path: str | Path) -> list[Document]:
        file_path = _ensure_file(path)
        data = json.loads(file_path.read_text(encoding="utf-8"))
        metadata = _base_file_metadata(file_path)
        if isinstance(data, list):
            return [
                Document(
                    page_content=json.dumps(item, ensure_ascii=False, indent=2),
                    metadata={**metadata, "json_pointer": f"$[{index}]"},
                )
                for index, item in enumerate(data)
            ]
        return [
            Document(
                page_content=json.dumps(data, ensure_ascii=False, indent=2),
                metadata={**metadata, "json_pointer": "$"},
            )
        ]


class DocumentLoaderRegistry:
    """Selects a document loader by file extension."""

    def __init__(self, loaders: list[DocumentLoader]):
        self.loaders = list(loaders)

    @classmethod
    def default(cls) -> DocumentLoaderRegistry:
        return cls(
            loaders=[
                TextDocumentLoader(),
                MarkdownDocumentLoader(),
                CSVDocumentLoader(),
                HTMLDocumentLoader(),
                JSONDocumentLoader(),
            ]
        )

    def load(self, path: str | Path) -> list[Document]:
        file_path = Path(path)
        for loader in self.loaders:
            if loader.supports(file_path):
                return loader.load(file_path)

        extension = file_path.suffix.lower() or "<none>"
        raise ValueError(f"Unsupported document extension: {extension}")


def _ensure_file(path: str | Path) -> Path:
    file_path = Path(path)
    if not file_path.exists() or not file_path.is_file():
        raise ValueError(f"Document file does not exist: {path}")
    return file_path


def _base_file_metadata(path: Path) -> dict[str, Any]:
    resolved_path = path.resolve()
    return {
        "source_path": resolved_path.as_posix(),
        "extension": resolved_path.suffix.lower(),
        "file_name": resolved_path.name,
        "_source": resolved_path.as_posix(),
        "_extension": resolved_path.suffix.lower(),
        "_file_name": resolved_path.name,
    }


def _normalize_header(value: str, index: int) -> str:
    normalized = value.strip()
    return normalized or f"column_{index + 1}"


def _format_tabular_row(headers: list[str], row: list[str]) -> str:
    extra_headers = [
        f"column_{index + 1}" for index in range(len(headers), len(row))
    ]
    active_headers = [*headers, *extra_headers]
    values = [*row, *([""] * max(0, len(active_headers) - len(row)))]
    return "\n".join(
        f"{header}: {value.strip()}"
        for header, value in zip(active_headers, values, strict=False)
    )


class _VisibleTextHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self._in_title = False
        self._chunks: list[str] = []
        self._title_chunks: list[str] = []

    @property
    def title(self) -> str:
        return " ".join(self._title_chunks).strip()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        normalized = tag.lower()
        if normalized in {"script", "style", "noscript"}:
            self._skip_depth += 1
        if normalized == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
        if normalized == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if not text or self._skip_depth:
            return
        if self._in_title:
            self._title_chunks.append(text)
        self._chunks.append(text)

    def visible_text(self) -> list[str]:
        return self._chunks
