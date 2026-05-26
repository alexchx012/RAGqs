import json
from pathlib import Path

import pytest

import app.evaluation.runner as evaluation_runner
from app.evaluation import (
    AgentRunResult,
    FaithfulnessVerdict,
    GoldenExample,
    HttpRagEvaluationClient,
    ModelFaithfulnessJudge,
    StaticFaithfulnessJudge,
    evaluate_results,
    load_golden_dataset,
    run_fake_evaluation,
    run_http_evaluation,
    run_service_evaluation,
)
from app.evaluation.runner import build_arg_parser, main


def test_load_golden_dataset_validates_jsonl_records(tmp_path: Path):
    dataset_path = tmp_path / "golden.jsonl"
    dataset_path.write_text(
        "\n".join(
            [
                '{"id":"rag-basics","question":"What is RAG?","expectedAnswerTraits":["retrieval","generation"],"expectedSources":["rag.md"],"expectsRefusal":false}',
                '{"id":"unknown","question":"Who owns the moon?","expectedAnswerTraits":[],"expectedSources":[],"expectsRefusal":true}',
            ]
        ),
        encoding="utf-8",
    )

    examples = load_golden_dataset(dataset_path)

    assert [example.id for example in examples] == ["rag-basics", "unknown"]
    assert examples[0].expected_answer_traits == ["retrieval", "generation"]
    assert examples[0].expected_sources == ["rag.md"]
    assert examples[1].expects_refusal is True


def test_evaluate_results_computes_core_rag_metrics():
    examples = [
        GoldenExample(
            id="rag-basics",
            question="What is RAG?",
            expected_answer_traits=["retrieval", "generation"],
            expected_sources=["rag.md"],
        ),
        GoldenExample(id="unknown", question="Unknown?", expects_refusal=True),
    ]
    results = [
        AgentRunResult(
            example_id="rag-basics",
            answer="RAG combines retrieval with generation.",
            retrieved_sources=[{"fileName": "rag.md"}],
            cited_sources=[{"fileName": "rag.md"}],
            latency_ms=10.0,
            faithfulness=FaithfulnessVerdict(score=1.0, passed=True, rationale="grounded"),
        ),
        AgentRunResult(
            example_id="unknown",
            answer="知识库中没有相关信息，无法回答。",
            latency_ms=20.0,
        ),
    ]

    report = evaluate_results(examples, results)

    assert report.metrics.total_examples == 2
    assert report.metrics.retrieval_hit_rate == 1.0
    assert report.metrics.answer_trait_coverage == 1.0
    assert report.metrics.answer_faithfulness == 1.0
    assert report.metrics.citation_accuracy == 1.0
    assert report.metrics.no_answer_refusal_rate == 1.0
    assert report.metrics.average_latency_ms == 15.0
    assert report.failures == []


def test_evaluate_results_reports_missing_sources_and_traits():
    examples = [
        GoldenExample(
            id="grounded",
            question="Explain grounded answers.",
            expected_answer_traits=["grounded"],
            expected_sources=["grounding.md"],
        )
    ]
    results = [
        AgentRunResult(
            example_id="grounded",
            answer="This answer has no expected trait.",
            retrieved_sources=[],
            cited_sources=[],
            faithfulness=FaithfulnessVerdict(score=0.25, passed=False, rationale="unsupported"),
        )
    ]

    report = evaluate_results(examples, results)

    assert report.metrics.retrieval_hit_rate == 0.0
    assert report.metrics.answer_trait_coverage == 0.0
    assert report.metrics.answer_faithfulness == 0.25
    assert report.metrics.citation_accuracy == 0.0
    assert report.failures == [
        {
            "exampleId": "grounded",
            "issues": [
                "retrieval_miss",
                "answer_trait_miss",
                "faithfulness_miss",
                "citation_miss",
            ],
        }
    ]


def test_fake_evaluation_runner_uses_expected_traits_and_sources():
    examples = [
        GoldenExample(
            id="rag-basics",
            question="What is RAG?",
            expected_answer_traits=["retrieval", "generation"],
            expected_sources=["rag.md"],
        )
    ]

    report = run_fake_evaluation(examples)

    assert report.metrics.retrieval_hit_rate == 1.0
    assert report.metrics.answer_trait_coverage == 1.0
    assert report.metrics.answer_faithfulness == 1.0
    assert report.metrics.citation_accuracy == 1.0


def test_evaluation_cli_accepts_service_mode_and_judge_boundary():
    args = build_arg_parser().parse_args(
        ["--mode", "service", "--faithfulness-judge", "model"]
    )

    assert args.mode == "service"
    assert args.faithfulness_judge == "model"


