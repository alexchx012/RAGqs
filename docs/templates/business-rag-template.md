# Business RAG Template

Use this checklist to create a second business RAG agent while keeping the foundation reusable.

## Configuration

Create a business-specific `.env` from `.env.example` and set:

```env
# 默认聊天：DEEPSEEK_API_KEY 存在时自动选择 DeepSeek。
# CHAT_PROVIDER 留空即可；DashScope chat / openai_compatible / fake 必须显式设置。
CHAT_PROVIDER=
CHAT_MODEL=deepseek-v4-pro
DEEPSEEK_API_KEY=your-api-key
DEEPSEEK_BASE_URL=https://api.deepseek.com

# 默认向量化：DashScope embedding（与 chat provider 独立）。
EMBEDDING_PROVIDER=dashscope
DASHSCOPE_API_KEY=your-api-key
DASHSCOPE_EMBEDDING_MODEL=text-embedding-v4

VECTOR_STORE_PROVIDER=milvus
SESSION_STORE_PROVIDER=sqlite
RETRIEVAL_AUDIT_STORE_PROVIDER=sqlite
DEPLOYMENT_ENVIRONMENT=local
SESSION_STORE_SQLITE_PATH=data/sessions.sqlite3
SESSION_STORE_POSTGRES_DSN=
RETRIEVAL_AUDIT_SQLITE_PATH=data/retrieval-audits.sqlite3
RETRIEVAL_AUDIT_POSTGRES_DSN=
UPLOAD_ALLOWED_EXTENSIONS=txt,md,markdown,csv,html,htm,json
INGESTION_PROVIDER=vector_index
INDEXING_EXECUTION_MODE=sync
INDEXING_QUEUE_PROVIDER=sqlite
INDEXING_QUEUE_SQLITE_PATH=data/indexing-queue.sqlite3
INDEXING_QUEUE_POSTGRES_DSN=
INDEXING_QUEUE_LEASE_TIMEOUT_SECONDS=300.0
INDEXING_WORKER_POLL_INTERVAL_SECONDS=0.25
INDEXING_WORKER_SHUTDOWN_TIMEOUT_SECONDS=5.0
INDEXING_WORKER_RECOVER_PENDING_JOBS=true
INDEXING_JOB_STORE_PROVIDER=sqlite
INDEXING_JOB_STORE_SQLITE_PATH=data/indexing-jobs.sqlite3
INDEXING_JOB_STORE_POSTGRES_DSN=
DOCUMENT_CATALOG_PROVIDER=sqlite
DOCUMENT_CATALOG_SQLITE_PATH=data/document-catalog.sqlite3
DOCUMENT_CATALOG_POSTGRES_DSN=
CHECKPOINT_PROVIDER=sqlite
CHECKPOINT_SQLITE_PATH=data/checkpoints.sqlite3
CHECKPOINT_POSTGRES_DSN=
AGENT_RUNTIME=explicit_graph
ENABLED_TOOLS=retrieve_knowledge,get_current_time
PROMPT_PROFILE=strict
RAG_TOP_K=3
RETRIEVAL_PROFILE=default
RETRIEVAL_HIGH_RECALL_TOP_K_MULTIPLIER=2
RETRIEVAL_RELAXED_FILTER_PRESERVE_KEYS=space_id,spaceId,tenant_id,tenantId
QUERY_REWRITER_PROVIDER=none
RERANKER_PROVIDER=none
CONTEXT_COMPRESSOR_PROVIDER=none
CONTEXT_COMPRESSOR_MAX_CHARACTERS=1200
```

Only compare DeepSeek-first auto order when two valid chat keys are present. A single
valid chat key is selected without priority comparison.

For local test doubles, switch to:

```env
CHAT_PROVIDER=fake
EMBEDDING_PROVIDER=fake
VECTOR_STORE_PROVIDER=fake
INGESTION_PROVIDER=fake
```

For OpenAI-compatible endpoints, set `CHAT_PROVIDER=openai_compatible` or
`EMBEDDING_PROVIDER=openai_compatible`, then configure `OPENAI_COMPATIBLE_API_KEY`,
`OPENAI_COMPATIBLE_BASE_URL`, shared `CHAT_MODEL` for chat, and
`OPENAI_COMPATIBLE_EMBEDDING_MODEL` for embeddings. There is no
`OPENAI_COMPATIBLE_MODEL` / `RAG_MODEL` / `DASHSCOPE_MODEL` setting.

