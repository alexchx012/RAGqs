# 重新掌控 RAGqs 代码库教程

读者是项目所有者本人。目标不是从零学 RAG，而是重新建立“我知道问题从哪里进来、数据存在哪里、模型在哪里调用、坏了该看哪里”的控制感。

本教程只基于当前代码库文件，不描述未实现的理想架构。凡是代码里能看到但语义不确定的地方，统一标为“需要人工确认”。

## 1. 项目一句话说明

阅读顺序：先看 `README.md`，再看 `app/main.py`，再看 `app/services/rag_agent_service.py`。

一句话：本项目是一个 FastAPI + LangGraph + LangChain + Milvus 的知识库问答系统，支持文档上传入库、向量检索、显式 RAG 图编排、基础 Agent 工具调用、会话记录、检索审计和本地运维脚本。

从文件上看：

- `app/main.py::create_app` 是 Web 服务入口。
- `app/api/chat.py::chat` 和 `app/api/chat.py::chat_stream` 是用户提问入口。
- `app/api/file.py::upload_file` 是文档入库入口。
- `app/services/rag_agent_service.py::RagAgentService` 是 RAG/Agent 总调度。
- `app/agents/rag_graph.py::build_rag_state_graph` 是当前默认 Agent/RAG 状态图。

需要人工确认：`README.md` 中的项目定位是否仍是当前产品定位，还是只代表某个阶段的说明。

## 2. 本项目解决什么问题

阅读顺序：先看 `app/api/file.py`，再看 `app/services/vector_index_service.py`，再看 `app/api/chat.py`，最后看 `app/agents/rag_graph.py`。

它解决的是一条完整知识问答闭环：

1. 用户上传 `txt/md/markdown/csv/html/htm/json` 文档：入口在 `app/api/file.py::upload_file`。
2. 系统做上传安全检查：`app/security/uploads.py::secure_upload_payload`。
3. 系统加载、切分、生成元数据、写入向量库：`app/services/vector_index_service.py::VectorIndexService.run_indexing_job`。
4. 用户提问：`app/api/chat.py::chat` 或 `app/api/chat.py::chat_stream`。
5. 系统按知识空间过滤检索：`app/agents/rag_graph.py::RagGraphNodes.retrieve`。
6. 系统把检索 chunk 注入 prompt：`app/agents/rag_graph.py::_build_answer_prompt`。
7. 系统调用模型生成答案：`app/agents/rag_graph.py::ChatModelAnswerGenerator.generate` 或 `stream`。
8. 系统保存会话、检索审计和指标：`app/services/rag_agent_service.py::_record_session_exchange`、`_record_retrieval_audit`、`_record_rag_query_metric`。

你重新掌控它时，要优先盯住两条主线：提问链路和入库链路。其他模块是这两条链路的支撑。

需要人工确认：当前业务最关心的是“企业内部知识问答”、还是“可扩展 RAG Agent 框架”。这会影响后续改进路线。

## 3. 本项目当前能力边界

阅读顺序：先看 `openspec/changes/learn-rag-agent-codebase/Map.md`，再看 `openspec/changes/learn-rag-agent-codebase/risk-register.md`，再看 `tests/test_evaluation_foundation.py`。

当前可以确认的能力：

- 本地 FastAPI 服务：`app/main.py::create_app`。
- 静态前端：`static/index.html` 和 `static/app.js::RAGApp`。
- 文档上传和索引：`app/api/file.py::upload_file` 到 `app/services/vector_index_service.py::VectorIndexService.run_indexing_job`。
- 默认 Milvus 向量存储：`app/providers/milvus.py::MilvusVectorStoreProvider`，collection 固定为 `biz`。
- 默认 DeepSeek chat + DashScope embedding：`app/providers/deepseek.py`、`app/providers/dashscope.py`、`app/services/vector_embedding_service.py::build_vector_embedding_service`。
- 显式 LangGraph RAG 图：`app/agents/rag_graph.py::build_rag_state_graph`。
- 会话、审计、checkpoint 默认 SQLite：`app/providers/factory.py::create_default_provider_container` 和 `app/config.py::Settings`。
- fake/service/http/real preflight 评估框架：`app/evaluation/runner.py`、`app/evaluation/readiness.py`。

当前边界和限制：

- `app/security/auth.py::SimpleAuthProvider.authenticate` 默认 `AUTH_ENABLED=false`，本地是 `local-admin/admin/*`。
- `app/providers/milvus.py::MilvusVectorStoreProvider.similarity_search` 捕获检索异常后返回 `[]`，可能把故障伪装成无召回。
- `app/core/milvus_client.py::MilvusClientManager.connect` 遇到向量维度不匹配会 drop/recreate `biz`。
- `app/observability/metrics.py::RuntimeMetrics` 是进程内指标，多实例不会自动聚合。
- `.github/workflows/ci.yml` 运行基线和评估报告，但不证明真实 DeepSeek/DashScope/Milvus/Postgres 生产质量。

