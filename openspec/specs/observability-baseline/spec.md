# observability-baseline Specification

## Purpose
TBD - created by archiving change capture-current-ragqs-baseline. Update Purpose after archive.
## Requirements
### Requirement: Request Tracing And Access Logs
The system SHALL assign and propagate a trace id for each HTTP request and emit structured
access logs.

#### Scenario: Client trace id is reused
- **WHEN** a request includes `X-Trace-Id`
- **THEN** the system SHALL expose the same trace id in request context and response
  headers

#### Scenario: Access logs are structured
- **WHEN** a request completes or an unhandled exception escapes
- **THEN** the system SHALL emit one `http_request` record with trace id, method, path,
  status code, and latency in milliseconds

### Requirement: Runtime Metrics
The system SHALL expose process-local HTTP and RAG runtime metrics as JSON and Prometheus
text.

#### Scenario: JSON metrics are exposed
- **WHEN** `GET /api/metrics` is requested
- **THEN** the system SHALL return HTTP request totals, status codes, per-route counts,
  latency buckets, RAG query counts, per-space query counts, RAG success/failure counts,
  RAG latency buckets, and token usage when available

#### Scenario: Prometheus metrics are exposed
- **WHEN** `GET /api/metrics/prometheus` is requested
- **THEN** the system SHALL return Prometheus-compatible text using the `ragqs_` metric
  prefix for the same process-local snapshot

### Requirement: Retrieval Audit Store
The system SHALL persist successful traced RAG answers to the configured retrieval audit
store.

#### Scenario: Audit records include traceable retrieval data
- **WHEN** a traced RAG answer completes
- **THEN** the system SHALL store trace id, session id, space id, question, answer,
  serialized sources, retrieval debug payload, and creation timestamp

#### Scenario: Audit records are queryable
- **WHEN** `GET /api/chat/audits` is called
- **THEN** the system SHALL return recent audit records filtered by session id, space id,
  trace id, and limit, while enforcing audit permission and knowledge-space access

### Requirement: Operational Health And Smoke Gates
The system SHALL provide health, integration smoke, Postgres smoke, and running-API
preflight checks with explicit scope.

#### Scenario: Health endpoint gates readiness
- **WHEN** any required dependency health check is unhealthy
- **THEN** `/health` SHALL return HTTP 503 with the dependency status envelope

#### Scenario: Smoke gates are non-destructive
- **WHEN** Milvus integration smoke or Postgres smoke scripts run
- **THEN** they SHALL validate configuration and reachability without creating, deleting,
  starting, stopping, or restarting external databases; Postgres write-path validation
  SHALL use an opt-in temporary-table rollback path

