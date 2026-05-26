import json
from pathlib import Path

import pytest

import app.evaluation.runner as evaluation_runner
from app.config import Settings
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
    validate_business_golden_dataset,
)
from app.evaluation.runner import build_arg_parser, main

ROOT = Path(__file__).resolve().parents[1]


def _real_settings(**overrides):
    values = {
        "dashscope_api_key": "sk-real-dashscope",
        "chat_provider": "dashscope",
        "embedding_provider": "dashscope",
        "vector_store_provider": "milvus",
        "ingestion_provider": "vector_index",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _real_golden_examples():
    return [
        GoldenExample(
            id="grounded",
            question="What is the policy?",
            expected_answer_traits=["policy"],
            expected_sources=["policy.md"],
            metadata={"spaceId": "business"},
        ),
        GoldenExample(
            id="unsupported",
            question="What is not documented?",
            expects_refusal=True,
            metadata={"spaceId": "business"},
        ),
    ]


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


def test_evaluation_cli_accepts_real_preflight_only_mode():
    args = build_arg_parser().parse_args(
        ["--mode", "service", "--preflight-only", "--faithfulness-judge", "model"]
    )

    assert args.preflight_only is True
    assert args.mode == "service"
    assert args.faithfulness_judge == "model"


def test_evaluation_cli_accepts_min_examples_preflight_threshold():
    args = build_arg_parser().parse_args(
        ["--mode", "service", "--preflight-only", "--min-examples", "6"]
    )

    assert args.preflight_only is True
    assert args.min_examples == 6


def test_real_evaluation_readiness_rejects_fake_mode():
    from app.evaluation.readiness import validate_real_evaluation_readiness

    report = validate_real_evaluation_readiness(
        _real_golden_examples(),
        settings=_real_settings(),
        mode="fake",
        faithfulness_judge="static",
    )

    assert report.ready is False
    assert ("EVALUATION_MODE", "use service or http mode for real-provider evaluation") in [
        (issue.field, issue.message) for issue in report.errors
    ]


def test_real_evaluation_readiness_validates_dataset_quality():
    from app.evaluation.readiness import validate_real_evaluation_readiness

    report = validate_real_evaluation_readiness(
        [
            GoldenExample(
                id="weak",
                question="What is documented?",
                expected_answer_traits=[],
                expected_sources=[],
            )
        ],
        settings=_real_settings(),
        mode="service",
        faithfulness_judge="static",
    )

    assert report.ready is False
    assert "DATASET" in {issue.field for issue in report.errors}
    assert "DATASET[weak].expectedAnswerTraits" in {issue.field for issue in report.errors}
    assert "DATASET[weak].expectedSources" in {issue.field for issue in report.errors}


def test_business_golden_dataset_quality_requires_unique_space_scoped_examples():
    report = validate_business_golden_dataset(
        [
            GoldenExample(
                id="duplicate",
                question="What is the PTO policy?",
                expected_answer_traits=["pto"],
                expected_sources=["hr-handbook.md"],
                metadata={"spaceId": "hr"},
            ),
            GoldenExample(
                id="duplicate",
                question="What is the expense policy?",
                expected_answer_traits=["expense"],
                expected_sources=["expense-policy.md"],
            ),
            GoldenExample(
                id="unsupported",
                question="What is the private payroll password?",
                expected_answer_traits=["password"],
                expected_sources=["payroll.md"],
                expects_refusal=True,
                metadata={"spaceId": "hr"},
            ),
        ],
        min_examples=4,
    )

    assert report.ready is False
    assert report.dataset_examples == 3
    assert report.grounded_examples == 2
    assert report.refusal_examples == 1
    errors = {(issue.field, issue.message) for issue in report.errors}
    assert ("DATASET", "include at least 4 golden examples") in errors
    assert ("DATASET[duplicate].id", "example ids must be unique") in errors
    assert (
        "DATASET[duplicate].metadata.spaceId",
        "business evaluation examples must target a knowledge space",
    ) in errors
    assert (
        "DATASET[unsupported].expectedAnswerTraits",
        "refusal examples must not define answer traits",
    ) in errors
    assert (
        "DATASET[unsupported].expectedSources",
        "refusal examples must not define expected sources",
    ) in errors


def test_real_evaluation_readiness_requires_space_scoped_business_examples():
    from app.evaluation.readiness import validate_real_evaluation_readiness

    report = validate_real_evaluation_readiness(
        [
            GoldenExample(
                id="grounded",
                question="What is the policy?",
                expected_answer_traits=["policy"],
                expected_sources=["policy.md"],
            ),
            GoldenExample(
                id="unsupported",
                question="What is not documented?",
                expects_refusal=True,
                metadata={"spaceId": "business"},
            ),
        ],
        settings=_real_settings(),
        mode="service",
        faithfulness_judge="static",
    )

    assert report.ready is False
    assert "DATASET[grounded].metadata.spaceId" in {issue.field for issue in report.errors}


def test_business_example_dataset_is_strong_enough_for_real_evaluation_preflight():
    examples = load_golden_dataset("data/evaluation/business.example.jsonl")

    quality = validate_business_golden_dataset(examples, min_examples=6)
    fake_report = run_fake_evaluation(examples)

    assert quality.ready is True
    assert quality.dataset_examples == 6
    assert quality.grounded_examples >= 4
    assert quality.refusal_examples >= 2
    assert fake_report.metrics.retrieval_hit_rate == 1.0
    assert fake_report.metrics.citation_accuracy == 1.0
    assert fake_report.metrics.no_answer_refusal_rate == 1.0


def test_business_example_dataset_sources_map_to_sample_documents():
    examples = load_golden_dataset("data/evaluation/business.example.jsonl")
    sample_docs_dir = ROOT / "docs" / "business-samples"

    for example in examples:
        if example.expects_refusal:
            continue
        for source in example.expected_sources:
            source_path = sample_docs_dir / source
            assert source_path.exists(), source
            content = source_path.read_text(encoding="utf-8").lower()
            for trait in example.expected_answer_traits:
                assert trait.lower() in content


def test_real_evaluation_readiness_rejects_fake_service_providers():
    from app.evaluation.readiness import validate_real_evaluation_readiness

    report = validate_real_evaluation_readiness(
        _real_golden_examples(),
        settings=_real_settings(
            chat_provider="fake",
            embedding_provider="fake",
            vector_store_provider="fake",
            ingestion_provider="fake",
        ),
        mode="service",
        faithfulness_judge="static",
    )

    assert report.ready is False
    assert {
        "CHAT_PROVIDER",
        "EMBEDDING_PROVIDER",
        "VECTOR_STORE_PROVIDER",
        "INGESTION_PROVIDER",
    }.issubset({issue.field for issue in report.errors})


def test_real_evaluation_readiness_checks_http_base_url_and_model_judge_provider():
    from app.evaluation.readiness import validate_real_evaluation_readiness

    report = validate_real_evaluation_readiness(
        _real_golden_examples(),
        settings=_real_settings(chat_provider="fake"),
        mode="http",
        faithfulness_judge="model",
        base_url="",
    )

    assert report.ready is False
    assert "BASE_URL" in {issue.field for issue in report.errors}
    assert "CHAT_PROVIDER" in {issue.field for issue in report.errors}


def test_real_evaluation_readiness_warns_when_langsmith_is_disabled():
    from app.evaluation.readiness import validate_real_evaluation_readiness

    report = validate_real_evaluation_readiness(
        _real_golden_examples(),
        settings=_real_settings(),
        mode="service",
        faithfulness_judge="static",
        env={"LANGSMITH_TRACING": "false"},
    )

    assert report.ready is True
    assert ("LANGSMITH_TRACING", "enable LangSmith tracing for real-provider diagnostics") in [
        (issue.field, issue.message) for issue in report.warnings
    ]


def test_real_evaluation_cli_preflight_only_outputs_readiness_report(
    tmp_path: Path,
    capsys,
    monkeypatch,
):
    dataset_path = tmp_path / "business-golden.jsonl"
    dataset_path.write_text(
        "\n".join(
            [
                '{"id":"grounded","question":"What is the policy?","expectedAnswerTraits":["policy"],"expectedSources":["policy.md"],"expectsRefusal":false,"metadata":{"spaceId":"business"}}',
                '{"id":"unsupported","question":"What is not documented?","expectedAnswerTraits":[],"expectedSources":[],"expectsRefusal":true,"metadata":{"spaceId":"business"}}',
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(evaluation_runner, "config", _real_settings(), raising=False)
    monkeypatch.setattr(
        evaluation_runner,
        "os_environ",
        {"LANGSMITH_TRACING": "true", "LANGSMITH_API_KEY": "ls-key", "LANGSMITH_PROJECT": "ragqs"},
        raising=False,
    )

    exit_code = main(
        [
            "--dataset",
            str(dataset_path),
            "--mode",
            "service",
            "--preflight-only",
            "--output-json",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"ready": true' in output
    assert '"datasetExamples": 2' in output


def test_real_evaluation_cli_preflight_only_enforces_min_examples(
    tmp_path: Path,
    capsys,
    monkeypatch,
):
    dataset_path = tmp_path / "small-business-golden.jsonl"
    dataset_path.write_text(
        "\n".join(
            [
                '{"id":"grounded","question":"What is the policy?","expectedAnswerTraits":["policy"],"expectedSources":["policy.md"],"expectsRefusal":false,"metadata":{"spaceId":"business"}}',
                '{"id":"unsupported","question":"What is not documented?","expectedAnswerTraits":[],"expectedSources":[],"expectsRefusal":true,"metadata":{"spaceId":"business"}}',
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(evaluation_runner, "config", _real_settings(), raising=False)
    monkeypatch.setattr(
        evaluation_runner,
        "os_environ",
        {"LANGSMITH_TRACING": "true", "LANGSMITH_API_KEY": "ls-key", "LANGSMITH_PROJECT": "ragqs"},
        raising=False,
    )

    exit_code = main(
        [
            "--dataset",
            str(dataset_path),
            "--mode",
            "service",
            "--preflight-only",
            "--min-examples",
            "3",
        ]
    )

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "error DATASET: include at least 3 golden examples" in output


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
