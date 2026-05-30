# retrieval-baseline Specification

## Purpose
TBD - created by archiving change capture-current-ragqs-baseline. Update Purpose after archive.
## Requirements
### Requirement: Structured Retrieval Pipeline
The system SHALL retrieve knowledge through a structured retrieval pipeline that returns
documents, citation-friendly sources, and debug metadata.

#### Scenario: Default retrieval runs vector search
- **WHEN** the default retrieval profile is selected
- **THEN** the system SHALL query the vector-store retriever with the requested top-k and
  knowledge-space filters, deduplicate results, derive sources, and include stage timing
  debug data

#### Scenario: Retrieval result includes citation fields
- **WHEN** documents are returned by retrieval
- **THEN** each source SHALL include index, source path, file name, heading path, chunk id,
  document id, and score when available

### Requirement: Retrieval Profiles
The system SHALL support configurable retrieval profiles for default and high-recall
retrieval behavior.

#### Scenario: High-recall profile widens retrieval
- **WHEN** `RETRIEVAL_PROFILE=high_recall`
- **THEN** the system SHALL use widened top-k retrieval and a relaxed-filter fallback while
  preserving configured protected filters such as space and tenant keys

#### Scenario: Unsupported retrieval profiles are rejected
- **WHEN** an unknown retrieval profile id is configured
- **THEN** the system SHALL fail validation or provider construction with an actionable
  unsupported-profile error

### Requirement: Optional Retrieval Enhancers
The system SHALL expose optional LLM-backed query rewrite, rerank, and context compression
enhancers through configuration.

#### Scenario: Enhancers are disabled by default
- **WHEN** the default `none` enhancer settings are used
- **THEN** the system SHALL run retrieval without extra query rewrite, rerank, or context
  compression model calls

#### Scenario: LLM enhancers are enabled
- **WHEN** query rewriter, reranker, or context compressor provider settings are `llm`
- **THEN** the retrieval pipeline SHALL invoke the configured chat model provider for the
  corresponding enhancement and include the stage in retrieval debug metadata

### Requirement: Knowledge Retrieval Tool
The system SHALL expose a `retrieve_knowledge` LangChain tool backed by the configured
retriever provider and RAG top-k setting.

#### Scenario: Request-scoped space is enforced
- **WHEN** chat execution enforces a request-selected knowledge space
- **THEN** the retrieval tool SHALL use that active space even if a tool-call argument
  attempts to pass another space id

