"""Document loader interfaces and local file implementations."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

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


class DocumentLoaderRegistry:
    """Selects a document loader by file extension."""

    def __init__(self, loaders: list[DocumentLoader]):
        self.loaders = list(loaders)

    @classmethod
    def default(cls) -> "DocumentLoaderRegistry":
        return cls(loaders=[TextDocumentLoader(), MarkdownDocumentLoader()])

    def load(self, path: str | Path) -> list[Document]:
        file_path = Path(path)
        for loader in self.loaders:
            if loader.supports(file_path):
                return loader.load(file_path)

        extension = file_path.suffix.lower() or "<none>"
        raise ValueError(f"Unsupported document extension: {extension}")


def _base_file_metadata(path: Path) -> dict[str, str]:
    resolved_path = path.resolve()
    return {
        "source_path": resolved_path.as_posix(),
        "extension": resolved_path.suffix.lower(),
        "file_name": resolved_path.name,
        "_source": resolved_path.as_posix(),
        "_extension": resolved_path.suffix.lower(),
        "_file_name": resolved_path.name,
    }
