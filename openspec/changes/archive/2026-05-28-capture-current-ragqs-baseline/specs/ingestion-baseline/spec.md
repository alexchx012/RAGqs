## ADDED Requirements

### Requirement: Secure Upload Ingestion
The system SHALL validate uploads before writing files or indexing documents.

#### Scenario: Upload policy accepts configured text document types
- **WHEN** a file is uploaded through `/api/upload`
- **THEN** the system SHALL allow only configured extensions currently covering TXT,
  Markdown, CSV, HTML/HTM, and JSON, enforce the configured byte limit, require valid
  UTF-8 text content, normalize the filename, and resolve the target path inside the upload
  directory

#### Scenario: High-risk prompt injection text is rejected
- **WHEN** upload scanning is enabled and the uploaded text contains configured high-risk
  instructions such as ignoring previous instructions or revealing the system prompt
- **THEN** the system SHALL reject the upload before the file is indexed

### Requirement: Document Loading And Metadata Normalization
The system SHALL load supported documents through extension-specific UTF-8 loaders and
normalize document and chunk metadata for indexing.

#### Scenario: Supported loader registry is used
- **WHEN** a supported file is indexed
- **THEN** the system SHALL use the loader registry to read TXT, Markdown, CSV, HTML/HTM,
  or JSON content into LangChain documents

#### Scenario: Stable document and chunk metadata are created
- **WHEN** a document is indexed into a knowledge space
- **THEN** the system SHALL assign stable document id, content hash, source path,
  extension, file name, space id, chunk index, chunk id, heading path, and legacy source
  metadata fields to indexed chunks

### Requirement: Indexing Job Lifecycle
The system SHALL track document indexing as persisted jobs with explicit statuses.

#### Scenario: Synchronous indexing runs immediately
- **WHEN** `INDEXING_EXECUTION_MODE=sync` and a document is uploaded
- **THEN** the system SHALL create a pending job, run indexing before returning the upload
  response, update chunk counts, persist success or failure, and include indexing status in
  the response

#### Scenario: Background indexing queues pending jobs
- **WHEN** `INDEXING_EXECUTION_MODE=background`
- **THEN** the system SHALL create a pending job, enqueue its id on the configured memory,
  SQLite, or Postgres queue, return the pending job in the ingestion result, and allow the
  FastAPI-managed in-process worker to execute queued jobs

### Requirement: Indexing Reliability Operations
The system SHALL support idempotent reindexing, retry, directory indexing, and persisted
background worker recovery.

#### Scenario: Existing chunks are removed before reindex
- **WHEN** an indexing job runs for an existing document
- **THEN** the system SHALL delete previous chunks by stable document id and legacy source
  path before adding replacement chunks

#### Scenario: Pending jobs can be recovered
- **WHEN** background indexing starts with pending-job recovery enabled
- **THEN** the worker SHALL re-enqueue persisted pending jobs from the indexing job store
  and drain queued jobs during graceful shutdown
