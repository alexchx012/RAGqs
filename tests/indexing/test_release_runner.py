"""Retrieval release acceptance runner tests (metrics-file driven, frozen gates).

runner 消费外部指标 JSON、复用 releases.py 的 gate 判定（通过→released+run 记录；
违规→failed 记录且 release 保持 staged）、指标输入接口校验与 CLI 退出码。
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, select

from app.documents.schema import documents_metadata
from app.indexing import indexing_metadata
from app.indexing.release_gates import RetrievalReleaseGateService
from app.indexing.release_runner import (
    REQUIRED_ACCEPTANCE_METRICS,
    main,
    parse_acceptance_metrics,
    run_release_acceptance,
)
from app.indexing.releases import RetrievalReleaseService
from app.indexing.schema import retrieval_releases_table
from app.platform.config import load_platform_settings
from app.platform.errors import PlatformError
from app.platform.runtime import PlatformRuntime

_SETTINGS = load_platform_settings(
    {
        "RAG_PLATFORM_PROFILE": "development",
        "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
        "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
        "RAG_PROVIDER_NAME": "fake",
        "RAG_MAINTENANCE_KEY": "ops-key",
    }
)


def _engine():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    documents_metadata.create_all(engine)
    indexing_metadata.create_all(engine)
    return engine


def _runtime(engine) -> PlatformRuntime:
    return PlatformRuntime(_SETTINGS, adapters={"database_engine": engine})


def _gate_metrics() -> list[dict[str, object]]:
    return [
        {
            "metric": name,
            "direction": "below",
            "absolute_threshold": threshold,
            "allowed_regression": 0.0,
            "min_samples": 1,
            "aggregation": aggregation,
            "severity": "blocking",
        }
        for name, threshold, aggregation in (
            ("p50_ms", 10, "p50"),
            ("p95_ms", 20, "p95"),
            ("p99_ms", 30, "p99"),
            ("error_rate", 1, "rate"),
            ("vram_mb", 10, "max"),
        )
    ] + [
        {
            "metric": name,
            "direction": "above",
            "absolute_threshold": threshold,
            "allowed_regression": 0.0,
            "min_samples": 1,
            "aggregation": "mean",
            "severity": "blocking",
        }
        for name, threshold in (
            ("hit_at_k", 0.8),
            ("mrr", 0.8),
            ("ndcg", 0.8),
            ("refusal", 0.9),
        )
    ]


def _register_gate(engine) -> dict[str, object]:
    return dict(
        RetrievalReleaseGateService(engine).register(
            version="gate_runner_1",
            hardware_profile={"accelerator": "test"},
            concurrency=1,
            metrics=_gate_metrics(),
        )
    )


def _suite() -> dict[str, object]:
    return {
        "acl_assertions": {"space_isolation": "passed"},
        "hardware_profile": {"accelerator": "test"},
        "thresholds": {"p50_ms": 10, "p95_ms": 20, "p99_ms": 30, "error_rate": 1, "vram_mb": 10},
        "samples": {
            name: [{"sample_id": f"{name}-1", "input": name, "expected": "pass"}]
            for name in (
                "phrase_query",
                "proper_noun_query",
                "quoted_exact_query",
                "real_question",
                "acl_filter",
                "sparse_exact_hit",
                "refusal",
                "source_conflict",
            )
        },
        "quality_thresholds": {"hit_at_k": 0.8, "mrr": 0.8, "ndcg": 0.8, "refusal": 0.9},
    }


def _stage_release(engine, *, gate_version_id: str) -> dict[str, object]:
    from app.indexing import RetrievalProfile, SqlAlchemyIndexingRepository

    SqlAlchemyIndexingRepository(engine).active_generation_id()
    staged = RetrievalReleaseService(engine).stage(
        generation_id="generation_initial",
        profile=RetrievalProfile(),
        acceptance_suite=_suite(),
        gate_version_id=gate_version_id,
    )
    return dict(staged)


def _metrics_input(**overrides) -> dict[str, object]:
    payload: dict[str, object] = {
        "hardware_profile": {"accelerator": "test"},
        "metrics": {
            "p50_ms": 1,
            "p95_ms": 2,
            "p99_ms": 3,
            "error_rate": 0,
            "vram_mb": 1,
            "hit_at_k": 0.9,
            "mrr": 0.9,
            "ndcg": 0.9,
            "refusal": 1.0,
        },
    }
    payload.update(overrides)
    return payload


def _metrics_file(tmp_path, payload: dict[str, object]):
    path = tmp_path / "metrics.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_runner_replays_frozen_suite_and_releases_on_pass() -> None:
    engine = _engine()
    gate = _register_gate(engine)
    staged = _stage_release(engine, gate_version_id=str(gate["id"]))
    runtime = _runtime(engine)
    try:
        record = run_release_acceptance(
            _SETTINGS,
            runtime=runtime,
            release_id=str(staged["id"]),
            metrics_input=_metrics_input(),
        )
        # Verify before close: runtime.close() disposes the in-memory pool.
        with engine.connect() as connection:
            state = connection.execute(
                select(retrieval_releases_table.c.state).where(
                    retrieval_releases_table.c.id == str(staged["id"])
                )
            ).scalar_one()
    finally:
        runtime.close()
    assert state == "released"
    assert record["passed"] is True
    assert record["state"] == "released"
    assert record["sample_count"] == 8  # frozen suite: one sample per category
    assert record["gate_version_id"] == str(gate["id"])
    assert record["judgment"]["passed"] is True
    assert record["metrics"]["p50_ms"] == 1


def test_runner_reports_gate_failure_and_keeps_the_release_staged() -> None:
    engine = _engine()
    gate = _register_gate(engine)
    staged = _stage_release(engine, gate_version_id=str(gate["id"]))
    failing = _metrics_input()
    failing["metrics"] = {**failing["metrics"], "hit_at_k": 0.1}  # type: ignore[dict-item]
    runtime = _runtime(engine)
    try:
        record = run_release_acceptance(
            _SETTINGS,
            runtime=runtime,
            release_id=str(staged["id"]),
            metrics_input=failing,
        )
        assert record["passed"] is False
        assert record["state"] == "staged"
        assert record["failure"]["code"] == "release_gate_failed"
        assert record["failure"]["details"]["metric"] == "hit_at_k"
        # A corrected run on the same staged release still passes afterwards.
        recovered = run_release_acceptance(
            _SETTINGS,
            runtime=runtime,
            release_id=str(staged["id"]),
            metrics_input=_metrics_input(),
        )
    finally:
        runtime.close()
    assert recovered["passed"] is True
    assert recovered["state"] == "released"


def test_runner_propagates_non_gate_errors() -> None:
    engine = _engine()
    runtime = _runtime(engine)
    try:
        with pytest.raises(PlatformError) as error:
            run_release_acceptance(
                _SETTINGS,
                runtime=runtime,
                release_id="retrieval_release_missing",
                metrics_input=_metrics_input(),
            )
        assert error.value.code == "not_found"

        gate = _register_gate(engine)
        staged = _stage_release(engine, gate_version_id=str(gate["id"]))
        mismatched = _metrics_input(hardware_profile={"accelerator": "other"})
        record = run_release_acceptance(
            _SETTINGS,
            runtime=runtime,
            release_id=str(staged["id"]),
            metrics_input=mismatched,
        )
        assert record["passed"] is False
        assert record["failure"]["code"] == "release_gate_failed"
    finally:
        runtime.close()


def test_metrics_input_interface_is_validated_upfront() -> None:
    assert REQUIRED_ACCEPTANCE_METRICS >= {
        "p50_ms",
        "p95_ms",
        "p99_ms",
        "error_rate",
        "vram_mb",
        "hit_at_k",
        "mrr",
        "ndcg",
        "refusal",
    }
    metrics, hardware = parse_acceptance_metrics(_metrics_input())
    assert metrics["hit_at_k"] == 0.9
    assert hardware == {"accelerator": "test"}

    with pytest.raises(PlatformError) as error:
        parse_acceptance_metrics({"hardware_profile": {}, "metrics": _metrics_input()["metrics"]})
    assert error.value.code == "validation_error"

    incomplete = _metrics_input()
    del incomplete["metrics"]["mrr"]  # type: ignore[union-attr]
    with pytest.raises(PlatformError) as error:
        parse_acceptance_metrics(incomplete)
    assert error.value.details["missing_metrics"] == ["mrr"]

    non_numeric = _metrics_input()
    non_numeric["metrics"] = {**non_numeric["metrics"], "p50_ms": "fast"}  # type: ignore[dict-item]
    with pytest.raises(PlatformError) as error:
        parse_acceptance_metrics(non_numeric)
    assert error.value.details["metric"] == "p50_ms"


def test_main_judges_from_a_metrics_file(capsys, monkeypatch, tmp_path) -> None:
    engine = _engine()
    gate = _register_gate(engine)
    staged = _stage_release(engine, gate_version_id=str(gate["id"]))
    runtime = _runtime(engine)
    monkeypatch.setattr("app.indexing.release_runner.load_platform_settings", lambda: _SETTINGS)
    monkeypatch.setattr("app.indexing.release_runner.build_runtime", lambda _settings: runtime)
    path = _metrics_file(tmp_path, _metrics_input())
    main(["--release-id", str(staged["id"]), "--metrics-file", str(path)])
    record = json.loads(capsys.readouterr().out)
    assert record["passed"] is True
    assert record["state"] == "released"


def test_main_exits_one_on_gate_failure(capsys, monkeypatch, tmp_path) -> None:
    engine = _engine()
    gate = _register_gate(engine)
    staged = _stage_release(engine, gate_version_id=str(gate["id"]))
    runtime = _runtime(engine)
    monkeypatch.setattr("app.indexing.release_runner.load_platform_settings", lambda: _SETTINGS)
    monkeypatch.setattr("app.indexing.release_runner.build_runtime", lambda _settings: runtime)
    failing = _metrics_input()
    failing["metrics"] = {**failing["metrics"], "mrr": 0.1}  # type: ignore[dict-item]
    path = _metrics_file(tmp_path, failing)
    with pytest.raises(SystemExit) as exit_info:
        main(["--release-id", str(staged["id"]), "--metrics-file", str(path)])
    assert exit_info.value.code == 1
    record = json.loads(capsys.readouterr().out)
    assert record["passed"] is False


def test_main_exits_two_without_maintenance_key(monkeypatch, tmp_path) -> None:
    settings = load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
        }
    )
    monkeypatch.setattr("app.indexing.release_runner.load_platform_settings", lambda: settings)
    path = _metrics_file(tmp_path, _metrics_input())
    with pytest.raises(SystemExit) as exit_info:
        main(["--release-id", "whatever", "--metrics-file", str(path)])
    assert exit_info.value.code == 2
