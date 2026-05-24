# Business RAG Template

Use this checklist to create a second business RAG agent while keeping the foundation reusable.

## Configuration

Create a business-specific `.env` from `.env.example` and set:

```env
CHAT_PROVIDER=dashscope
EMBEDDING_PROVIDER=dashscope
VECTOR_STORE_PROVIDER=milvus
SESSION_STORE_PROVIDER=memory
SESSION_STORE_SQLITE_PATH=data/sessions.sqlite3
SESSION_STORE_POSTGRES_DSN=
INGESTION_PROVIDER=vector_index
INDEXING_JOB_STORE_PROVIDER=memory
INDEXING_JOB_STORE_SQLITE_PATH=data/indexing-jobs.sqlite3
INDEXING_JOB_STORE_POSTGRES_DSN=
DOCUMENT_CATALOG_PROVIDER=memory
DOCUMENT_CATALOG_SQLITE_PATH=data/document-catalog.sqlite3
DOCUMENT_CATALOG_POSTGRES_DSN=
CHECKPOINT_PROVIDER=memory
CHECKPOINT_SQLITE_PATH=data/checkpoints.sqlite3
AGENT_RUNTIME=explicit_graph
ENABLED_TOOLS=retrieve_knowledge,get_current_time
TOOL_PLANNING_ENABLED=false
TOOL_PLANNING_EXCLUDED_TOOLS=retrieve_knowledge
PROMPT_PROFILE=strict
RAG_TOP_K=3
```

For local test doubles, switch to:

```env
CHAT_PROVIDER=fake
EMBEDDING_PROVIDER=fake
VECTOR_STORE_PROVIDER=fake
INGESTION_PROVIDER=fake
```

For OpenAI-compatible endpoints, set `CHAT_PROVIDER=openai_compatible` or `EMBEDDING_PROVIDER=openai_compatible`, then configure `OPENAI_COMPATIBLE_API_KEY`, `OPENAI_COMPATIBLE_BASE_URL`, `OPENAI_COMPATIBLE_MODEL`, and `OPENAI_COMPATIBLE_EMBEDDING_MODEL`.

For multi-instance persistence, install the optional Postgres dependency group. Use `SESSION_STORE_PROVIDER=postgres` plus `SESSION_STORE_POSTGRES_DSN` for chat history, `INDEXING_JOB_STORE_PROVIDER=postgres` plus `INDEXING_JOB_STORE_POSTGRES_DSN` for indexing job status, and `DOCUMENT_CATALOG_PROVIDER=postgres` plus `DOCUMENT_CATALOG_POSTGRES_DSN` for knowledge-space document lifecycle metadata.

## Tools

Register business tools through `ToolRegistry`, then add their names to `ENABLED_TOOLS`. Tool names should be stable, action-oriented, and specific, for example `crm_lookup` or `policy_lookup`.

Enable `TOOL_PLANNING_ENABLED=true` only when the chat model should choose non-retrieval tools inside the explicit graph. Leave `retrieve_knowledge` in `TOOL_PLANNING_EXCLUDED_TOOLS`; document retrieval is already handled by the graph retrieval node.

## Prompts

Add a new `PromptProfile` for domain tone and grounding rules. Keep source citation, refusal, and escalation policies explicit in the profile.

## Evaluation

Add a business golden dataset under `data/evaluation/`, then run:

```powershell
.\scripts\run-evaluation.ps1 -Dataset data\evaluation\golden.jsonl -ReportPath artifacts\evaluation-report.json
```

## Rule

Business-specific work should use configuration, provider selection, tool registration, prompt profiles, and evaluation data. As a default rule, do not modify core code unless a reusable extension point is missing.
