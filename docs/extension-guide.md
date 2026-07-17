# Extension Guide

This guide describes the Phase 8 extension points for building a second RAG business without editing core runtime code.

## Tool Registry

Built-in LangChain tools are registered through `app.extensions.tools`. The default registry includes `retrieve_knowledge` and `get_current_time`, and `ENABLED_TOOLS` controls which registered tools are passed to the agent.

Business tools should be defined with LangChain's `@tool` decorator and registered with a clear name and category:

```python
registry = build_default_tool_registry()
registry.register(crm_lookup, category="business")
```

## Tool Continuation (answer ↔ tool)

There is no pre-retrieval planner. `decide_retrieval` routes to `tool` only when state already has an explicit `tool_request.name`; otherwise it retrieves (or handoffs empty questions).

During answer generation the chat model may emit `tool_calls` (via LangChain `bind_tools`). The graph then routes `answer → tool → answer` until the model returns a normal answer or hits the tool-round limit. Public SSE tokens stay content-only; reasoning / tool deltas never leak as client content.

## Provider Switching

Provider ids are configured by environment variables:

- `DEPLOYMENT_ENVIRONMENT`: `local` by default, `staging` for pre-production checks, or `production` to reject unsafe local defaults during startup validation.
- `CHAT_PROVIDER`: optional. Leave blank for key-based auto-selection (`deepseek` preferred only when two valid chat keys are present). Explicit values: `deepseek`, `dashscope`, `openai_compatible`, or `fake`. DashScope chat, OpenAI-compatible chat, and `fake` must be set explicitly.
- `CHAT_MODEL`: required shared chat model name for every chat provider (default sample: `deepseek-v4-pro`). There is no separate RAG-only model variable.
- `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL`: DeepSeek chat credentials when DeepSeek is selected or auto-chosen.
- `EMBEDDING_PROVIDER`: `dashscope`, `openai_compatible`, or `fake`.
- `DASHSCOPE_API_KEY` / `DASHSCOPE_EMBEDDING_MODEL`: DashScope embedding credentials/model. Embedding settings stay independent of `CHAT_MODEL`.
- `VECTOR_STORE_PROVIDER`: `milvus` or `fake`.
- `SESSION_STORE_PROVIDER`: `sqlite` by default for local durable chat history, `memory` for throwaway tests, or `postgres` for multi-instance deployments.
- `SESSION_STORE_SQLITE_PATH`: SQLite database path when `SESSION_STORE_PROVIDER=sqlite`.
- `SESSION_STORE_POSTGRES_DSN`: PostgreSQL connection string when `SESSION_STORE_PROVIDER=postgres`.
- `RETRIEVAL_AUDIT_STORE_PROVIDER`: `sqlite` by default for local durable retrieval audits, `memory` for throwaway tests, or `postgres` for multi-instance audit storage.
- `RETRIEVAL_AUDIT_SQLITE_PATH`: SQLite database path when `RETRIEVAL_AUDIT_STORE_PROVIDER=sqlite`.
- `RETRIEVAL_AUDIT_POSTGRES_DSN`: PostgreSQL connection string when `RETRIEVAL_AUDIT_STORE_PROVIDER=postgres`.
- `INGESTION_PROVIDER`: `vector_index` or `fake`.
- `INDEXING_QUEUE_PROVIDER`: `sqlite` by default for local durable queueing, `memory` for throwaway tests, or `postgres` for shared multi-instance background indexing.
- `INDEXING_QUEUE_SQLITE_PATH`: SQLite database path when `INDEXING_QUEUE_PROVIDER=sqlite`.
- `INDEXING_QUEUE_POSTGRES_DSN`: PostgreSQL connection string when `INDEXING_QUEUE_PROVIDER=postgres`.
- `INDEXING_QUEUE_LEASE_TIMEOUT_SECONDS`: how long a SQLite- or Postgres-claimed indexing job can remain running before another worker may reclaim it.
- `INDEXING_WORKER_RECOVER_PENDING_JOBS`: `true` to re-enqueue persisted pending jobs when the background worker starts.
- `INDEXING_JOB_STORE_PROVIDER`: `sqlite` by default for local durable indexing job status, `memory` for throwaway tests, or `postgres` for multi-instance ingestion status.
- `INDEXING_JOB_STORE_SQLITE_PATH`: SQLite database path when `INDEXING_JOB_STORE_PROVIDER=sqlite`.
- `INDEXING_JOB_STORE_POSTGRES_DSN`: PostgreSQL connection string when `INDEXING_JOB_STORE_PROVIDER=postgres`.
- `DOCUMENT_CATALOG_PROVIDER`: `sqlite` by default for local durable document lifecycle metadata, `memory` for throwaway tests, or `postgres` for multi-instance document lifecycle metadata.
- `DOCUMENT_CATALOG_SQLITE_PATH`: SQLite database path when `DOCUMENT_CATALOG_PROVIDER=sqlite`.
- `DOCUMENT_CATALOG_POSTGRES_DSN`: PostgreSQL connection string when `DOCUMENT_CATALOG_PROVIDER=postgres`.
- `CHECKPOINT_PROVIDER`: `sqlite` by default for local durable LangGraph checkpoints, `memory` for throwaway tests, or `postgres` for multi-instance graph state.
- `CHECKPOINT_SQLITE_PATH`: SQLite database path when `CHECKPOINT_PROVIDER=sqlite`.
- `CHECKPOINT_POSTGRES_DSN`: PostgreSQL connection string when `CHECKPOINT_PROVIDER=postgres`.
- `AGENT_RUNTIME`: `explicit_graph` by default, or `legacy` for the old `create_agent` compatibility path.