def test_evaluation_cli_accepts_http_mode_and_base_url():
    args = build_arg_parser().parse_args(
        ["--mode", "http", "--base-url", "http://127.0.0.1:8000"]
    )

    assert args.mode == "http"
    assert args.base_url == "http://127.0.0.1:8000"


def test_model_judge_uses_configured_chat_provider_container(monkeypatch):
    class FakeProvider:
        pass

    provider = FakeProvider()

    class FakeContainer:
        chat_model_provider = provider

    monkeypatch.setattr(
        evaluation_runner,
        "get_default_provider_container",
        lambda: FakeContainer(),
        raising=False,
    )

    judge = evaluation_runner._build_faithfulness_judge("model")

    assert isinstance(judge, ModelFaithfulnessJudge)
    assert judge.chat_model_provider is provider


def test_evaluation_cli_fake_mode_outputs_faithfulness_metric(tmp_path: Path, capsys):
    dataset_path = tmp_path / "golden.jsonl"
    dataset_path.write_text(
        '{"id":"rag-basics","question":"What is RAG?","expectedAnswerTraits":["retrieval"],"expectedSources":["rag.md"],"expectsRefusal":false}',
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--dataset",
            str(dataset_path),
            "--mode",
            "fake",
            "--faithfulness-judge",
            "static",
            "--output-json",
            "--min-faithfulness",
            "1.0",
        ]
    )

    assert exit_code == 0
    assert '"answerFaithfulness": 1.0' in capsys.readouterr().out


def test_evaluation_cli_writes_json_report_artifact(tmp_path: Path):
    dataset_path = tmp_path / "golden.jsonl"
    report_path = tmp_path / "artifacts" / "evaluation-report.json"
    dataset_path.write_text(
        '{"id":"rag-basics","question":"What is RAG?","expectedAnswerTraits":["retrieval"],"expectedSources":["rag.md"],"expectsRefusal":false}',
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--dataset",
            str(dataset_path),
            "--mode",
            "fake",
            "--faithfulness-judge",
            "static",
            "--report-path",
            str(report_path),
        ]
    )

    assert exit_code == 0
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["metrics"]["answerFaithfulness"] == 1.0
    assert payload["failures"] == []


def test_model_faithfulness_judge_invokes_chat_model_and_parses_json_verdict():
    class FakeResponse:
        content = '{"score": 0.82, "passed": true, "rationale": "answer is grounded"}'

    class FakeJudgeModel:
        def __init__(self):
            self.messages = []

        def invoke(self, messages):
            self.messages.append(messages)
            return FakeResponse()

    class FakeJudgeProvider:
        def __init__(self):
            self.model = FakeJudgeModel()
            self.streaming_values = []

        def create_chat_model(self, streaming: bool = True):
            self.streaming_values.append(streaming)
            return self.model

    provider = FakeJudgeProvider()
    judge = ModelFaithfulnessJudge(provider)
    example = GoldenExample(id="rag", question="What is RAG?", expected_sources=["rag.md"])
    result = AgentRunResult(
        example_id="rag",
        answer="RAG combines retrieval and generation.",
        cited_sources=[{"fileName": "rag.md"}],
    )

    verdict = judge.judge(example, result)

    assert provider.streaming_values == [False]
    assert verdict == FaithfulnessVerdict(
        score=0.82,
        passed=True,
        rationale="answer is grounded",
    )
    human_prompt = provider.model.messages[0][1].content
    assert "What is RAG?" in human_prompt
    assert "RAG combines retrieval and generation." in human_prompt
    assert "rag.md" in human_prompt


def test_model_faithfulness_judge_returns_failed_verdict_for_invalid_json():
    class InvalidModel:
        def invoke(self, messages):
            return "not-json"

    class InvalidProvider:
        def create_chat_model(self, streaming: bool = True):
            return InvalidModel()

    judge = ModelFaithfulnessJudge(InvalidProvider())
    verdict = judge.judge(
        GoldenExample(id="bad", question="question"),
        AgentRunResult(example_id="bad", answer="answer"),
    )

    assert verdict.score == 0.0
    assert verdict.passed is False
    assert "invalid faithfulness judge response" in verdict.rationale


