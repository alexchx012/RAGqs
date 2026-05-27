"""文档分割服务 - 基于 LangChain 的智能文档分割"""

from pathlib import Path
from typing import Any

from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter
from loguru import logger

from app.config import config


class DocumentSplitterService:
    """文档分割服务"""

    def __init__(self, settings: Any | None = None):
        self.settings = settings or config
        self.chunk_size = _settings_value(
            self.settings,
            "chunking",
            "max_size",
            "chunk_max_size",
            800,
        )
        self.chunk_overlap = _settings_value(
            self.settings,
            "chunking",
            "overlap",
            "chunk_overlap",
            100,
        )

        self.markdown_splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=[("#", "h1"), ("##", "h2")],
            strip_headers=False,
        )

        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.chunk_size * 2,
            chunk_overlap=self.chunk_overlap,
            length_function=len,
            is_separator_regex=False,
        )

    def split_markdown(self, content: str, file_path: str = "") -> list[Document]:
        if not content or not content.strip():
            return []
        try:
            md_docs = self.markdown_splitter.split_text(content)
            docs_after_split = self.text_splitter.split_documents(md_docs)
            final_docs = self._merge_small_chunks(docs_after_split, min_size=300)
            extension = Path(file_path).suffix or ".md"
            for doc in final_docs:
                doc.metadata["_source"] = file_path
                doc.metadata["_extension"] = extension
                doc.metadata["_file_name"] = Path(file_path).name
            return final_docs
        except Exception as e:
            logger.error(f"Markdown 分割失败: {file_path}, 错误: {e}")
            raise

    def split_text(self, content: str, file_path: str = "") -> list[Document]:
        if not content or not content.strip():
            return []
        try:
            docs = self.text_splitter.create_documents(
                texts=[content],
                metadatas=[{"_source": file_path, "_extension": Path(file_path).suffix, "_file_name": Path(file_path).name}],
            )
            return docs
        except Exception as e:
            logger.error(f"文本分割失败: {file_path}, 错误: {e}")
            raise

    def split_document(self, content: str, file_path: str = "") -> list[Document]:
        if Path(file_path).suffix.lower() in {".md", ".markdown"}:
            return self.split_markdown(content, file_path)
        return self.split_text(content, file_path)

    def _merge_small_chunks(self, documents: list[Document], min_size: int = 300) -> list[Document]:
        if not documents:
            return []
        merged_docs = []
        current_doc = None
        for doc in documents:
            if current_doc is None:
                current_doc = doc
            elif len(doc.page_content) < min_size and len(current_doc.page_content) < self.chunk_size * 2:
                current_doc.page_content += "\n\n" + doc.page_content
            else:
                merged_docs.append(current_doc)
                current_doc = doc
        if current_doc is not None:
            merged_docs.append(current_doc)
        return merged_docs


def _settings_value(
    settings: Any,
    group_name: str,
    group_field_name: str,
    flat_field_name: str,
    default: Any,
) -> Any:
    group = getattr(settings, group_name, None)
    if group is not None and hasattr(group, group_field_name):
        return getattr(group, group_field_name)
    return getattr(settings, flat_field_name, default)


document_splitter_service = DocumentSplitterService()
