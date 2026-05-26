"""Local evaluation runner."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from app.config import config
from app.evaluation.datasets import load_golden_dataset
from app.evaluation.fake import run_fake_evaluation
from app.evaluation.http import HttpRagEvaluationClient, run_http_evaluation
from app.evaluation.judges import FaithfulnessJudge, ModelFaithfulnessJudge, StaticFaithfulnessJudge
from app.evaluation.readiness import validate_real_evaluation_readiness
from app.evaluation.service import run_service_evaluation
from app.providers.factory import get_default_provider_container

os_environ = os.environ


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run local RAG evaluation.")
    parser.add_argument("--dataset", default="data/evaluation/golden.jsonl")
    parser.add_argument("--mode", choices=["fake", "service", "http"], default="fake")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument(
        "--faithfulness-judge",
        choices=["none", "static", "model"],
        default="static",
    )
    parser.add_argument("--output-json", action="store_true")
    parser.add_argument("--report-path", default="")
    parser.add_argument("--min-examples", type=int, default=2)
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Validate real-provider evaluation readiness without running examples.",
    )
    parser.add_argument("--min-retrieval-hit-rate", type=float, default=0.0)
    parser.add_argument("--min-answer-trait-coverage", type=float, default=0.0)
    parser.add_argument("--min-faithfulness", type=float, default=0.0)
    parser.add_argument("--min-citation-accuracy", type=float, default=0.0)
    parser.add_argument("--min-refusal-rate", type=float, default=0.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    examples = load_golden_dataset(args.dataset)
    if args.preflight_only:
        report = validate_real_evaluation_readiness(
            examples,
            settings=config,
            mode=args.mode,
            faithfulness_judge=args.faithfulness_judge,
            base_url=args.base_url,
            env=os_environ,
            min_examples=args.min_examples,
        )
        report_json = report.model_dump_json(by_alias=True, indent=2)
        if args.report_path:
            report_path = Path(args.report_path)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(report_json + "\n", encoding="utf-8")
        if args.output_json:
            print(report_json)
        else:
            _print_readiness_report(report)
        return 0 if report.ready else 1

    if args.mode == "fake":
        report = run_fake_evaluation(examples)
    elif args.mode == "service":
        judge = _build_faithfulness_judge(args.faithfulness_judge)
        from app.services.rag_agent_service import rag_agent_service

        report = asyncio.run(
            run_service_evaluation(
                examples,
                rag_agent_service,
                faithfulness_judge=judge,
            )
        )
    else:
        judge = _build_faithfulness_judge(args.faithfulness_judge)
        report = run_http_evaluation(
            examples,
            HttpRagEvaluationClient(
                args.base_url,
                timeout_seconds=args.timeout_seconds,
            ),
            faithfulness_judge=judge,
        )

    report_json = report.model_dump_json(by_alias=True, indent=2)
    if args.report_path:
        report_path = Path(args.report_path)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_json + "\n", encoding="utf-8")

    if args.output_json:
        print(report_json)
    else:
        metrics = report.metrics
        print(f"total_examples={metrics.total_examples}")
        print(f"retrieval_hit_rate={metrics.retrieval_hit_rate}")
        print(f"answer_trait_coverage={metrics.answer_trait_coverage}")
        print(f"answer_faithfulness={metrics.answer_faithfulness}")
        print(f"citation_accuracy={metrics.citation_accuracy}")
        print(f"no_answer_refusal_rate={metrics.no_answer_refusal_rate}")
        print(f"average_latency_ms={metrics.average_latency_ms}")

    if (
        report.metrics.retrieval_hit_rate < args.min_retrieval_hit_rate
        or report.metrics.answer_trait_coverage < args.min_answer_trait_coverage
        or report.metrics.answer_faithfulness < args.min_faithfulness
        or report.metrics.citation_accuracy < args.min_citation_accuracy
        or report.metrics.no_answer_refusal_rate < args.min_refusal_rate
    ):
        return 1
    return 0


def _build_faithfulness_judge(name: str) -> FaithfulnessJudge | None:
    if name == "none":
        return None
    if name == "static":
        return StaticFaithfulnessJudge(score=1.0, rationale="static evaluation judge")
    if name == "model":
        return ModelFaithfulnessJudge(
            get_default_provider_container().chat_model_provider
        )
    raise ValueError(f"unsupported faithfulness judge: {name}")


def _print_readiness_report(report) -> None:
    print(f"ready={str(report.ready).lower()}")
    print(f"status={report.status}")
    print(f"quality_conclusion={report.quality_conclusion}")
    print(f"mode={report.mode}")
    print(f"dataset_examples={report.dataset_examples}")
    for limitation in report.limitations:
        print(f"limitation: {limitation}")
    for issue in report.errors:
        print(f"error {issue.field}: {issue.message}")
    for issue in report.warnings:
        print(f"warning {issue.field}: {issue.message}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