需要人工确认：`default` knowledge space 是否是全局检索空间，还是普通默认空间。

## 4. 本地如何启动

阅读顺序：先看 `start.ps1`，再看 `.env.example`，再看 `vector-database.yml`，最后看 `app/main.py`。

本地启动主入口是 `start.ps1`。它做四件事：

1. `start.ps1::Ensure-PythonDependencies` 检查 `.venv` 和 Python 依赖。
2. `start.ps1::Assert-AppConfiguration` 调用 `python -m app.operations.config_validation` 校验 `.env`。
3. `start.ps1::Start-MilvusStack` 使用 `vector-database.yml` 启动或复用 Milvus 相关容器。
4. `start.ps1::Start-FastApiForeground` 调用 Uvicorn 启动 `app.main:app`。

最直接的本地命令：

```powershell
uv venv
uv pip install -e ".[dev]"
copy .env.example .env
.\start.ps1
```

如果只想做启动前检查：

```powershell
.\start.ps1 -PreflightOnly
```

关键配置：

- `.env.example` 默认混合供应商：`CHAT_MODEL=deepseek-v4-pro` + `DEEPSEEK_API_KEY`（chat），以及 `DASHSCOPE_API_KEY` + `DASHSCOPE_EMBEDDING_MODEL`（embedding）。两者都要换成真实值，除非你改用 fake provider。
- `CHAT_PROVIDER` 可留空自动选择；只有同时存在两个有效 chat Key 才比较 DeepSeek-first 顺序。DashScope chat / OpenAI-compatible / fake 必须显式设置 `CHAT_PROVIDER`。
- `MILVUS_PORT` 默认是 `19530`，由 `vector-database.yml` 映射到 `milvus-standalone`。
- `HOST`、`PORT` 最终被 `app/main.py::build_uvicorn_options` 使用。

需要人工确认：`vector-database.yml` 使用 external network `milvus`，首次运行时这个网络是否总是已经存在。

## 5. 最小运行路径

阅读顺序：先看 `static/app.js`，再看 `app/api/chat.py`，再看 `app/services/rag_agent_service.py`，最后看 `app/agents/rag_graph.py`。

最小路径不是完整产品功能，而是确认“问答主链能跑通”：

1. 打开浏览器访问 `/`，由 `app/main.py::root` 返回 `static/index.html`。
2. `static/app.js::RAGApp.constructor` 初始化 UI 状态、会话 ID、知识空间和事件绑定。
3. 用户点击发送，进入 `static/app.js::RAGApp.sendMessage`。
4. 快速模式进入 `static/app.js::RAGApp.sendQuick`，请求 `POST /api/chat`。
5. 后端进入 `app/api/chat.py::chat`。
6. `chat` 调用 `app/services/rag_agent_service.py::RagAgentService.query_with_trace`。
7. 默认 runtime 是 `explicit_graph`，进入 `app/agents/rag_graph.py::build_rag_state_graph` 产出的图。
8. 图检索、生成答案、返回 `answer/sources/retrieval`。

如果你只想读最小链路，不要先看所有 provider。先抓住这四个文件：

- `static/app.js`
- `app/api/chat.py`
- `app/services/rag_agent_service.py`
- `app/agents/rag_graph.py`

需要人工确认：最小运行路径是否要求真实知识库已有数据；如果没有数据，`RagGraphNodes.handoff` 会返回“知识库中没有足够依据回答这个问题。”。

## 6. 用户提问的完整调用链

阅读顺序：先看 `app/api/chat.py`，再看 `app/services/rag_agent_service.py`，再看 `app/agents/rag_graph.py`，再看 `app/retrieval/pipeline.py`。

非流式链路：

```text
static/app.js::RAGApp.sendQuick
  -> POST /api/chat
  -> app/models/request.py::ChatRequest
  -> app/api/chat.py::chat
  -> app/security/auth.py::require_permission("chat:write")
  -> app/security/auth.py::require_space_access
  -> app/api/chat.py::_call_query_with_trace
  -> app/services/rag_agent_service.py::RagAgentService.query_with_trace
  -> app/services/rag_agent_service.py::_invoke_explicit_graph
  -> app/agents/rag_graph.py::RagGraphNodes.normalize_input
  -> app/agents/rag_graph.py::RagGraphNodes.decide_retrieval
  -> app/agents/rag_graph.py::RagGraphNodes.retrieve
  -> app/retrieval/pipeline.py::RetrievalPipeline.retrieve
  -> app/agents/rag_graph.py::RagGraphNodes.answer
  -> app/agents/rag_graph.py::RagGraphNodes.final_response
  -> app/services/rag_agent_service.py::_serialize_graph_state
  -> app/models/response.py::success_envelope
```

