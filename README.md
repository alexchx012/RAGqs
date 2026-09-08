# RAGqs

**面向企业内部知识场景的企业级 RAG 问答与知识运营平台。**

RAGqs 不是把文档丢进向量库再调用大模型的演示项目，而是围绕企业知识的真实生命周期构建的一套平台：文档摄取、版本发布、权限控制、混合检索、引用回答、质量评估、成本治理、通知、备份恢复和生产运维都纳入同一套业务与数据契约。

它主要展示两类能力：

- **企业级 RAG 工程能力：** 将准确性、权限、可恢复性、可审计性和运营约束落实到持久化状态机、worker、事实源和发布门禁中。
- **RAG 算法与效果能力：** 组合混合召回、查询改写、HyDE、Contextual Retrieval、两阶段 rerank、树搜索、知识图谱路由和生成侧自评，形成可评估、可回滚的检索生成链路。

> V1 采用严格单租户部署：一个实例服务一家企业。每个部署使用独立 PostgreSQL、对象存储、索引命名空间和管理员清单，不在请求中传递 `tenant_id` 作为隔离手段。

## 它解决什么问题

企业知识问答真正困难的部分，通常不只是“找到相似文本”：

- 谁可以看到这段知识？引用、预览和下载是否仍然满足权限？
- 新版本索引构建失败时，旧版本能否继续服务？
- worker 崩溃、网络断开或 provider 超时后，如何避免重复生成、重复计费和错误发布？
- 检索策略或模型升级后，如何证明效果变好，并在失败时回滚？
- PostgreSQL、对象存储和外部索引不一致时，哪一方代表真实业务事实？
- 企业如何知道每次模型调用花了什么成本，如何处理未知 provider 结果？

RAGqs 将这些问题作为系统设计的一部分，而不是部署后的人工约定。

## 端到端闭环

```text
文档上传 / 投稿审批
        │
        ▼
持久化 ingestion job
        │
        ▼
解析 / OCR / VLM / 结构化切块 / embedding / Contextual Retrieval
        │
        ▼
暂存向量、稀疏、树和图谱派生数据
        │
        └──────────────► 验收通过后原子激活 publication

用户提问
    │
    ▼
认证与知识空间 ACL
    │
    ▼
混合召回 / 查询理解 / rerank / 树搜索 / 图谱路由
    │
    ▼
生成 + 引用校验 + 可选自评与有界重检索
    │
    ▼
持久化 generation events ──► SSE / Last-Event-ID 断线续传
    │
    ▼
反馈 / A-B 投票 / shadow evaluation / 指标与质量门禁
```

PostgreSQL 保存用户、ACL、文档版本、任务、会话、生成事件、用量、评估和审计等业务事实；S3 兼容对象存储保存原始内容和持久处理产物。Milvus、Meilisearch/OpenSearch、树索引、知识图谱和缓存均为可重建的派生数据。外部索引即使存在条目，没有匹配的 PostgreSQL `active publication` 也不会对查询可见。

## 核心工程设计

### 1. 用持久化任务替代进程内后台任务

摄取、生成、评估、图谱构建、通知和维护工作都以数据库状态为恢复依据。任务由持久化 lease 和 fencing token 认领；worker 重启后从 `pending`、到期重试、过期 lease 和 provider reconciliation 状态恢复，而不是依赖内存队列。

### 2. 用 fencing 阻止旧 worker 提交结果

worker 丢失租约或 fencing authority 后不能继续发布、收费、通知或写入终态。所有关键提交使用条件更新/事务边界，避免网络延迟或进程僵死导致“最后写入者获胜”。

### 3. 用 staging + gate + atomic activation 发布新版本

文档 publication 和索引 generation 都先在暂存空间构建。只有完成一致性检查、覆盖率检查、标识关联检查和 frozen retrieval acceptance 后，才原子切换 PostgreSQL 中的 active 指针。构建失败不会污染线上查询；上一代索引保留为限定窗口内的回滚候选。

### 4. 把事实源与派生数据明确分层

业务真相来自 PostgreSQL 与对象存储的联合事实，而不是外部索引。查询、预览、下载会根据 `document_id`、`document_version_id`、`publication_id` 和 generation 信息回查事实源。派生索引缺失或过期时，系统选择显式降级或失败，不从不完整索引推断业务事实。

