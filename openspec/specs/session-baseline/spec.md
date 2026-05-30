# session-baseline Specification

## Purpose
TBD - created by archiving change capture-current-ragqs-baseline. Update Purpose after archive.
## Requirements
### Requirement: Chat API Session Contract
The system SHALL accept chat requests with session id, question, and optional knowledge-space
id, and SHALL return non-streaming answers through the public response envelope.

#### Scenario: Non-streaming chat returns trace fields
- **WHEN** `/api/chat` succeeds
- **THEN** the response SHALL include `success`, `answer`, `sources`, `retrievalDebug`,
  full `retrieval` metadata, and `errorMessage` inside the envelope data

#### Scenario: Chat request aliases are accepted
- **WHEN** clients send `Id`, `Question`, and `spaceId` fields
- **THEN** the request model SHALL populate session id, question, and space id while
  preserving `default` as the fallback space

### Requirement: Server-Side Session Store
The system SHALL persist chat transcripts through a session store provider independent of
LangGraph checkpoint storage.

#### Scenario: Successful answers record messages
- **WHEN** non-streaming or streaming chat completes
- **THEN** the service SHALL append user and assistant messages to the configured session
  store with knowledge-space metadata and assistant retrieval metadata when available

#### Scenario: Session history prefers the session store
- **WHEN** `/api/chat/session/{session_id}` is requested
- **THEN** the service SHALL read messages from the session store first and use LangGraph
  checkpoint history only as a compatibility fallback

### Requirement: Session Listing And Clearing
The system SHALL expose backend-first session list, search, detail, and clear behavior.

#### Scenario: Session summaries are searchable
- **WHEN** `/api/chat/sessions` is called with or without a query
- **THEN** the system SHALL return sidebar-ready summaries containing id, title, message
  count, updated timestamp, and last message

#### Scenario: Clearing a session clears store and checkpoint when possible
- **WHEN** `/api/chat/clear` is called for an authorized session
- **THEN** the system SHALL clear the session store and attempt to delete the matching
  LangGraph checkpoint thread when supported

### Requirement: Session Store Providers
The system SHALL support memory, SQLite, and Postgres session stores.

#### Scenario: SQLite is the local durable default
- **WHEN** `SESSION_STORE_PROVIDER=sqlite`
- **THEN** chat transcripts and summaries SHALL persist to the configured local SQLite file
  across FastAPI restarts

#### Scenario: Postgres supports multi-instance sessions
- **WHEN** `SESSION_STORE_PROVIDER=postgres`
- **THEN** the system SHALL use the configured Postgres DSN for shared chat history across
  API instances