流式链路：

```text
static/app.js::RAGApp.sendStream
  -> POST /api/chat_stream
  -> app/api/chat.py::chat_stream
  -> app/services/rag_agent_service.py::RagAgentService.query_stream_with_trace
  -> app/services/rag_agent_service.py::_stream_explicit_graph
  -> app/agents/rag_graph.py::_stream_answer_tokens
  -> app/api/chat.py::format_stream_chunk
  -> SSE message
  -> static/app.js::RAGApp.sendStream
```

你要重点看两个转换点：

- 请求字段转换在 `app/models/request.py::ChatRequest`，前端发 `Id/Question/spaceId`。
- SSE chunk 转换在 `app/api/chat.py::format_stream_chunk`，graph 的 `token` 会变成前端的 `content`。

需要人工确认：外部 API 客户端应该以 HTTP status 为准，还是以 `app/models/response.py::ApiEnvelope.code` 和 `data.success` 为准。

## 7. 文档入库的完整调用链

阅读顺序：先看 `app/api/file.py`，再看 `app/security/uploads.py`，再看 `app/providers/ingestion.py`，再看 `app/services/vector_index_service.py`。

完整链路：

```text
static/app.js::RAGApp.handleFileUpload
  -> POST /api/upload?space_id=...
  -> app/api/file.py::upload_file
  -> app/security/auth.py::require_permission("document:upload")
  -> app/security/auth.py::require_space_access
  -> app/security/uploads.py::secure_upload_payload
  -> uploads/<safe_filename>
  -> app/api/file.py::_call_index_single_file
  -> app/providers/factory.py::get_default_provider_container().ingestion_provider
  -> app/providers/ingestion.py::VectorIndexIngestionProvider.index_file
  -> app/services/vector_index_service.py::VectorIndexService.index_single_file
  -> app/services/vector_index_service.py::VectorIndexService.run_indexing_job
  -> app/ingestion/loaders.py::DocumentLoaderRegistry.load
  -> app/ingestion/metadata.py::DocumentMetadataNormalizer.document_metadata
  -> app/services/document_splitter_service.py::DocumentSplitterService.split_document
  -> app/ingestion/metadata.py::DocumentMetadataNormalizer.chunk_metadata
  -> app/services/vector_store_manager.py::VectorStoreManager.add_documents
  -> app/providers/milvus.py::MilvusVectorStoreProvider.add_documents
  -> app/knowledge/catalog.py::*KnowledgeCatalog.upsert_from_job
```

入库时要盯住三类数据：

- 原始文件：写到 `uploads/`，路径由 `app/api/file.py::UPLOAD_DIR` 控制。
- chunk 元数据：由 `app/ingestion/metadata.py::DocumentMetadataNormalizer` 生成。
- 索引状态：由 `app/ingestion/job_store.py` 和 `app/knowledge/catalog.py` 保存，默认 SQLite 路径在 `app/config.py::Settings`。

需要人工确认：同名上传文件当前会覆盖 `uploads/<safe_filename>`，这是否符合业务预期。

## 8. RAG 检索链路详解

阅读顺序：先看 `app/agents/rag_graph.py::RagGraphNodes.retrieve`，再看 `app/providers/factory.py::create_default_provider_container`，再看 `app/retrieval/pipeline.py::RetrievalPipeline.retrieve`，最后看 `app/providers/milvus.py::MilvusVectorStoreProvider.similarity_search`。

检索从 graph 节点开始：

- `RagGraphNodes.retrieve` 构造 `RetrievalRequest`。
- `RetrievalRequest.query` 来自 `normalized_question`。
- `RetrievalRequest.top_k` 默认来自 `RagAgentService.retrieval_top_k`，配置是 `app/config.py::Settings.rag_top_k`。
- `RetrievalRequest.filters` 来自 `app/agents/rag_graph.py::_filters_from_space_id`。

Provider 装配在 `app/providers/factory.py::create_default_provider_container`：

- 先创建 `VectorStoreRetrieverProvider`。
- 再创建 `RetrievalProfileRegistry`。
- 再按 `RETRIEVAL_PROFILE` 构造 primary/additional retrievers。
- 最后包成 `RetrievalPipeline`。

`RetrievalPipeline.retrieve` 的顺序：

