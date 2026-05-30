## ADDED Requirements

### Requirement: Provider Container Composition
The system SHALL compose runtime dependencies through a provider container without opening
external network connections during container construction.

#### Scenario: Default real providers are selected
- **WHEN** default provider settings are used
- **THEN** the system SHALL select DashScope-compatible chat and embedding providers, a
  Milvus vector store provider, vector-index ingestion, SQLite session storage, SQLite
  retrieval audit storage, SQLite checkpoint storage, and a retrieval pipeline over the
  vector-store retriever

#### Scenario: Local test doubles are selected
- **WHEN** `fake` provider ids are configured for supported boundaries
- **THEN** the system SHALL build deterministic in-memory chat, embedding, vector store,
  ingestion, session, audit, and checkpoint providers suitable for tests and software-path
  checks without DashScope or Milvus

### Requirement: Supported Provider IDs
The system SHALL support the current provider ids for chat, embeddings, vector store,
session storage, retrieval audit storage, ingestion, indexing queue, indexing job storage,
document catalog, and checkpoints.

#### Scenario: OpenAI-compatible providers are configured
- **WHEN** chat or embedding provider ids are `openai_compatible`
- **THEN** the system SHALL require OpenAI-compatible API key and model settings and SHALL
  create providers for OpenAI-style chat and embedding endpoints

#### Scenario: Postgres runtime stores are configured
- **WHEN** session, retrieval audit, indexing queue, indexing job, document catalog, or
  checkpoint providers are set to `postgres`
- **THEN** the system SHALL require the matching DSN and SHALL construct Postgres-backed
  provider objects lazily for multi-instance runtime state

### Requirement: Provider Health Boundaries
The system SHALL report provider-aware health by dependency boundary rather than only
application liveness.

#### Scenario: Health endpoint reports required dependencies
- **WHEN** `/health` is requested
- **THEN** the response SHALL include `app`, `modelProvider`, `embeddingProvider`,
  `vectorStore`, `sessionStore`, `checkpointStore`, `retrievalAuditStore`,
  `indexingQueue`, `indexingJobStore`, and `documentCatalog` dependency statuses

#### Scenario: Fake providers are marked as software-path checks
- **WHEN** fake model, embedding, or vector-store providers are selected
- **THEN** health output SHALL identify the fake provider and SHALL NOT claim real answer,
  embedding, or retrieval quality validation
