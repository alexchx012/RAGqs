# Provider、模型与 Embedding 装配

该图描述 `create_default_provider_container()` 如何根据配置装配 chat、embedding、vector store、retrieval、session、audit、ingestion 和 checkpoint provider。

默认关系：**DeepSeek chat + DashScope embedding**。`CHAT_PROVIDER` 留空时按有效 Key 自动选择 chat provider（双有效 chat Key 时 DeepSeek-first）；所有 chat 分支只使用共享 `CHAT_MODEL`。DashScope embedding 使用独立的 `DASHSCOPE_EMBEDDING_MODEL`，与 `CHAT_MODEL` 解耦。检索增强器需要 LLM 时复用 `ProviderContainer.chat_model_provider`。

```mermaid
flowchart TD
    A["app/config.py<br/>Settings grouped config<br/>CHAT_MODEL + optional CHAT_PROVIDER"] --> B["app/providers/selection.py<br/>ProviderSelection / validate_provider_selection()"]
    B --> C["app/providers/factory.py<br/>create_default_provider_container()"]
    C --> D{"chat_provider<br/>blank = auto by keys"}
    D -- deepseek --> E["app/providers/deepseek.py<br/>DeepSeekChatModelProvider<br/>model = CHAT_MODEL"]
    D -- dashscope --> E2["app/providers/dashscope.py<br/>DashScopeChatModelProvider<br/>model = CHAT_MODEL"]
    D -- openai-compatible --> F["app/providers/openai_compatible.py<br/>OpenAICompatibleChatModelProvider<br/>model = CHAT_MODEL"]
    D -- fake --> G["app/providers/fakes.py<br/>FakeChatModelProvider"]
    C --> H{"embedding_provider"}
    H -- dashscope --> I["DashScopeEmbeddingProvider<br/>DASHSCOPE_EMBEDDING_MODEL"]
    H -- openai-compatible --> J["OpenAICompatibleEmbeddingProvider"]
    H -- fake --> K["FakeEmbeddingProvider"]
    C --> L{"vector_store_provider"}
    L -- milvus --> M["app/providers/milvus.py<br/>MilvusVectorStoreProvider<br/>collection = biz"]
    L -- fake --> N["FakeVectorStoreProvider"]
    C --> O["RetrievalProfileRegistry<br/>default / high_recall"]
    O --> P["app/retrieval/pipeline.py<br/>RetrievalPipeline<br/>LLM enhancers reuse chat_model_provider"]
    C --> Q{"storage/session/audit/checkpoint"}
    Q -- sqlite --> R["SQLite session / audit / checkpoint / catalog"]
    Q -- postgres --> S["Postgres session / audit / checkpoint / catalog"]
    C --> T["app/providers/ingestion.py<br/>VectorIndexIngestionProvider"]
    E --> U["ProviderContainer<br/>chat_model_provider boundary"]
    E2 --> U
    F --> U
    G --> U
    I --> U
    J --> U
    K --> U
    M --> U
    N --> U
    P --> U
    R --> U
    S --> U
    T --> U
```