1. 可选 query rewrite：`LLMQueryRewriter.rewrite`。
2. primary/additional retriever 检索。
3. `_deduplicate_documents` 去重。
4. 可选 LLM rerank：`LLMReranker.rerank`。
5. 可选 context compress：`LLMContextCompressor.compress`。
6. 截断到 `top_k`。
7. `_source_from_document` 生成 `RetrievalSource`。

Milvus 搜索在 `app/providers/milvus.py::MilvusVectorStoreProvider.similarity_search`，它把 filters 传给 LangChain Milvus 的 `similarity_search`。

需要人工确认：当前 LangChain Milvus 版本对 dict `filter` 的支持是否符合预期，还是应改成 Milvus 表达式字符串。

## 9. Agent 决策链路详解

阅读顺序：先看 `app/services/rag_agent_service.py::RagAgentService.__init__`，再看 `app/agents/rag_graph.py::build_rag_state_graph`，再看 `app/agents/rag_graph.py::RagGraphNodes.decide_retrieval`，最后看 `app/agents/rag_graph.py::RagGraphNodes.tool`。

当前默认 Agent runtime：

- 配置在 `app/config.py::Settings.agent_runtime`，默认 `explicit_graph`。
- 归一化在 `app/services/rag_agent_service.py::_normalize_agent_runtime`。
- 图构建在 `app/services/rag_agent_service.py::_build_default_explicit_graph`。

显式图的决策顺序：

```text
normalize_input
  -> decide_retrieval
     -> tool, if state.tool_request.name exists (explicit only)
     -> handoff, if question is empty
     -> retrieve, default path
  -> retrieve / tool / handoff
  -> answer
     -> tool, if AIMessage.tool_calls (answer↔tool continuation)
     -> final_response / error_policy
  -> final_response
```

工具相关位置：

- 工具列表来自 `app/extensions/tools.py::build_enabled_tools`。
- 默认工具注册在 `app/extensions/tools.py::build_default_tool_registry`。
- 默认工具名配置在 `app/config.py::Settings.enabled_tools`。
- 工具执行在 `app/agents/rag_graph.py::LangChainToolExecutor.execute`。
- 模型在 answer 阶段通过 `bind_tools` 产出 tool_calls 后由 `route_after_answer` 进入 tool 续轮；无 pre-retrieval planner。

当前默认是“RAG 优先 + 显式 tool_request / answer 阶段 tool_calls”，没有独立的 pre-retrieval 工具规划开关。

## 10. 配置项详解

阅读顺序：先看 `app/config.py`，再看 `.env.example`，再看 `app/providers/factory.py`，最后看 `app/operations/config_validation.py`。

配置分组按这个顺序读：

| 配置组 | 文件/类 | 影响 |
| --- | --- | --- |
| 应用 | `app/config.py::AppConfig`, `.env.example` 的 `APP_*`, `HOST`, `PORT` | FastAPI 名称、版本、监听地址。 |
| CORS | `app/config.py::CorsConfig`, `app/security/cors.py::build_cors_options` | 浏览器跨域。 |
| 上传 | `app/config.py::UploadConfig`, `app/security/uploads.py` | 允许扩展名、最大大小、prompt injection 扫描开关。 |
| 部署 | `app/config.py::DeploymentConfig`, `app/operations/config_validation.py::_validate_production_settings` | local/staging/production 约束。 |
| 认证 | `app/config.py::AuthConfig`, `app/security/auth.py` | header auth、角色、知识空间权限。 |
| Runtime controls | `app/config.py::RuntimeConfig`, `app/security/runtime_controls.py` | 单进程并发和请求超时。 |
| Provider | `app/config.py::ProviderConfig`, `app/providers/selection.py` | chat/embedding/vector/session/audit/ingestion/checkpoint 选择；chat 可自动选择。 |
| DeepSeek | `app/config.py::DeepSeekConfig`, `app/providers/deepseek.py` | 默认候选 chat Key / base URL。 |
| Storage | `app/config.py::StorageConfig`, `app/providers/factory.py` | SQLite/Postgres 路径和 DSN。 |
| Agent | `app/config.py::AgentConfig`, `app/services/rag_agent_service.py` | runtime、工具、prompt profile。 |
| RAG | `app/config.py::RagConfig`, `app/retrieval/pipeline.py` | top_k、retrieval profile、rewrite/rerank/compress；无专用模型字段。 |
| Chat model | `app/config.py::Settings.chat_model` | 所有 chat provider 共用模型名 `CHAT_MODEL`。 |
| Chunking | `app/config.py::ChunkingConfig`, `app/services/document_splitter_service.py` | chunk size 和 overlap。 |
| Milvus | `app/config.py::MilvusConfig`, `app/core/milvus_client.py` | host、port、timeout。 |

配置验证入口：

