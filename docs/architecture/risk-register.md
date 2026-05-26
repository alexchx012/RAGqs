# RAGqs Foundation Risk Register

## Configuration

Risk: settings now expose typed groups over the existing environment variables and production deployment guardrails reject unsafe local defaults, but some service modules still read flat global config fields directly.

Mitigation: keep grouped settings views for app, CORS, uploads, deployment environment, providers, storage, agent, model providers, Milvus, RAG, and chunking. Continue migrating runtime modules to these groups while keeping `.env.example` synchronized with `app/config.py`.

## Retrieval Quality

Risk: default top-k similarity retrieval can still fail on ambiguous questions, long documents, sparse metadata, and queries needing synthesis across sections. LLM-backed query rewrite, rerank, and context compression are available, but they add latency and are not yet calibrated by real-provider evaluation.

Mitigation: keep query rewrite, metadata filters, rerank, contextual compression, source attribution, and retrieval debug output behind provider boundaries. Use `RETRIEVAL_PROFILE=high_recall` for wider multi-retriever recall while preserving space and tenant filters, track retrieval hit rate in evaluation tests, enable `QUERY_REWRITER_PROVIDER=llm`, `RERANKER_PROVIDER=llm`, and `CONTEXT_COMPRESSOR_PROVIDER=llm` only for profiles that benefit from the extra model calls, and add provider-backed evaluation before production quality claims.

## Session Persistence

Risk: backend sessions, indexing jobs, document catalog metadata, and LangGraph checkpoints can use memory, SQLite, or Postgres, but real Postgres integration still depends on deployed database credentials and schema setup at runtime.

Mitigation: keep the `SessionStore` boundary, indexing-job store boundary, document-catalog boundary, checkpoint boundary, SQLite/Postgres providers, and backend-first frontend history covered by tests; add environment-backed integration checks before production multi-instance use.

## Indexing Reliability

Risk: uploads can run through synchronous indexing or an in-process background worker; SQLite and Postgres can persist job status and document lifecycle metadata, but distributed queue execution and cross-process worker coordination are not implemented yet.

Mitigation: keep ingestion jobs, idempotent document ids, delete/reindex operations, SQLite/Postgres job persistence, SQLite/Postgres document metadata persistence, background worker behavior, and explicit API errors covered by tests; add an external queue or dedicated worker service before production multi-instance ingestion.

## Observability

Risk: request trace ids, structured access logs, retrieval audit storage, health gates, and evaluation artifacts exist, but token usage, latency buckets, central trace collection, real Postgres audit integration checks, and LangGraph node transition analysis are still limited.

Mitigation: keep selected retrieval chunks, sources, answer text, session id, space id, and trace id in memory, SQLite, or Postgres retrieval audit stores; extend per-step timing, optional LangSmith tracing, CI collection, real database integration checks, and LangGraph event logs beyond the current request and health boundaries.

## Security

Risk: upload filename/path normalization, extension and size limits, UTF-8 validation, prompt-injection screening, configurable CORS, and production-mode rejection of debug/demo/local defaults are in place, but document trust tiers, malware scanning, storage isolation, and production secret handling are still limited.

Mitigation: keep upload security tests in the baseline, keep secrets out of logs, add document trust metadata, add storage isolation and malware scanning for untrusted uploads, keep deployment CORS origins explicit, and document prompt-injection constraints.

## Extensibility

Risk: several legacy compatibility paths still rely on global service objects, which makes full multi-tenant configuration and production lifecycle management harder.

Mitigation: provider factories, selection validation, fake providers, prompt profiles, and the tool registry now cover the first extension layer. Continue moving remaining global service paths behind durable, tenant-aware factories before production multi-tenant use.
