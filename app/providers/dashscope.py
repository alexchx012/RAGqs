"""DashScope provider implementations."""

from __future__ import annotations

from langchain_qwq import ChatQwen
from loguru import logger
from openai import OpenAI


class DashScopeChatModelProvider:
    """DashScope chat model provider using ChatQwen."""

    def __init__(
        self,
        api_key: str,
        model_name: str = "qwen-max",
        temperature: float = 0.7,
    ):
        self.api_key = api_key
        self.model_name = model_name
        self.temperature = temperature

    def create_chat_model(self, streaming: bool = True) -> ChatQwen:
        if not self.api_key or self.api_key == "your-api-key-here":
            raise ValueError("请设置环境变量 DASHSCOPE_API_KEY")

        return ChatQwen(
            model=self.model_name,
            api_key=self.api_key,
            temperature=self.temperature,
            streaming=streaming,
        )


class DashScopeEmbeddingProvider:
    """DashScope embedding provider using the OpenAI-compatible API."""

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-v4",
        dimensions: int = 1024,
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ):
        if not api_key or api_key == "your-api-key-here":
            raise ValueError("请设置环境变量 DASHSCOPE_API_KEY")

        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.dimensions = dimensions
        logger.info(f"DashScope Embeddings 初始化完成 - 模型: {model}, 维度: {dimensions}")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = self.client.embeddings.create(
                model=self.model,
                input=texts,
                dimensions=self.dimensions,
                encoding_format="float",
            )
            return [item.embedding for item in response.data]
        except Exception as e:
            logger.error(f"批量嵌入失败: {e}")
            raise RuntimeError(f"批量嵌入失败: {e}") from e

    def embed_query(self, text: str) -> list[float]:
        if not text or not text.strip():
            raise ValueError("查询文本不能为空")
        try:
            response = self.client.embeddings.create(
                model=self.model,
                input=text,
                dimensions=self.dimensions,
                encoding_format="float",
            )
            return response.data[0].embedding
        except Exception as e:
            logger.error(f"查询嵌入失败: {e}")
            raise RuntimeError(f"查询嵌入失败: {e}") from e
