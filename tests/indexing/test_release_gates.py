"""retrieval_release_gate 版本化配置契约测试（design §7.4.1）。

覆盖：不可变版本注册与 supersede、验收 run 引用 gate 版本并保存判定输入与
结论、历史 staged 行沿用内嵌判定路径、不同 gate 版本重复判定被 409 拒绝、
方向/样本数/回退判定语义。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select, update

from app.documents.schema import documents_metadata
from app.indexing import (
    RetrievalProfile,
    RetrievalReleaseService,
    SqlAlchemyIndexingRepository,
    indexing_metadata,
)
from app.indexing.release_gates import RetrievalReleaseGateService
from app.indexing.schema import retrieval_releases_table
from app.platform.errors import PlatformError


def _engine():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    documents_metadata.create_all(engine)
    indexing_metadata.create_all(engine)
    return engine


def _gate_metrics(*, regression: float = 0.0) -> list[dict[str, object]]:
    return [
        {
            "metric": name,
            "direction": "below",
            "absolute_threshold": threshold,
            "allowed_regression": regression,
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
            "allowed_regression": regression,
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


def _register_gate(engine, *, regression: float = 0.0, version: str = "gate_1"):
    return RetrievalReleaseGateService(engine).register(
        version=version,
        hardware_profile={"accelerator": "test"},
        concurrency=1,
        metrics=_gate_metrics(regression=regression),
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


def _metrics() -> dict[str, float]:
    return {
        "p50_ms": 1,
        "p95_ms": 2,
        "p99_ms": 3,
        "error_rate": 0,
        "vram_mb": 1,
        "hit_at_k": 0.9,
        "mrr": 0.9,
        "ndcg": 0.9,
        "refusal": 1.0,
    }


def _stage(engine, releases, *, gate_version_id, profile=RetrievalProfile()):
    SqlAlchemyIndexingRepository(engine).active_generation_id()
    return releases.stage(
        generation_id="generation_initial",
        profile=profile,
        acceptance_suite=_suite(),
        gate_version_id=gate_version_id,
    )


def test_gate_versions_are_immutable_and_supersede_the_open_version() -> None:
    engine = _engine()
    gates = RetrievalReleaseGateService(engine)
    fixed_now = datetime(2026, 8, 26, tzinfo=UTC)
    first = gates.register(
        version="v1",
        hardware_profile={"accelerator": "test"},
        concurrency=1,
        metrics=_gate_metrics(),
        effective_from=fixed_now,
    )
    # 发布后不可修改：服务不提供任何更新入口，读取始终返回冻结值。
    assert gates.get(str(first["id"]))["metrics"] == first["metrics"]
    assert gates.resolve(at=fixed_now + timedelta(seconds=1))["id"] == str(first["id"])
    assert gates.resolve(at=fixed_now - timedelta(seconds=1)) is None

    with pytest.raises(PlatformError) as error:
        gates.register(
            version="v2",
            hardware_profile={"accelerator": "test"},
            concurrency=1,
            metrics=_gate_metrics(),
        )
    assert error.value.code == "retrieval_release_gate_conflict"

    second = gates.register(
        version="v2",
        hardware_profile={"accelerator": "test"},
        concurrency=2,
        metrics=_gate_metrics(),
        supersedes_version_id=str(first["id"]),
        effective_from=fixed_now + timedelta(hours=1),
    )
    closed = gates.get(str(first["id"]))
    assert closed["effective_to_utc"] == fixed_now + timedelta(hours=1)
    assert closed["concurrency"] == 1  # 旧版本字段不被 successor 改写
    assert gates.resolve(at=fixed_now + timedelta(minutes=30))["id"] == str(first["id"])
    assert gates.resolve(at=fixed_now + timedelta(hours=2))["id"] == str(second["id"])
    with pytest.raises(PlatformError) as error:
        gates.register(
            version="v3",
            hardware_profile={"accelerator": "test"},
            concurrency=1,
            metrics=_gate_metrics(),
            supersedes_version_id=str(first["id"]),
        )
    assert error.value.code == "retrieval_release_gate_conflict"
    with pytest.raises(PlatformError) as error:
        gates.register(
            version="v3",
            hardware_profile={"accelerator": "test"},
            concurrency=1,
            metrics=_gate_metrics(),
        )
    assert error.value.code == "retrieval_release_gate_conflict"


def test_register_rejects_direction_and_metric_set_violations() -> None:
    engine = _engine()
    gates = RetrievalReleaseGateService(engine)
    with pytest.raises(PlatformError) as error:
        gates.register(
            version="bad_direction",
            hardware_profile={"accelerator": "test"},
            concurrency=1,
            metrics=[
                *_gate_metrics(),
                # p95_ms 是延迟指标，方向必须 below。
                {
                    "metric": "p95_ms",
                    "direction": "above",
                    "absolute_threshold": 20,
                    "allowed_regression": 0.0,
                    "min_samples": 1,
                    "aggregation": "p95",
                    "severity": "blocking",
                },
            ],
        )
    assert error.value.code == "validation_error"
    incomplete = [entry for entry in _gate_metrics() if entry["metric"] != "refusal"]
    with pytest.raises(PlatformError) as error:
        gates.register(
            version="incomplete",
            hardware_profile={"accelerator": "test"},
            concurrency=1,
            metrics=incomplete,
        )
    assert error.value.code == "validation_error"
    assert error.value.details.get("missing_metrics") == ["refusal"]


def test_new_runs_require_a_gate_version_and_record_the_judgment() -> None:
    engine = _engine()
    releases = RetrievalReleaseService(engine)
    SqlAlchemyIndexingRepository(engine).active_generation_id()
    with pytest.raises(PlatformError) as error:
        releases.stage(
            generation_id="generation_initial",
            profile=RetrievalProfile(),
            acceptance_suite=_suite(),
        )
    assert error.value.code == "validation_error"

    gate = _register_gate(engine)
    staged = _stage(engine, releases, gate_version_id=str(gate["id"]))
    releases.release(
        str(staged["id"]),
        metrics=_metrics(),
        hardware_profile=_suite()["hardware_profile"],
    )
    row = (
        engine.connect()
        .execute(
            select(
                retrieval_releases_table.c.gate_version_id,
                retrieval_releases_table.c.gate_judgment_json,
            ).where(retrieval_releases_table.c.id == str(staged["id"]))
        )
        .mappings()
        .one()
    )
    assert row["gate_version_id"] == str(gate["id"])
    judgment = dict(row["gate_judgment_json"] or {})
    # 判定输入（指标值、样本数、profile）与结论被保存。
    assert judgment["gate_version"] == "gate_1"
    assert judgment["hardware_profile"] == {"accelerator": "test"}
    assert judgment["sample_count"] == 8
    assert judgment["metrics"]["hit_at_k"] == 0.9
    assert judgment["passed"] is True


def test_stage_replay_with_a_different_gate_version_is_rejected() -> None:
    engine = _engine()
    releases = RetrievalReleaseService(engine)
    first = _register_gate(engine)
    staged = _stage(engine, releases, gate_version_id=str(first["id"]))
    # 相同 gate 的幂等重放返回既有记录。
    replay = _stage(engine, releases, gate_version_id=str(first["id"]))
    assert replay["id"] == staged["id"]

    later = datetime.now(UTC) + timedelta(hours=1)
    second = RetrievalReleaseGateService(engine).register(
        version="gate_2",
        hardware_profile={"accelerator": "test"},
        concurrency=1,
        metrics=_gate_metrics(),
        supersedes_version_id=str(first["id"]),
        effective_from=later,
    )
    with pytest.raises(PlatformError) as error:
        _stage(engine, releases, gate_version_id=str(second["id"]))
    assert error.value.code == "idempotency_key_conflict"


def test_historical_staged_rows_keep_the_embedded_judgment_path() -> None:
    engine = _engine()
    releases = RetrievalReleaseService(engine)
    gate = _register_gate(engine)
    staged = _stage(engine, releases, gate_version_id=str(gate["id"]))
    # 模拟迁移前已存在的 staged 行：去掉 gate 引用与内嵌 gate 字段。
    with engine.begin() as connection:
        evidence = dict(
            connection.execute(
                select(retrieval_releases_table.c.acceptance_suite_json).where(
                    retrieval_releases_table.c.id == str(staged["id"])
                )
            ).scalar_one()
        )
        evidence.pop("gate_version_id", None)
        evidence["regression_tolerance"] = 0.1
        connection.execute(
            update(retrieval_releases_table)
            .where(retrieval_releases_table.c.id == str(staged["id"]))
            .values(gate_version_id=None, acceptance_suite_json=evidence)
        )
    releases.release(
        str(staged["id"]),
        metrics=_metrics(),
        hardware_profile=_suite()["hardware_profile"],
    )
    row = (
        engine.connect()
        .execute(
            select(
                retrieval_releases_table.c.state,
                retrieval_releases_table.c.gate_judgment_json,
            ).where(retrieval_releases_table.c.id == str(staged["id"]))
        )
        .mappings()
        .one()
    )
    assert row["state"] == "released"
    # 历史（无 gate 引用）release 不写 gate 判定记录，保持原样。
    assert row["gate_judgment_json"] is None


def test_gate_judgment_enforces_direction_min_samples_and_regression() -> None:
    engine = _engine()
    releases = RetrievalReleaseService(engine)
    gate = _register_gate(engine, regression=0.05)
    staged = _stage(engine, releases, gate_version_id=str(gate["id"]))
    releases.release(
        str(staged["id"]),
        metrics=_metrics(),
        hardware_profile=_suite()["hardware_profile"],
    )

    # 绝对门槛：p50_ms 11 > 10。
    violating = _stage(
        engine, releases, gate_version_id=str(gate["id"]), profile=RetrievalProfile(version="2")
    )
    with pytest.raises(PlatformError) as error:
        releases.release(
            str(violating["id"]),
            metrics={**_metrics(), "p50_ms": 11},
            hardware_profile=_suite()["hardware_profile"],
        )
    assert error.value.code == "release_gate_failed"
    assert error.value.details.get("metric") == "p50_ms"
    assert error.value.details.get("check") == "absolute_threshold"

    # 相对回退：hit_at_k 0.83 清过绝对门槛 0.8 但低于 0.9 * (1 - 0.05) = 0.855。
    regress = _stage(
        engine, releases, gate_version_id=str(gate["id"]), profile=RetrievalProfile(version="3")
    )
    with pytest.raises(PlatformError) as error:
        releases.release(
            str(regress["id"]),
            metrics={**_metrics(), "hit_at_k": 0.83},
            hardware_profile=_suite()["hardware_profile"],
        )
    assert error.value.details.get("check") == "allowed_regression"

    # 样本不足：min_samples 抬到 suite 样本数之上。
    later = datetime.now(UTC) + timedelta(hours=1)
    stricter = RetrievalReleaseGateService(engine).register(
        version="gate_strict",
        hardware_profile={"accelerator": "test"},
        concurrency=1,
        metrics=[
            (
                {**entry, "min_samples": 9}
                if entry["metric"] == "p50_ms"
                else {**entry, "allowed_regression": 0.05}
            )
            for entry in _gate_metrics()
        ],
        supersedes_version_id=str(gate["id"]),
        effective_from=later,
    )
    sparse = _stage(
        engine, releases, gate_version_id=str(stricter["id"]), profile=RetrievalProfile(version="4")
    )
    with pytest.raises(PlatformError) as error:
        releases.release(
            str(sparse["id"]),
            metrics=_metrics(),
            hardware_profile=_suite()["hardware_profile"],
        )
    assert error.value.details.get("check") == "min_samples"
    assert error.value.details.get("sample_count") == 8