Use `fake` providers for local tests and demos that must not call DeepSeek, DashScope, Milvus, or external APIs. Use `openai_compatible` for chat or embedding endpoints that implement OpenAI-compatible APIs, and set `CHAT_MODEL` (not a vendor-specific chat model env var). Install the optional `postgres` dependency group before enabling Postgres-backed session, indexing queue, indexing job, document catalog, or checkpoint storage. For multi-instance ingestion, set the queue, indexing job, and document catalog providers to Postgres so API and worker processes share the same job ownership state.

When extending chat providers, implement `ChatModelProvider` and wire it through
`ProviderContainer.chat_model_provider`. That vendor-agnostic boundary is what answer
generation (including answer↔tool continuation), evaluation judges, and retrieval
enhancers (`llm` rewrite / rerank / compress) consume—do not hard-code
“DashScope chat provider” as the extension point.

Retrieval audits are exposed through `GET /api/chat/audits` and can be filtered by session, knowledge space, trace id, and limit. Enable SQLite audit storage when a business needs local post-answer review of selected chunks and retrieval debug data; use Postgres when multiple API instances need to share the same audit trail.

When `DEPLOYMENT_ENVIRONMENT=production`, the validator rejects fake providers, memory-backed stores, debug mode, and wildcard or localhost CORS origins. Keep production templates on SQLite only for single-instance durable deployments; use Postgres for multi-instance deployments.

## Prompt Profiles

Prompt Profiles live in `app.prompts.profiles`. Set `PROMPT_PROFILE=default`, `strict`, or `concise` to choose the system prompt style without changing `RagAgentService`.

Add a profile by registering a `PromptProfile` with a unique name, description, and full system prompt. Keep profiles specific to answer style and grounding policy; do not mix provider or database configuration into prompts.

## Retrieval Enhancers

The default retriever runs vector search through `RetrievalPipeline`. Keep `RETRIEVAL_PROFILE=default`, `QUERY_REWRITER_PROVIDER=none`, `RERANKER_PROVIDER=none`, and `CONTEXT_COMPRESSOR_PROVIDER=none` for low-latency local development. Set `RETRIEVAL_PROFILE=high_recall` when a business needs wider recall: it increases per-branch top-k and adds a relaxed metadata-filter fallback while preserving space and tenant filter keys.

Use `RETRIEVAL_HIGH_RECALL_TOP_K_MULTIPLIER` to tune widened recall and `RETRIEVAL_RELAXED_FILTER_PRESERVE_KEYS` to define filters that must never be relaxed, for example `space_id,tenant_id`. Set enhancer values to `llm` when the configured chat model should rewrite user questions before retrieval, rerank retrieved chunks before truncation, or compress each retrieved chunk before answer generation. Those LLM enhancers reuse `ProviderContainer.chat_model_provider` and therefore the shared `CHAT_MODEL`; there is no separate RAG model setting. Use `CONTEXT_COMPRESSOR_MAX_CHARACTERS` to cap each compressed chunk, for example `CONTEXT_COMPRESSOR_MAX_CHARACTERS=1200`. Treat these switches as business-profile choices and keep evaluation reports before making them default for a new agent.

## Second-Business Template

Start from `docs/templates/business-rag-template.md` when creating another business agent. The template keeps customization in environment settings, prompt profiles, tool registration, evaluation data, and docs. If the new business needs runtime behavior that cannot fit these extension points, add a new provider or registry entry instead of editing API handlers directly.