### 5. 将生成执行与 HTTP/SSE 连接解耦

HTTP 请求创建 generation 身份，独立 generation worker 执行检索和 provider 调用，SSE 只订阅已持久化事件。客户端断开不会立即取消生成；重连时通过 `Last-Event-ID` 重放事件。执行 checkpoint、租约恢复和终态幂等保证不会重复创建用户消息或回答。

### 6. 对不确定的 provider 结果进行对账

连接超时不等于 provider 一定没有执行。系统保留不可变 `provider_call_id`、请求指纹和 deadline，将传输不确定状态置为 reconciliation；确认完成、确认未发送或到达 deadline 后，分别走对应状态转移，避免重复计费或静默丢失结果。

### 7. 把质量、成本和配置纳入发布身份

模型、embedding 维度和 metric、tokenizer、稀疏分析器、reranker 硬件、provider endpoint 等影响结果的配置都版本化。索引更换必须创建新 generation 并通过 acceptance gates；用量 ledger、价格版本、成本归属和配额分离保存，评估报告锁定策略和模型版本后再比较。

## RAG 能力栈

### 文档理解

支持文本、Markdown、CSV、XLSX、JSON/YAML/TOML/XML、代码，以及经 MinerU 处理的 PDF、Word、PowerPoint、HTML。摄取链可包含 OCR/VLM、表格结构保留、结构化切块、embedding、稀疏索引和 Contextual Retrieval。

### 检索与排序

- 向量 + 稀疏混合召回；
- 中文分词（jieba），以及可选 OpenSearch + IK；
- 查询改写、查询拆分和 HyDE；
- Contextual Retrieval 与前缀缓存；
- 两阶段 rerank；
- PageIndex 树搜索；
- 公共知识图谱路由；
- 会话历史检索；
- generation 级索引一致性和发布门禁。

### 生成侧控制

提供 `quick`、`think`、`deep` 努力档位。`think`/`deep` 可在有限预算内进行自评；草稿未通过时，可以重写查询、重新检索并再次生成。未通过自评的草稿不会直接发布。回答带引用，并支持反馈、候选比较和 A/B 投票。

## 系统架构

### 技术栈

- 后端：Python 3.11+、FastAPI、SQLAlchemy 2、Alembic、Pydantic 2、Uvicorn；
- 前端：React 19、TypeScript、Vite、Tailwind CSS、Radix UI、React Router；
- 事实源：PostgreSQL + 私有 S3 兼容对象存储；
- 检索：Milvus 向量索引，Meilisearch 稀疏索引，或显式配置的 OpenSearch + IK；
- 模型：OpenAI-compatible HTTP，生成、embedding、Contextual Retrieval、VLM、reranker 和 judge 独立配置；
- 测试：pytest/pytest-asyncio、Vitest、Testing Library、MSW、Playwright。

### 后端代码边界

| 目录 | 主要职责 |
| --- | --- |
| `app/api/v1` | 版本化 HTTP API、依赖和错误处理 |
| `app/platform` | 配置、运行时、数据库、provider、对象存储、HTTP contract、可观测性 |
| `app/identity` | 认证、会话、组织、角色、空间 ACL、撤销和账号生命周期 |
| `app/documents` | 上传、投稿、审批、文档版本、预览、摄取任务和 worker |
| `app/indexing` | 解析处理、混合检索、Contextual Retrieval、rerank、树搜索和索引代际 |
| `app/chat` | 会话、generation、SSE、反馈、A/B 和 generation worker |
| `app/agents` | 自评和有界重检索循环 |
| `app/evaluation` | judge、shadow run、指标、策略快照和校准 |
| `app/graph` | 图谱抽取、构建运行、存储和可用性 |
| `app/usage` | 计量 ledger、价格、账单、预算、配额和对账 |
| `app/outbox` | 事件发布、通知物化、投递、压缩和退休 |
| `app/backup` / `app/retention` | 备份恢复、保留、删除、GC 交接和对账 |

### 三条独立业务流水线