- 启动脚本调用 `app/operations/config_validation.py::main`。
- 核心函数是 `app/operations/config_validation.py::validate_settings`。
- 生产硬化检查在 `_validate_production_settings`。

当前真相：业务 chat 模型 source of truth 是唯一的 `CHAT_MODEL`。`RAG_MODEL` / `DASHSCOPE_MODEL` / `OPENAI_COMPATIBLE_MODEL` 已删除；检索增强器需要 LLM 时复用 `ProviderContainer.chat_model_provider`。

## 11. 如何新增一种数据源

阅读顺序：先看 `app/ingestion/loaders.py`，再看 `app/services/vector_index_service.py`，再看 `app/security/uploads.py`，最后看 `tests/test_ingestion_foundation.py`。

这里的“数据源”如果是新的文件格式，接入点在 loader：

1. 在 `app/ingestion/loaders.py` 里实现 `DocumentLoader` 协议：`supports(path)` 和 `load(path)`。
2. 把新 loader 加到 `app/ingestion/loaders.py::DocumentLoaderRegistry.default`。
3. 如果是上传文件格式，把扩展名加到 `.env.example::UPLOAD_ALLOWED_EXTENSIONS` 和 `app/config.py::Settings.upload_allowed_extensions`。
4. 确认 `app/security/uploads.py::secure_upload_payload` 能处理这种文件内容；当前只接受 UTF-8 文本。
5. 确认 `app/services/vector_index_service.py::_load_document_metadata` 拼接出来的 `content` 是你希望进入 splitter 的文本。
6. 给 `tests/test_ingestion_foundation.py` 加 loader 测试，给 `tests/test_vector_index_service_ingestion.py` 加索引链路测试。

如果“数据源”不是文件，而是数据库/API：

- 仍然应转成 `langchain_core.documents.Document`，并填好 `source_path/file_name/_source/_file_name` 等元数据。
- 可以新增 provider，但要先看 `app/providers/ingestion.py::VectorIndexIngestionProvider` 和 `app/providers/contracts.py::IngestionProvider`。

需要人工确认：非文件型数据源应走 upload API，还是新增单独 ingestion API。

## 12. 如何新增一个工具

阅读顺序：先看 `app/extensions/tools.py`，再看 `app/tools/knowledge_tool.py`，再看 `app/agents/rag_graph.py::LangChainToolExecutor`，最后看 `tests/test_phase8_foundation_templates.py`。

新增工具的真实接入点是 `ToolRegistry`：

1. 在 `app/tools/` 下新增工具函数，参考 `app/tools/time_tool.py::get_current_time` 或 `app/tools/knowledge_tool.py::retrieve_knowledge`。
2. 在 `app/extensions/tools.py::build_default_tool_registry` 中注册工具名、callable、description。
3. 在 `.env.example::ENABLED_TOOLS` 和 `app/config.py::Settings.enabled_tools` 中决定是否默认启用。
4. `app/services/rag_agent_service.py::RagAgentService.__init__` 会调用 `build_enabled_tools` 构造工具列表。
5. 显式工具执行走 `app/agents/rag_graph.py::RagGraphNodes.tool` 和 `LangChainToolExecutor.execute`。
6. answer 阶段模型可通过 `bind_tools` 产出 tool_calls，由 `route_after_answer` / `route_after_tool` 完成 answer↔tool 续轮；`decide_retrieval` 不再做 pre-retrieval planner。

测试优先看：

- `tests/test_phase8_foundation_templates.py::test_tool_registry_registers_builtin_and_business_tools`
- `tests/test_rag_state_graph.py::test_langchain_tool_executor_invokes_registered_tools_by_name`
- `tests/test_rag_agent_graph_runtime.py` 中的 answer↔tool / streaming 相关用例

需要人工确认：新工具是否允许访问外部系统；如果允许，需要新增超时、权限和审计策略。

## 13. 如何替换向量库

阅读顺序：先看 `app/providers/contracts.py::VectorStoreProvider`，再看 `app/providers/milvus.py`，再看 `app/providers/factory.py`，最后看 `app/providers/selection.py`。

替换向量库不是改 retrieval pipeline，而是新增一个 `VectorStoreProvider`：

1. 在 `app/providers/contracts.py` 确认必须实现的方法：`add_documents`、`delete_by_source`、`delete_by_document_id`、`similarity_search(query, k, filters)`。
2. 参考 `app/providers/milvus.py::MilvusVectorStoreProvider` 新增 provider 文件。
3. 在 `app/providers/selection.py::SUPPORTED_VECTOR_STORE_PROVIDERS` 加 provider id。
4. 在 `app/providers/factory.py::create_default_provider_container` 的 vector store 分支中按配置创建新 provider。
5. 在 `.env.example` 和 `app/config.py::Settings.vector_store_provider` 中文档化新 provider。
6. 为新 provider 加 tests，先看 `tests/test_provider_contracts.py` 和 `tests/test_provider_factory.py`。