def test_http_evaluation_client_posts_chat_and_normalizes_response():
    calls = []

    def post_json(url, payload, timeout):
        calls.append((url, payload, timeout))
        return {
            "code": 200,
            "data": {
                "success": True,
                "answer": "RAG combines retrieval and generation.",
                "sources": [{"fileName": "rag.md", "documentId": "doc-rag"}],
                "retrieval": {
                    "query": payload["Question"],
                    "sources": [{"fileName": "rag.md", "documentId": "doc-rag"}],
                    "debug": {"stages": ["retrieve"]},
                },
            },
        }

    client = HttpRagEvaluationClient(
        "http://127.0.0.1:8000/",
        post_json=post_json,
        timeout_seconds=3.5,
    )

    trace = client.query_with_trace("What is RAG?", session_id="eval-rag")

    assert calls == [
        (
            "http://127.0.0.1:8000/chat",
            {"Id": "eval-rag", "Question": "What is RAG?"},
            3.5,
        )
    ]
    assert trace["answer"] == "RAG combines retrieval and generation."
    assert trace["sources"][0]["fileName"] == "rag.md"
    assert trace["retrieval"]["debug"]["stages"] == ["retrieve"]


def test_http_evaluation_runner_uses_chat_api_trace_response():
    ticks = iter([10.0, 10.2])

    def post_json(url, payload, timeout):
        return {
            "data": {
                "success": True,
                "answer": "retrieval generation",
                "sources": [{"fileName": "rag.md"}],
                "retrieval": {"sources": [{"fileName": "rag.md"}]},
            }
        }

    examples = [
        GoldenExample(
            id="rag-basics",
            question="What is RAG?",
            expected_answer_traits=["retrieval", "generation"],
            expected_sources=["rag.md"],
        )
    ]
    client = HttpRagEvaluationClient("http://localhost:8000", post_json=post_json)

    report = run_http_evaluation(
        examples,
        client,
        clock=lambda: next(ticks),
        faithfulness_judge=StaticFaithfulnessJudge(score=1.0),
    )

    assert report.metrics.retrieval_hit_rate == 1.0
    assert report.metrics.answer_trait_coverage == 1.0
    assert report.metrics.answer_faithfulness == 1.0
    assert report.metrics.average_latency_ms == 200.0


def test_http_evaluation_runner_passes_example_space_id_to_chat_api():
    calls = []

    def post_json(url, payload, timeout):
        calls.append(payload)
        return {
            "data": {
                "success": True,
                "answer": "retrieval",
                "sources": [{"fileName": "finance.md"}],
                "retrieval": {"sources": [{"fileName": "finance.md"}]},
            }
        }

    examples = [
        GoldenExample(
            id="finance",
            question="What is the refund policy?",
            expected_answer_traits=["retrieval"],
            expected_sources=["finance.md"],
            metadata={"spaceId": "finance"},
        )
    ]
    client = HttpRagEvaluationClient("http://localhost:8000", post_json=post_json)

    run_http_evaluation(examples, client)

    assert calls[0]["spaceId"] == "finance"


@pytest.mark.asyncio
async def test_service_evaluation_runner_maps_traced_service_results():
    class FakeService:
        def __init__(self):
            self.calls = []

        async def query_with_trace(self, question: str, session_id: str):
            self.calls.append((question, session_id))
            return {
                "answer": "RAG combines retrieval and generation.",
                "sources": [{"fileName": "rag.md", "documentId": "doc-rag"}],
                "retrieval": {
                    "query": question,
                    "sources": [{"fileName": "rag.md", "documentId": "doc-rag"}],
                    "debug": {"stages": ["retrieve"]},
                },
            }

    ticks = iter([100.0, 100.125])
    examples = [
        GoldenExample(
            id="rag-basics",
            question="What is RAG?",
            expected_answer_traits=["retrieval", "generation"],
            expected_sources=["rag.md"],
        )
    ]

    report = await run_service_evaluation(
        examples,
        FakeService(),
        session_id_prefix="eval",
        clock=lambda: next(ticks),
        faithfulness_judge=StaticFaithfulnessJudge(score=1.0, rationale="grounded"),
    )

    assert report.metrics.retrieval_hit_rate == 1.0
    assert report.metrics.answer_faithfulness == 1.0
    assert report.metrics.average_latency_ms == 125.0
    assert report.failures == []


@pytest.mark.asyncio
async def test_service_evaluation_runner_passes_example_space_id_when_supported():
    class SpaceAwareService:
        def __init__(self):
            self.calls = []

        async def query_with_trace(self, question: str, session_id: str, space_id: str):
            self.calls.append((question, session_id, space_id))
            return {
                "answer": "retrieval",
                "sources": [{"fileName": "hr.md"}],
                "retrieval": {"sources": [{"fileName": "hr.md"}]},
            }

    examples = [
        GoldenExample(
            id="hr",
            question="What is PTO?",
            expected_answer_traits=["retrieval"],
            expected_sources=["hr.md"],
            metadata={"spaceId": "hr"},
        )
    ]
    service = SpaceAwareService()

    report = await run_service_evaluation(examples, service)

    assert service.calls == [("What is PTO?", "eval-hr", "hr")]
    assert report.metrics.retrieval_hit_rate == 1.0
