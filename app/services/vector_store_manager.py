"""向量存储管理器 - provider-backed compatibility module."""

from typing import Any

from langchain_core.documents import Document

from app.config import config
from app.core.milvus_client import milvus_manager
from app.providers.milvus import MilvusVectorStoreProvider
from app.services.vector_embedding_service import vector_embedding_service

COLLECTION_NAME = "biz"


class VectorStoreManager:
    """向量存储管理器"""

    def __init__(self, provider: Any | None = None):
        self.collection_name = COLLECTION_NAME
        self.provider = provider or MilvusVectorStoreProvider(
            embedding_provider=vector_embedding_service,
            milvus_manager=milvus_manager,
            collection_name=self.collection_name,
            host=config.milvus_host,
            port=config.milvus_port,
        )

    def add_documents(self, documents: list[Document]) -> list[str]:
        """批量添加文档到向量存储"""
        return self.provider.add_documents(documents)

    def delete_by_source(self, file_path: str) -> int:
        """删除指定文件的所有文档"""
        return self.provider.delete_by_source(file_path)

    def delete_by_document_id(self, document_id: str) -> int:
        """删除指定 document_id 的所有文档"""
        return self.provider.delete_by_document_id(document_id)

    def get_vector_store(self) -> Any:
        return self.provider.get_vector_store()

    def similarity_search(self, query: str, k: int = 3) -> list[Document]:
        return self.provider.similarity_search(query, k=k)


vector_store_manager = VectorStoreManager()
