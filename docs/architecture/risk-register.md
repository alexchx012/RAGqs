# RAGqs Foundation Risk Register

## Configuration

Risk: settings now expose typed groups over the existing environment variables; the provider factory, upload policy, and ingestion storage runtime paths consume those grouped views; production deployment guardrails reject unsafe local defaults; real Milvus and Postgres smoke commands exist; and Milvus host ports are configurable for Windows reserved-port cases, but some remaining service modules still read flat global config fields directly.

Mitigation: keep grouped settings views for app, CORS, uploads, deployment environment, providers, storage, agent, model providers, Milvus, RAG, and chunking. Continue migrating runtime modules to these groups while keeping `.env.example` synchronized with `app/config.py`, run `scripts/run-integration-smoke.ps1 -Json` before local or staged deployments that depend on Milvus, and run `scripts/run-postgres-smoke.ps1 -RequireConfigured -Json` before deployments that depend on Postgres-backed runtime state.

## Retrieval Quality

Risk: default top-k similarity retrieval can still fail on ambiguous questions, long documents, sparse metadata, and queries needing synthesis across sections. LLM-backed query rewrite, rerank, and context compression are available, and real-provider evaluation has a preflight boundary plus a space-scoped business example dataset, but production quality is not yet calibrated against a business-owned golden dataset.

Mitigation: keep query rewrite, metadata filters, rerank, contextual compression, source attribution, and retrieval debug output behind provider boundaries. Use `RETRIEVAL_PROFILE=high_recall` for wider multi-retriever recall while preserving space and tenant filters, track retrieval hit rate in evaluation tests, run `scripts/run-evaluation.ps1 -Dataset data\evaluation\business.example.jsonl -Mode service -PreflightOnly -MinExamples 6` before real-provider evaluation, enable `QUERY_REWRITER_PROVIDER=llm`, `RERANKER_PROVIDER=llm`, and `CONTEXT_COMPRESSOR_PROVIDER=llm` only for profiles that benefit from the extra model calls, and replace the example dataset with provider-backed business-owned evaluation before production quality claims.

## Session Persistence

Risk: backend sessions, indexing jobs, document catalog metadata, retrieval audits, and LangGraph checkpoints can use memory, SQLite, or Postgres, and a Postgres smoke gate can verify configured DSN connectivity, but schema setup and full runtime write paths still depend on deployed database credentials at runtime.

Mitigation: keep the `SessionStore` boundary, indexing-job store boundary, document-catalog boundary, retrieval-audit boundary, checkpoint boundary, SQLite/Postgres providers, and backend-first frontend history covered by tests; run the Postgres smoke gate before production multi-instance use and add deeper schema/write-path integration checks when deployment credentials are available.

## Indexing Reliability

Risk: uploads can run through synchronous indexing or an in-process background worker; SQLite and Postgres can persist job status and document lifecycle metadata; memory, SQLite, and Postgres queue providers can feed background workers; the worker can recover persisted pending jobs on startup, but full production tuning for high-volume distributed ingestion is not complete yet.

Mitigation: keep ingestion jobs, idempotent document ids, delete/reindex operations, SQLite/Postgres job persistence, SQLite/Postgres document metadata persistence, memory/SQLite/Postgres queue behavior, pending-job recovery, background worker behavior, and explicit API errors covered by tests; use `INDEXING_QUEUE_PROVIDER=sqlite` for durable single-node development and `INDEXING_QUEUE_PROVIDER=postgres` with Postgres job/catalog stores for multi-instance ingestion, then load-test worker concurrency, lease timing, and retry behavior before high-volume production use.

## Observability

Risk: request trace ids, structured access logs, in-process HTTP/RAG metrics, Prometheus-compatible metrics export, retrieval audit storage, health gates, Milvus/Postgres smoke gates, and evaluation artifacts exist, but central metrics collection, central trace collection, retrieval audit write-path validation against a real Postgres instance, and LangGraph node transition analysis are still limited.

Mitigation: keep selected retrieval chunks, sources, answer text, session id, space id, and trace id in memory, SQLite, or Postgres retrieval audit stores; use `GET /api/metrics` for local HTTP and RAG latency buckets, per-space query counts, and provider token usage when available; scrape `GET /api/metrics/prometheus` when an external collector is available; extend per-step timing, optional LangSmith tracing, CI collection, central metrics storage, real database integration checks, and LangGraph event logs beyond the current process-local boundaries.

## Security

Risk: upload filename/path normalization, extension and size limits, UTF-8 validation, prompt-injection screening, configurable CORS, and production-mode rejection of debug/demo/local defaults are in place, but document trust tiers, malware scanning, storage isolation, and production secret handling are still limited.

Mitigation: keep upload security tests in the baseline, keep secrets out of logs, add document trust metadata, add storage isolation and malware scanning for untrusted uploads, keep deployment CORS origins explicit, and document prompt-injection constraints.

## Extensibility

Risk: several legacy compatibility paths still rely on global service objects, which makes full multi-tenant configuration and production lifecycle management harder.

Mitigation: provider factories, selection validation, fake providers, prompt profiles, and the tool registry now cover the first extension layer. Continue moving remaining global service paths behind durable, tenant-aware factories before production multi-tenant use.