For multi-instance persistence, install the optional Postgres dependency group. Use `SESSION_STORE_PROVIDER=postgres` plus `SESSION_STORE_POSTGRES_DSN` for chat history, `INDEXING_QUEUE_PROVIDER=postgres` plus `INDEXING_QUEUE_POSTGRES_DSN` for shared background indexing claims, `INDEXING_JOB_STORE_PROVIDER=postgres` plus `INDEXING_JOB_STORE_POSTGRES_DSN` for indexing job status, `DOCUMENT_CATALOG_PROVIDER=postgres` plus `DOCUMENT_CATALOG_POSTGRES_DSN` for knowledge-space document lifecycle metadata, and `CHECKPOINT_PROVIDER=postgres` plus `CHECKPOINT_POSTGRES_DSN` for LangGraph checkpoint state.

Local development uses SQLite stores by default. Keep the `*_SQLITE_PATH` values under `data/` so chat history, selected sources, retrieval debug data, indexing queue state, indexing jobs, document metadata, and checkpoints survive FastAPI restarts.

The default loader registry supports TXT, Markdown, CSV, HTML/HTM, and JSON uploads. Keep `UPLOAD_ALLOWED_EXTENSIONS` aligned with registered loaders; do not enable a new extension until a loader and security review exist for that file type.

For multi-instance audit inspection, set `RETRIEVAL_AUDIT_STORE_PROVIDER=postgres` plus `RETRIEVAL_AUDIT_POSTGRES_DSN` so every API instance writes retrieval audits to the same database.

Before deployment, set `DEPLOYMENT_ENVIRONMENT=production`. The startup validator then rejects fake providers, process-memory stores, localhost CORS origins, and debug mode.

For larger uploads, set `INDEXING_EXECUTION_MODE=background` so upload responses return a pending job while workers perform indexing. Keep `INDEXING_QUEUE_PROVIDER=sqlite` for local single-process deployments that should survive FastAPI restarts. Use `INDEXING_QUEUE_PROVIDER=memory` only for throwaway tests. Use `INDEXING_QUEUE_PROVIDER=postgres` plus Postgres job/catalog stores when multiple processes must share background indexing work. Keep `INDEXING_WORKER_RECOVER_PENDING_JOBS=true` so persisted pending jobs are re-enqueued on startup. Keep `sync` for simple local deployments where callers should receive immediate indexing success or failure.

For retrieval-quality profiles, set `RETRIEVAL_PROFILE=high_recall` when the business needs a wider recall path with relaxed non-isolation metadata filters. Keep `RETRIEVAL_RELAXED_FILTER_PRESERVE_KEYS` aligned with tenant and knowledge-space boundaries. Set `QUERY_REWRITER_PROVIDER=llm` to rewrite user questions into concise retrieval queries, `RERANKER_PROVIDER=llm` to rerank retrieved chunks before truncation, and `CONTEXT_COMPRESSOR_PROVIDER=llm` to compress retrieved chunks before answer generation. LLM enhancers reuse `ProviderContainer.chat_model_provider` and the shared `CHAT_MODEL`; RAG has no dedicated model variable. Tune these switches with evaluation data instead of enabling extra model calls blindly.

## Tools

Register business tools through `ToolRegistry`, then add their names to `ENABLED_TOOLS`. Tool names should be stable, action-oriented, and specific, for example `crm_lookup` or `policy_lookup`.

There is no pre-retrieval planner. `decide_retrieval` only routes to tools for an explicit `tool_request`. During answer generation the model may still emit `tool_calls`; the graph continues `answer ↔ tool` until a final answer is produced. Document retrieval stays on the graph retrieve node rather than a model-planned `retrieve_knowledge` pre-step.

## Prompts

Add a new `PromptProfile` for domain tone and grounding rules. Keep source citation, refusal, and escalation policies explicit in the profile.

## Evaluation

Start from `data/evaluation/business.example.jsonl` and the matching sample
corpus in `docs/business-samples/`. Replace the sample HR, finance, and support
examples with business-specific questions, and keep each record scoped with
`metadata.spaceId`. Grounded examples need expected answer traits and expected
sources; refusal examples should leave both arrays empty.

Run the readiness gate before spending model calls:

```powershell
.\scripts\run-evaluation.ps1 -Dataset data\evaluation\business.example.jsonl -Mode service -FaithfulnessJudge model -PreflightOnly -MinExamples 6
```

Then run the evaluation and keep the report artifact:

```powershell
.\scripts\run-evaluation.ps1 -Dataset data\evaluation\business.example.jsonl -Mode service -FaithfulnessJudge model -MinExamples 6 -ReportPath artifacts\evaluation-report.json
```

## Rule

Business-specific work should use configuration, provider selection, indexing execution mode, queue provider selection, retrieval profiles, retrieval enhancer switches, tool registration, prompt profiles, and evaluation data. As a default rule, do not modify core code unless a reusable extension point is missing.
