## ADDED Requirements

### Requirement: Grouped Runtime Settings
The system SHALL expose typed grouped configuration views for app, CORS, upload,
deployment, authentication, runtime controls, providers, storage, agent, OpenAI-compatible
provider, DashScope, Milvus, RAG, and chunking settings while preserving the current flat
environment variable names.

#### Scenario: Grouped app settings drive application construction
- **WHEN** the FastAPI app or Uvicorn options are built from settings
- **THEN** the system SHALL prefer grouped app settings for service name, version, host,
  port, and debug/reload behavior while retaining flat-setting fallback compatibility

#### Scenario: Environment defaults describe the local baseline
- **WHEN** a developer starts from `.env.example`
- **THEN** the system SHALL default to DashScope chat and embedding, Milvus vector store,
  SQLite-backed local runtime state, explicit graph runtime, default prompt profile, and
  synchronous ingestion

### Requirement: Startup Configuration Validation
The system SHALL validate startup configuration through a shared Python validation module
used by tests and startup scripts.

#### Scenario: Unsafe local values are rejected
- **WHEN** configuration contains a missing or placeholder DashScope API key, invalid RAG
  top-k, invalid chunk sizes, invalid upload limits, unsafe credentialed wildcard CORS,
  invalid ports, or unsupported provider ids
- **THEN** the validator SHALL report field-specific errors and return a failing status

#### Scenario: Production profile rejects local-only settings
- **WHEN** `DEPLOYMENT_ENVIRONMENT=production`
- **THEN** the validator SHALL reject debug mode, fake providers, explicit process-memory
  runtime stores, disabled authentication, disabled runtime request controls, and wildcard
  or localhost CORS origins

### Requirement: Provider And Extension Selection Settings
The system SHALL validate configurable provider, prompt, tool, retrieval-profile, and
retrieval-enhancer ids before runtime use.

#### Scenario: Unsupported ids fail validation
- **WHEN** settings include unsupported chat, embedding, vector store, session store,
  ingestion, checkpoint, prompt profile, enabled tool, retrieval profile, query rewriter,
  reranker, or context compressor ids
- **THEN** the system SHALL return actionable validation errors identifying the unsupported
  setting

#### Scenario: Store-specific connection settings are required
- **WHEN** a SQLite or Postgres-backed store provider is selected
- **THEN** the system SHALL require the matching SQLite path or Postgres DSN setting for
  sessions, retrieval audit, indexing queue, indexing jobs, document catalog, and
  checkpoints
