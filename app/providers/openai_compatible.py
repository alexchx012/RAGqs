"""OpenAI-compatible provider implementations."""

from __future__ import annotations

from langchain_openai import ChatOpenAI
from openai import OpenAI

from app.providers.selection import is_valid_secret


class OpenAICompatibleChatModelProvider:
    """Chat provider for OpenAI-compatible chat-completions APIs."""

    def __init__(
        self,
        api_key: str,
        model_name: str,
        base_url: str = "",
        temperature: float = 0.7,
    ):
        self.api_key = api_key
        self.model_name = model_name
        self.base_url = base_url
        self.temperature = temperature

    def create_chat_model(self, streaming: bool = True) -> ChatOpenAI:
        if not is_valid_secret(self.api_key):
            raise ValueError("OPENAI_COMPATIBLE_API_KEY is required")
        if not self.model_name:
            raise ValueError("model_name is required")
        kwargs = {
            "model": self.model_name,
            "api_key": self.api_key,
            "temperature": self.temperature,
            "streaming": streaming,
        }
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return ChatOpenAI(**kwargs)


class OpenAICompatibleEmbeddingProvider:
    """Embedding provider for OpenAI-compatible embeddings APIs."""

    def __init__(
        self,
        api_key: str,
        model: str,
        dimensions: int = 1024,
        base_url: str = "",
    ):
        if not is_valid_secret(api_key):
            raise ValueError("OPENAI_COMPATIBLE_API_KEY is required")
        if not model:
            raise ValueError("OPENAI_COMPATIBLE_EMBEDDING_MODEL is required")
        client_kwargs = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        self.client = OpenAI(**client_kwargs)
        self.model = model
        self.dimensions = dimensions

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = self.client.embeddings.create(
            model=self.model,
            input=texts,
            dimensions=self.dimensions,
            encoding_format="float",
        )
        return [item.embedding for item in response.data]

    def embed_query(self, text: str) -> list[float]:
        if not text or not text.strip():
            raise ValueError("query text must not be empty")
        response = self.client.embeddings.create(
            model=self.model,
            input=text,
            dimensions=self.dimensions,
            encoding_format="float",
        )
        return response.data[0].embedding
