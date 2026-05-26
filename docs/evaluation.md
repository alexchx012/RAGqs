# RAG Evaluation

This repository includes a local evaluation harness for regression checks and staged real-service evaluation. The default command runs without external model or vector database credentials, while service mode can call the in-process RAG service when real dependencies are configured.

## Golden Dataset

Golden examples are stored as UTF-8 JSONL records. The default dataset is `data/evaluation/golden.jsonl`. A stronger business-shaped starter dataset is available at `data/evaluation/business.example.jsonl`.

Required fields:

- `id`: stable example id.
- `question`: user question.
- `expectedAnswerTraits`: phrases or traits that should appear in a good answer.
- `expectedSources`: expected source ids, file names, chunk ids, or document ids.
- `expectsRefusal`: whether the agent should refuse because the knowledge base lacks support.
- `metadata.spaceId` (optional): knowledge-space id to evaluate in `service` or `http` mode.

Example:

```json
{"id":"rag-basics","question":"What is RAG?","expectedAnswerTraits":["retrieval","generation"],"expectedSources":["rag-basics.md"],"expectsRefusal":false}
```

Space-scoped example:

```json
{"id":"hr-pto","question":"What is the PTO policy?","expectedAnswerTraits":["pto"],"expectedSources":["hr-policy.md"],"expectsRefusal":false,"metadata":{"spaceId":"hr"}}
```

For real-provider evaluation, treat `metadata.spaceId` as required. Each grounded
example should define non-empty `expectedAnswerTraits` and `expectedSources`.
Refusal examples should set `expectsRefusal=true` and leave traits and sources
empty. The readiness gate also rejects duplicate example ids.

## Real Provider Readiness

Before running a service or HTTP evaluation against real providers, run the preflight:

```powershell
.\scripts\run-evaluation.ps1 -Dataset data\evaluation\business.example.jsonl -Mode service -FaithfulnessJudge model -PreflightOnly -MinExamples 6
```

For an already-started API:

```powershell
.\scripts\run-evaluation.ps1 -Dataset data\evaluation\business.example.jsonl -Mode http -BaseUrl http://127.0.0.1:9900 -PreflightOnly -MinExamples 6
```

The preflight rejects `fake` mode, weak golden datasets, fake service providers,
missing model-judge credentials, invalid HTTP targets, and incomplete LangSmith
settings when tracing is enabled. A real dataset should include at least one
grounded answer example with `expectedAnswerTraits` and `expectedSources`, plus
one unsupported-question example with `expectsRefusal=true`.

For a business-owned dataset, raise the minimum example count:

```powershell
.\scripts\run-evaluation.ps1 -Dataset data\evaluation\business.example.jsonl -Mode service -FaithfulnessJudge model -PreflightOnly -MinExamples 6
```

The example dataset maps to Markdown sample documents under
`docs/business-samples/`. Index those files into the matching `metadata.spaceId`
knowledge spaces before using `service` or `http` mode as a real quality gate.

## Local Command

Run the deterministic fake-provider evaluation:

```powershell
.\scripts\run-evaluation.ps1
```

Run against the configured in-process RAG service:

```powershell
.\scripts\run-evaluation.ps1 -Mode service
```

Run against an already-started FastAPI server:

```powershell
.\scripts\run-evaluation.ps1 -Mode http -BaseUrl http://127.0.0.1:8000
```

The command calls `python -m app.evaluation.runner` and fails if retrieval hit rate, answer trait coverage, answer faithfulness, citation accuracy, or refusal rate drops below `1.0` for the fake dataset.

Use `-FaithfulnessJudge static` for the deterministic judge boundary. Use `-FaithfulnessJudge model` with `-Mode service` or `-Mode http` to score answers through the configured `CHAT_PROVIDER`; DashScope requires `DASHSCOPE_API_KEY` and `RAG_MODEL`, while OpenAI-compatible providers use `OPENAI_COMPATIBLE_API_KEY`, `OPENAI_COMPATIBLE_MODEL`, and optional `OPENAI_COMPATIBLE_BASE_URL`.

## LangSmith Tracing

LangChain-compatible calls can be traced by setting LangSmith environment variables before running service or HTTP evaluation:

```powershell
$env:LANGSMITH_TRACING = "true"
$env:LANGSMITH_ENDPOINT = "https://api.smith.langchain.com"
$env:LANGSMITH_API_KEY = "<your-langsmith-api-key>"
$env:LANGSMITH_PROJECT = "ragqs-local"
.\scripts\run-evaluation.ps1 -Mode service -FaithfulnessJudge model
```

Keep `LANGSMITH_TRACING=false` for the deterministic baseline path.

RAG runtime calls attach LangSmith metadata and tags from the request context:
`traceId`, `sessionId`, `spaceId`, `agentRuntime`, and `promptProfile`, plus tags such as
`ragqs`, `runtime:explicit_graph`, `space:default`, and `prompt:default`. Use `traceId` to
correlate an evaluation run with `GET /api/chat/audits` records and structured access logs.

## Metrics

- `retrievalHitRate`: at least one expected source appears in retrieved sources.
- `answerTraitCoverage`: expected answer traits are present in the answer text.
- `answerFaithfulness`: average faithfulness judge score for judged answers.
- `citationAccuracy`: expected sources are present in cited sources.
- `noAnswerRefusalRate`: unsupported questions are refused.
- `averageLatencyMs`: average captured latency.

## Current Limits

`-Mode service` calls the in-process `rag_agent_service`, so it requires valid local configuration for DashScope, Milvus, and indexed documents. `-Mode http` posts to `/chat` on a running FastAPI server and maps the response into the same evaluation result model. Use `-ReportPath artifacts\evaluation-report.json` to write a CI-friendly JSON report; product-grade trace analysis remains tied to the broader LangSmith and observability roadmap.