不要跳过 filters。知识空间隔离依赖 `similarity_search(..., filters=request.filters)`，调用点在 `app/providers/retrieval.py::VectorStoreRetrieverProvider.retrieve`。

需要人工确认：新向量库的 metadata filter 语法是否能完整表达 `space_id`，以及是否支持按 `document_id/source_path` 删除。

## 14. 如何替换大模型

阅读顺序：先看 `app/providers/contracts.py::ChatModelProvider`，再看 `app/providers/deepseek.py`，再看 `app/providers/dashscope.py`，再看 `app/providers/openai_compatible.py`，最后看 `app/providers/factory.py`。

大模型替换分两类：chat model 和 embedding model。

Chat model：

1. 实现 `app/providers/contracts.py::ChatModelProvider.create_chat_model`。
2. 参考 `app/providers/deepseek.py::DeepSeekChatModelProvider`、`app/providers/dashscope.py::DashScopeChatModelProvider` 或 `app/providers/openai_compatible.py::OpenAICompatibleChatModelProvider`。
3. 在 `app/providers/selection.py::SUPPORTED_CHAT_PROVIDERS` 加 id。
4. 在 `app/providers/factory.py::create_default_provider_container` 加 chat provider 分支，并只传入共享 `CHAT_MODEL`。
5. 确认返回的模型支持 `invoke`；如果要流式，需要支持 `stream`，调用点在 `app/agents/rag_graph.py::ChatModelAnswerGenerator.stream` / `stream_ai_message`。
6. 如果要 answer↔tool 续轮，模型需要支持 `bind_tools` 与 tool_call 输出；流式路径还必须把 tool_call 组装进最终 AIMessage（见 `app/providers/deepseek.py::_stream`）。
7. 扩展边界是 `ProviderContainer.chat_model_provider`，不是某个厂商专属 chat provider 名称。

Embedding model：

1. 实现 `app/providers/contracts.py::EmbeddingProvider.embed_documents` 和 `embed_query`。
2. 参考 `app/providers/dashscope.py::DashScopeEmbeddingProvider` 和 `app/providers/openai_compatible.py::OpenAICompatibleEmbeddingProvider`。
3. DashScope embedding 使用 `DASHSCOPE_EMBEDDING_MODEL`，与 `CHAT_MODEL` 独立。
4. 如果 embedding 维度变化，必须看 `app/core/milvus_client.py::MilvusClientManager.VECTOR_DIM`。

需要人工确认：替换 embedding 维度时是否允许重建 Milvus collection `biz`，因为当前代码可能 drop/recreate。

## 15. 如何调试一次失败的问答

阅读顺序：先看 `app/api/chat.py`，再看 `app/services/rag_agent_service.py`，再看 `app/agents/rag_graph.py`，再看 `app/observability/retrieval_audit.py`。

按症状定位：

1. 前端报错：看 `static/app.js::RAGApp.sendQuick` 或 `sendStream` 如何解析 `data.code`、`data.data.success`、SSE `error`。
2. API 权限失败：看 `app/security/auth.py::require_permission` 和 `require_space_access`。
3. API 普通异常：看 `app/api/chat.py::chat` 的 exception handler；它会返回 `error_envelope`。
4. graph 节点失败：看 `app/agents/rag_graph.py::_error_update`，错误会进入 `errors` 和 `error_policy`。
5. 检索失败但没有报错：重点看 `app/providers/milvus.py::MilvusVectorStoreProvider.similarity_search`，它可能返回空列表。
6. 模型失败：看 `app/agents/rag_graph.py::ChatModelAnswerGenerator.generate` 或 `stream`。
7. 审计记录：看 `app/services/rag_agent_service.py::_record_retrieval_audit` 和 `app/api/chat.py::list_retrieval_audits`。
8. 指标：看 `app/services/rag_agent_service.py::_record_rag_query_metric` 和 `app/observability/metrics.py::RuntimeMetrics.record_rag_query`。

调试时先找 trace：

- `app/observability/request_context.py::install_request_context_middleware` 会处理 `X-Trace-Id`。
- 审计记录结构是 `app/observability/retrieval_audit.py::RetrievalAuditRecord`。

需要人工确认：失败时是否要求 HTTP status 反映错误；当前部分错误只体现在 envelope 内。

## 16. 如何定位召回差的问题

