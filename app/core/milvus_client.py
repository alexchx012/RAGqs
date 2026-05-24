"""Milvus 客户端管理模块"""

from loguru import logger
from pymilvus import (
    Collection,
    CollectionSchema,
    DataType,
    FieldSchema,
    MilvusClient,
    connections,
    utility,
    MilvusException,
)

from app.config import config


def _patch_pymilvus_milvus_client_orm_alias() -> None:
    """
    修复 langchain_milvus 内部 MilvusClient 的 _using 别名问题。
    """
    if getattr(_patch_pymilvus_milvus_client_orm_alias, "_done", False):
        return
    try:
        from pymilvus.milvus_client.milvus_client import MilvusClient
    except ImportError:
        return

    _orig_init = MilvusClient.__init__

    def _wrapped_init(self, *args, **kwargs):
        _orig_init(self, *args, **kwargs)
        self._using = "default"

    MilvusClient.__init__ = _wrapped_init
    setattr(_patch_pymilvus_milvus_client_orm_alias, "_done", True)


class MilvusClientManager:
    """Milvus 客户端管理器"""

    COLLECTION_NAME: str = "biz"
    VECTOR_DIM: int = 1024
    ID_MAX_LENGTH: int = 100
    CONTENT_MAX_LENGTH: int = 8000
    DEFAULT_SHARD_NUMBER: int = 2

    def __init__(self) -> None:
        self._client: MilvusClient | None = None
        self._collection: Collection | None = None

    def connect(self) -> MilvusClient:
        """连接到 Milvus 服务器并初始化 collection"""
        if self._collection is not None and self._client is not None:
            logger.debug("Milvus 已连接，跳过重复 connect")
            return self._client

        try:
            _patch_pymilvus_milvus_client_orm_alias()

            logger.info(f"正在连接到 Milvus: {config.milvus_host}:{config.milvus_port}")

            connections.connect(
                alias="default",
                host=config.milvus_host,
                port=str(config.milvus_port),
                timeout=config.milvus_timeout / 1000,
            )

            uri = f"http://{config.milvus_host}:{config.milvus_port}"
            self._client = MilvusClient(uri=uri)
            logger.info("成功连接到 Milvus")

            if not self._collection_exists():
                logger.info(f"collection '{self.COLLECTION_NAME}' 不存在，正在创建...")
                self._create_collection()
            else:
                logger.info(f"collection '{self.COLLECTION_NAME}' 已存在")
                self._collection = Collection(self.COLLECTION_NAME)

                schema = self._collection.schema
                for field in schema.fields:
                    if field.name == "vector":
                        if hasattr(field, 'params') and 'dim' in field.params:
                            existing_dim = field.params['dim']
                            if existing_dim != self.VECTOR_DIM:
                                logger.warning(f"向量维度不匹配: {existing_dim} vs {self.VECTOR_DIM}，重建 collection")
                                _ = utility.drop_collection(self.COLLECTION_NAME)
                                self._create_collection()
                        break

            self._load_collection()
            return self._client

        except MilvusException as e:
            logger.error(f"Milvus 操作失败: {e}")
            self.close()
            raise RuntimeError(f"Milvus 操作失败: {e}") from e
        except Exception as e:
            logger.error(f"连接 Milvus 失败: {e}")
            self.close()
            raise RuntimeError(f"连接 Milvus 失败: {e}") from e

    def _collection_exists(self) -> bool:
        result = utility.has_collection(self.COLLECTION_NAME)
        return bool(result)

    def _create_collection(self) -> None:
        fields = [
            FieldSchema(name="id", dtype=DataType.VARCHAR, max_length=self.ID_MAX_LENGTH, is_primary=True),
            FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=self.VECTOR_DIM),
            FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=self.CONTENT_MAX_LENGTH),
            FieldSchema(name="metadata", dtype=DataType.JSON),
        ]
        schema = CollectionSchema(fields=fields, description="RAG knowledge collection", enable_dynamic_field=False)
        self._collection = Collection(name=self.COLLECTION_NAME, schema=schema, num_shards=self.DEFAULT_SHARD_NUMBER)
        self._create_index()

    def _create_index(self) -> None:
        if self._collection is None:
            raise RuntimeError("Collection 未初始化")
        index_params = {"metric_type": "L2", "index_type": "IVF_FLAT", "params": {"nlist": 128}}
        _ = self._collection.create_index(field_name="vector", index_params=index_params)
        logger.info("成功为 vector 字段创建索引")

    def _load_collection(self) -> None:
        if self._collection is None:
            self._collection = Collection(self.COLLECTION_NAME)
        try:
            load_state = utility.load_state(self.COLLECTION_NAME)
            state_name = getattr(load_state, "name", str(load_state))
            if state_name != "Loaded":
                self._collection.load()
                logger.info(f"成功加载 collection '{self.COLLECTION_NAME}'")
            else:
                logger.info(f"Collection '{self.COLLECTION_NAME}' 已加载")
        except AttributeError:
            try:
                self._collection.load()
            except MilvusException as e:
                if "already loaded" not in str(e).lower():
                    raise

    def get_collection(self) -> Collection:
        if self._collection is None:
            raise RuntimeError("Collection 未初始化，请先调用 connect()")
        return self._collection

    def health_check(self) -> bool:
        try:
            if self._client is None:
                return False
            _ = connections.list_connections()
            return True
        except Exception as e:
            logger.error(f"Milvus 健康检查失败: {e}")
            return False

    def close(self) -> None:
        try:
            if self._collection is not None:
                self._collection.release()
                self._collection = None
        except Exception:
            pass
        try:
            if connections.has_connection("default"):
                connections.disconnect("default")
        except Exception:
            pass
        self._client = None
        logger.info("已关闭 Milvus 连接")


milvus_manager = MilvusClientManager()
