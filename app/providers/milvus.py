"""Milvus provider implementations."""

from __future__ import annotations

import time
import uuid
from typing import Any, Callable, cast

from langchain_core.documents import Document
from langchain_milvus import Milvus
from loguru import logger
from pymilvus.orm.future import MutationFuture
from pymilvus.orm.mutation import MutationResult

from app.providers.contracts import EmbeddingProvider


class MilvusVectorStoreProvider:
    """Lazy Milvus-backed vector store provider."""

    def __init__(
        self,
        embedding_provider: EmbeddingProvider,
        milvus_manager: Any,
        collection_name: str,
        host: str,
        port: int,
        vector_store_factory: Callable[..., Any] = Milvus,
    ):
        self.embedding_provider = embedding_provider
        self.milvus_manager = milvus_manager
        self.collection_name = collection_name
        self.host = host
        self.port = port
        self.vector_store_factory = vector_store_factory
        self._vector_store: Any | None = None

    def get_vector_store(self) -> Any:
        if self._vector_store is None:
            self.milvus_manager.connect()
            self._vector_store = self.vector_store_factory(
                embedding_function=self.embedding_provider,
                collection_name=self.collection_name,
                connection_args={"host": self.host, "port": self.port},
                auto_id=False,
                drop_old=False,
                text_field="content",
                vector_field="vector",
                primary_field="id",
                metadata_field="metadata",
            )
            logger.info(f"VectorStore 初始化成功: collection={self.collection_name}")
        return self._vector_store

    def add_documents(self, documents: list[Document]) -> list[str]:
        try:
            start_time = time.time()
            ids = [str(uuid.uuid4()) for _ in documents]
            result_ids = self.get_vector_store().add_documents(documents, ids=ids)
            elapsed = time.time() - start_time
            logger.info(f"批量添加 {len(documents)} 个文档, 耗时: {elapsed:.2f}秒")
            return result_ids
        except Exception as e:
            logger.error(f"添加文档失败: {e}")
            raise

    def delete_by_source(self, source: str) -> int:
        return self._delete_by_metadata_field("_source", source, f"文件旧数据: {source}")

    def delete_by_document_id(self, document_id: str) -> int:
        return self._delete_by_metadata_field(
            "document_id",
            document_id,
            f"document_id旧数据: {document_id}",
        )

    def _delete_by_metadata_field(self, field_name: str, value: str, log_label: str) -> int:
        try:
            self.milvus_manager.connect()
            collection = self.milvus_manager.get_collection()
            escaped_value = _escape_milvus_string(value)
            expr = f'metadata["{field_name}"] == "{escaped_value}"'
            raw_result = collection.delete(expr)
            if isinstance(raw_result, MutationFuture):
                result = cast(MutationResult, raw_result.result())
            else:
                result = cast(MutationResult, raw_result)
            deleted_count = result.delete_count
            logger.info(f"删除{log_label}, 删除数量: {deleted_count}")
            return deleted_count
        except Exception as e:
            logger.warning(f"删除旧数据失败: {e}")
            return 0

    def similarity_search(
        self,
        query: str,
        k: int = 3,
        filters: dict[str, Any] | None = None,
    ) -> list[Document]:
        try:
            kwargs: dict[str, Any] = {}
            if filters:
                kwargs["filter"] = filters
            return self.get_vector_store().similarity_search(query, k=k, **kwargs)
        except Exception as e:
            logger.error(f"相似度搜索失败: {e}")
            return []


def _escape_milvus_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