阅读顺序：先看 `app/services/vector_index_service.py`，再看 `app/retrieval/pipeline.py`，再看 `app/providers/milvus.py`，最后看 `tests/test_retrieval_pipeline.py`。

召回差先分三类：没入库、检索不到、排序/截断丢失。

查没入库：

- 看 `app/api/file.py::upload_file` 是否成功返回 indexing job。
- 看 `app/services/vector_index_service.py::VectorIndexService.run_indexing_job` 是否生成 documents。
- 看 `app/ingestion/job_store.py` 的 job 是否 failed/partial。
- 看 `app/knowledge/catalog.py` 是否记录文档。

查检索不到：

- 看 `app/agents/rag_graph.py::_filters_from_space_id` 是否加了你预期的 `space_id` filter。
- 看 `app/providers/retrieval.py::VectorStoreRetrieverProvider.retrieve` 是否把 filters 传入 vector store。
- 看 `app/providers/milvus.py::MilvusVectorStoreProvider.similarity_search` 是否吞掉异常。
- 看 `app/retrieval/profiles.py::RetrievalProfile`，确认是否开启 `high_recall`。

查排序/截断：

- 看 `app/retrieval/pipeline.py::RetrievalPipeline.retrieve` 中 `top_k`、`_deduplicate_documents`、`LLMReranker.rerank`、`LLMContextCompressor.compress`。
- 看 `app/services/rag_agent_service.py::RagAgentService.retrieval_top_k`，配置来自 `app/config.py::Settings.rag_top_k`。

需要人工确认：召回差是向量相似度问题、chunk 切分问题、metadata filter 问题，还是用户问题需要 query rewrite。

## 17. 如何定位幻觉问题

阅读顺序：先看 `app/agents/rag_graph.py::_build_answer_prompt`，再看 `app/prompts/profiles.py`，再看 `app/retrieval/pipeline.py::_source_from_document`，最后看 `app/evaluation/metrics.py`。

先确认模型到底看到了什么：

- `app/agents/rag_graph.py::_build_answer_prompt` 把 retrieved documents 拼成 `Retrieved context`。
- 如果 `retrieval_result.documents` 为空，`RagGraphNodes.route_after_retrieval` 应进入 `handoff`，不应调用模型回答。
- `app/prompts/profiles.py::build_system_prompt` 决定 system prompt 风格。
- `app/services/rag_agent_service.py::_serialize_graph_state` 会把 sources 和 retrieval debug 返回给 API。

定位步骤：

1. 从 `/api/chat/audits` 看这次回答的 `sources` 和 `retrieval`，API 在 `app/api/chat.py::list_retrieval_audits`。
2. 如果 sources 不相关，按第 16 节定位召回。
3. 如果 sources 相关但答案超出上下文，看 `app/agents/rag_graph.py::_build_answer_prompt` 和 `app/prompts/profiles.py`。
4. 如果答案引用缺失，看 `app/retrieval/pipeline.py::_source_from_document` 是否生成了 `source_path/file_name/chunk_id/document_id`。
5. 如果要系统性评估，看 `app/evaluation/metrics.py::evaluate_results` 和 `app/evaluation/judges.py::ModelFaithfulnessJudge`。

需要人工确认：产品是否要求答案必须带显式引用编号；当前 prompt 注入了 `[n] source=...`，但并未强制最终答案格式必须引用 `[n]`。

## 18. 如何读测试

阅读顺序：先看 `scripts/validate-baseline.ps1`，再看 `pyproject.toml`，再按模块看 `tests/`。

先理解测试入口：

- `pyproject.toml::[tool.pytest.ini_options]` 指定 `testpaths = ["tests"]`，默认带 `--cov=app`。
- `.github/workflows/ci.yml` 在 Windows 上安装依赖后运行 `scripts/validate-baseline.ps1 -SkipPreflight`。
- 前端测试是 `tests/chat-history.test.js`，用 Node 运行。

按问题读测试：

- 问答主链：`tests/test_chat_retrieval_trace.py`、`tests/test_rag_agent_graph_runtime.py`。
- graph 节点：`tests/test_rag_state_graph.py`。
- 文档入库：`tests/test_file_upload_ingestion.py`、`tests/test_vector_index_service_ingestion.py`、`tests/test_ingestion_foundation.py`。
- 检索增强：`tests/test_retrieval_pipeline.py`、`tests/test_retrieval_profiles.py`、`tests/test_retrieval_enhancers.py`。
- Provider：`tests/test_provider_contracts.py`、`tests/test_provider_factory.py`。
- 安全：`tests/test_authz_foundation.py`、`tests/test_upload_security.py`。
- 运维：`tests/test_phase7_operations.py`、`tests/test_provider_aware_health.py`。
- 评估：`tests/test_evaluation_foundation.py`。
- 前端：`tests/chat-history.test.js`。