1. **摄取：** 上传/投稿 → 持久化 job → ingestion worker → 解析与索引 → 暂存 → 原子激活。
2. **查询：** 认证/ACL → 检索 → 排序/树/图 → 生成/引用/自评 → 持久化事件 → SSE。
3. **评估：** 外部调度 → 持久化 shadow run → evaluation worker → judge → 不可变报告。

生产参考拓扑将 API/query、ingestion、generation、evaluation、graph 和 maintenance 作为独立进程、权限、资源和伸缩边界。API 不在进程内同步执行评估批次，也不承担摄取或 provider 生成工作。

## 企业治理与可靠性

- 检索前解析服务端 ACL，引用、预览、下载前再次校验可见性；
- 使用结构化错误、幂等键、expected version 和审计记录；
- provider 重试采用统一上限、absolute deadline 和 `provider + operation` 级 circuit breaker；
- Transactional Outbox 支持重试、dead letter 和受保护重放；
- 用量账本不可变，支持 provider reconciliation 和配额不变量检查；
- 生产配置拒绝 fake/memory provider、debug 和不完整能力；
- 日志不输出 prompt、答案、文档内容、凭据和 provider 原始响应；
- 通过 request、trace、job、execution、publication、generation、index generation 和 backup ID 关联操作；
- 备份恢复先恢复 PostgreSQL 和对象存储，再按固定顺序重建派生索引。

## 本地开发

### 后端

```powershell
uv venv
.venv\Scripts\activate
uv pip install -e ".[dev]"
python -m uvicorn app.main:app --host 0.0.0.0 --port 9900 --reload
```

### 前端

```powershell
Set-Location frontend
npm ci
npm run dev
```

前端构建产物输出到仓库根目录的 `static/`。Compose 提供 Milvus、Meilisearch 及相关开发依赖，但不包含 RAGqs API、PostgreSQL 或业务对象存储。真实混合检索需要按环境配置 embedding provider、Milvus 和稀疏检索 provider。

## 生产部署边界

参考形态为 Linux 容器 + Kubernetes，也可以映射到其他编排平台，但必须保留独立事实源、资源隔离、lease/fencing、恢复和网络权限边界。生产通常需要：

- 独立 PostgreSQL 和私有 S3 命名空间；
- Milvus collection 前缀和稀疏索引；
- Secret manager、管理员清单和 HTTPS ingress；
- 独立的 API/query、ingestion、generation、evaluation、graph、maintenance workload；
- 外部 CronJob/workflow 触发评估、备份和维护；
- 保留 `Authorization`、`Origin`、`X-CSRF-Token`、`Idempotency-Key`、`Last-Event-ID`，并关闭 SSE 响应缓冲。

仓库提供源码、迁移、worker 入口和检索依赖 Compose，不提供通用的一键生产镜像或 Helm Chart。完整部署、发布、回滚、RPO/RTO 和故障处理契约见 [`docs/deployment.md`](docs/deployment.md) 与 [`docs/operations.md`](docs/operations.md)。

## 验证与测试

后端测试按领域覆盖迁移、并发、租约/fencing、上传安全、SSE、provider contract、索引代际、评估、配额、备份恢复和删除流程。前端使用 Vitest、Testing Library、MSW 和 Playwright。

```powershell
uv run --locked --extra dev --extra postgres python -m ruff check app alembic tests
uv run --locked --extra dev --extra postgres python -m black --check app alembic tests
uv run --locked --extra dev --extra postgres python -m isort --check-only app alembic tests
uv run --locked --extra dev --extra postgres python -m mypy app
uv run --locked --extra dev --extra postgres python -m pytest -q

Set-Location frontend
npm run test
npm run build
npm run test:e2e
```

配置 `RAGQS_TEST_POSTGRES_URL` 与 `RAGQS_TEST_S3_*` 后，集成测试覆盖 PostgreSQL、迁移、API/worker 和 S3 兼容对象存储；真实 Meilisearch + Milvus 后端另有独立验收门禁。

## 项目状态

项目当前版本为 `0.1.0`，仍处于快速迭代阶段，接口、配置和迁移可能继续演进。生产使用前请固定提交版本，并按部署文档完成完整验收。

## License

本项目基于 [Apache License 2.0](LICENSE) 发布。
