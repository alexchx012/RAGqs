# knowledge-spaces-baseline Specification

## Purpose
TBD - created by archiving change capture-current-ragqs-baseline. Update Purpose after archive.
## Requirements
### Requirement: Knowledge Space Catalog
The system SHALL manage knowledge spaces and document lifecycle metadata through a catalog
provider.

#### Scenario: Knowledge spaces can be listed and created
- **WHEN** authorized clients call `/api/knowledge-spaces` or create a space
- **THEN** the system SHALL return or persist space id, name, and description through the
  configured memory, SQLite, or Postgres catalog

#### Scenario: Document records track indexing state
- **WHEN** documents are indexed, deleted, or rebuilt
- **THEN** the catalog SHALL track document id, space id, source path, file name, status,
  latest job id, total chunks, indexed chunks, and errors

### Requirement: Space-Scoped Ingestion And Retrieval
The system SHALL preserve knowledge-space scope across upload, indexing metadata, retrieval
filters, chat requests, and evaluation examples.

#### Scenario: Upload indexes into selected space
- **WHEN** `/api/upload` receives a `space_id`
- **THEN** the system SHALL authorize that space, include the space in document metadata,
  create or update catalog records for that space, and return the indexing space id

#### Scenario: Chat retrieval filters by selected space
- **WHEN** a chat request includes a non-default `spaceId`
- **THEN** the service SHALL pass that space into retrieval filters and enforce it for
  retrieval tool calls inside the request

### Requirement: Document Lifecycle APIs
The system SHALL expose document list, detail, delete, rebuild, and indexing-job routes for
authorized knowledge-space operations.

#### Scenario: Document APIs enforce space access
- **WHEN** a user requests documents under a space
- **THEN** the system SHALL require document permissions and access to that knowledge space
  before returning, deleting, or rebuilding document records

#### Scenario: Indexing job APIs expose job state
- **WHEN** authorized clients list, inspect, or retry indexing jobs
- **THEN** the system SHALL return job id, document id, status, chunk counts, errors, and
  source path where appropriate, filtered by accessible spaces for non-global users