读测试时不要只看断言，也要看 fake 对象。很多测试通过 fake provider 证明软件路径，不证明真实模型或真实 Milvus 质量。

需要人工确认：哪些测试属于必须长期稳定的契约测试，哪些只是阶段性 baseline 防回归测试。

## 19. 当前代码库的风险与技术债

阅读顺序：先看 `openspec/changes/learn-rag-agent-codebase/risk-register.md`，再看 `openspec/changes/learn-rag-agent-codebase/Map.md`，再看对应源码文件。

优先级最高的风险：

1. 认证默认关闭：`app/config.py::Settings.auth_enabled=False`，`app/security/auth.py::SimpleAuthProvider._default_context` 默认 admin/all spaces。
2. `default` space 语义不清：`app/agents/rag_graph.py::_filters_from_space_id` 对 default 不加 filter。
3. 检索异常静默降级：`app/providers/milvus.py::MilvusVectorStoreProvider.similarity_search` 异常返回 `[]`。
4. Milvus 维度不匹配会重建 collection：`app/core/milvus_client.py::MilvusClientManager.connect`。
5. 上传 prompt injection 扫描很弱：`app/security/uploads.py::scan_prompt_injection`。
6. 同名上传覆盖：`app/api/file.py::upload_file` 对已存在文件先 `unlink`。
7. provider contract 不完全一致：`app/services/vector_store_manager.py::VectorStoreManager.similarity_search` 缺少 `filters`。
8. 指标进程内聚合：`app/observability/metrics.py::RuntimeMetrics`。
9. CI 不覆盖真实依赖质量：`.github/workflows/ci.yml` 和 `scripts/validate-baseline.ps1`。
10. legacy runtime 是否继续支持不清：`app/services/rag_agent_service.py::_run_legacy_query`。

处理这些风险时，不要直接改代码。先为每一类行为变化创建新的 OpenSpec change，至少回答影响 RAG、Agent、数据入口、配置、测试和回滚方式。

需要人工确认：这些风险中哪些是马上影响试运行，哪些只是生产化前债务。

## 20. 推荐的下一轮改进路线

阅读顺序：先看 `openspec/changes/learn-rag-agent-codebase/tasks.md`，再看 `openspec/changes/learn-rag-agent-codebase/risk-register.md`，再看 `openspec/specs/*/spec.md`。

下一轮不要从重构开始，应按“先恢复控制，再降低风险，再扩展能力”推进。

第一组：确认产品语义

- 明确 `default` knowledge space 是否全局检索，涉及 `app/agents/rag_graph.py::_filters_from_space_id` 和 `app/services/rag_agent_service.py::retrieve_context`。
- 明确 `legacy` runtime 是否保留，涉及 `app/services/rag_agent_service.py::_run_legacy_query`。
- 模型配置已统一：`CHAT_MODEL` 是唯一 chat 模型来源；RAG 无专用模型变量；检索增强器复用 `chat_model_provider`。后续只需确认业务默认模型 ID 是否仍用 `deepseek-v4-pro`。

第二组：修复高风险行为

- 把 Milvus 检索失败从“空结果”改成显式错误，涉及 `app/providers/milvus.py::similarity_search`、`app/agents/rag_graph.py::_error_update`、`tests/test_rag_state_graph.py`。
- 为 production auth 增加更明确门禁，涉及 `app/operations/config_validation.py::_validate_production_settings` 和 `tests/test_phase7_operations.py`。
- 处理 Milvus collection drop/recreate 风险，涉及 `app/core/milvus_client.py::MilvusClientManager.connect`。

第三组：增强可观测和诊断

- 前端展示 retrieval/tool/error_policy 事件，涉及 `static/app.js::RAGApp.sendStream` 和 `app/api/chat.py::format_stream_chunk`。
- 给召回差提供诊断输出，涉及 `app/retrieval/pipeline.py::RetrievalPipeline.retrieve` 的 debug 字段。
- 为审计数据定义保留和脱敏策略，涉及 `app/observability/retrieval_audit.py`。

第四组：扩展能力

- 新增数据源前先稳定 `app/ingestion/loaders.py::DocumentLoaderRegistry`。
- 新增工具前先稳定 `app/extensions/tools.py::ToolRegistry` 和 `app/agents/rag_graph.py::LangChainToolExecutor` / answer↔tool 续轮。
- 替换向量库前先统一 `app/providers/contracts.py::VectorStoreProvider` 和 `app/services/vector_store_manager.py` 的签名。

需要人工确认：下一轮目标是“内部试运行稳定”，还是“架构扩展性优先”。两者的 OpenSpec change 拆分方式不同。
