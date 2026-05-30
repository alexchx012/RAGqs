## Why

RAGqs already has implemented behavior across configuration, providers, ingestion,
retrieval, agent execution, sessions, knowledge spaces, evaluation, operations, and
security, but the OpenSpec baseline is empty. Capturing the current behavior as specs
prevents future optimization work from re-inventing requirements or drifting away from
the actual system contract.

## What Changes

- Add baseline OpenSpec specs that describe the capabilities already implemented in the
  current README, docs, tests, and application code.
- Cover configuration, provider selection, ingestion, retrieval, agent graph runtime,
  session persistence, knowledge spaces, evaluation, observability, security, and
  deployment/operations.
- Document current boundaries and known non-claims where the implementation exposes
  smoke tests, fake providers, or preflight checks rather than full production proof.
- No business code changes are included.

## Capabilities

### New Capabilities

- `configuration-baseline`: Runtime configuration groups, environment defaults, provider
  options, and startup validation boundaries.
- `provider-baseline`: LLM, embedding, vector store, session, checkpoint, queue, catalog,
  audit, and fake provider contracts and selection behavior.
- `ingestion-baseline`: Upload validation, document loading, metadata extraction,
  splitting, indexing jobs, background worker, and vector indexing behavior.
- `retrieval-baseline`: Retrieval profiles, enhanced retrieval pipeline, trace metadata,
  and knowledge retrieval tool behavior.
- `agent-graph-baseline`: LangGraph RAG graph state, tool invocation, provider injection,
  streaming, and runtime controls.
- `session-baseline`: Chat request/session models, session history APIs, SQLite/Postgres
  stores, and checkpoint persistence.
- `knowledge-spaces-baseline`: Knowledge space lifecycle, document catalog integration,
  authorization-aware space selection, and upload/chat scoping.
- `evaluation-baseline`: Golden dataset schema, evaluation runner/service, readiness
  checks, metrics, judges, and reporting boundaries.
- `observability-baseline`: Health checks, metrics endpoints, retrieval audit logging,
  request context, and operations preflight/smoke checks.
- `security-baseline`: Authentication providers, authorization checks, upload security,
  CORS, runtime limits, and development defaults.
- `deployment-baseline`: Local startup, Docker-backed Milvus stack, Postgres optional
  data layer, scripts, and documented deployment gates.

### Modified Capabilities

- None.

## Impact

- Affects only OpenSpec artifacts under
  `openspec/changes/capture-current-ragqs-baseline/`.
- No API, dependency, business logic, database, or static UI changes.
- Future OpenSpec changes can use these baseline specs as the current capability
  contract.
