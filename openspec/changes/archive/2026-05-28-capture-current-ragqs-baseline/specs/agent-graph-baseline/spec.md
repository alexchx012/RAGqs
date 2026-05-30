## ADDED Requirements

### Requirement: Explicit RAG State Graph Runtime
The system SHALL default to an explicit LangGraph `StateGraph` runtime for RAG question
answering.

#### Scenario: Graph nodes process a normal question
- **WHEN** a non-empty chat question is submitted with `AGENT_RUNTIME=explicit_graph`
- **THEN** the graph SHALL normalize input, decide retrieval, retrieve scoped context,
  generate an answer from retrieved documents, apply final response handling, and emit
  structured events

#### Scenario: Legacy runtime remains selectable
- **WHEN** `AGENT_RUNTIME=legacy`
- **THEN** the service SHALL use the LangChain `create_agent` compatibility path while
  keeping request-scoped knowledge-space enforcement

### Requirement: Graph Error And Handoff Policy
The graph SHALL produce deterministic terminal output for retrieval misses and runtime
errors.

#### Scenario: No retrieved context causes refusal
- **WHEN** retrieval returns no usable documents
- **THEN** the graph SHALL hand off to a deterministic refusal answer stating that the
  knowledge base lacks enough support and SHALL still emit a final `done` event

#### Scenario: Node failure becomes structured error state
- **WHEN** retrieval, tool execution, or answer generation raises an exception
- **THEN** the graph SHALL record a structured error event, route through `error_policy`,
  mark the final response unsuccessful, and avoid serializing the failure as a successful
  answer

### Requirement: Streaming Graph Events
The system SHALL stream graph execution through stable SSE chunk types.

#### Scenario: Streaming chat maps graph events
- **WHEN** `/api/chat_stream` is called
- **THEN** the system SHALL emit message events for retrieval decision, retrieval, handoff,
  error policy, source, tool call, tool result, token/content, error, and done payloads
  when those events occur

#### Scenario: Token streaming uses graph custom events
- **WHEN** the answer generator supports streaming under the explicit graph
- **THEN** token chunks SHALL be emitted through graph custom stream events and final state
  updates SHALL still provide a terminal done payload

### Requirement: Agent Extension Surface
The system SHALL support configured tool registry entries, prompt profiles, and optional
non-retrieval tool planning.

#### Scenario: Enabled tools are selected from registry
- **WHEN** `ENABLED_TOOLS` names built-in or registered tools
- **THEN** the agent service SHALL pass only those tools to the runtime and reject unknown
  tool names during validation

#### Scenario: Tool planning excludes native retrieval by default
- **WHEN** `TOOL_PLANNING_ENABLED=true` and `TOOL_PLANNING_EXCLUDED_TOOLS` includes
  `retrieve_knowledge`
- **THEN** the model-backed tool planner SHALL consider configured non-retrieval tools
  while native RAG retrieval remains on the graph retrieval path
