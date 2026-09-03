from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, Engine, select, update

from app.platform.errors import PlatformError

from .models import RetrievalProfile
from .release_gates import REQUIRED_LATENCY_METRICS as _REQUIRED_METRICS
from .release_gates import REQUIRED_QUALITY_METRICS as _REQUIRED_QUALITY_METRICS
from .release_gates import load_gate_version
from .schema import (
    index_generation_heads_table,
    index_generations_table,
    retrieval_releases_table,
)

_REQUIRED_SAMPLES = frozenset(
    {
        "phrase_query",
        "proper_noun_query",
        "quoted_exact_query",
        "real_question",
        "acl_filter",
        "sparse_exact_hit",
        "refusal",
        "source_conflict",
    }
)
# Quality metrics must improve (higher is better); latency/error/vram metrics
# must not regress beyond the tolerance against the current released baseline.
_HIGHER_IS_BETTER = frozenset(_REQUIRED_QUALITY_METRICS)


def _regression_tolerance(suite: Mapping[str, Any]) -> float:
    value = suite.get("regression_tolerance")
    if value is None:
        return 0.0
    if not isinstance(value, (int, float)) or not 0.0 <= float(value) < 1.0:
        raise PlatformError(
            "validation_error", "retrieval regression tolerance is invalid", {}, 422
        )
    return float(value)


def _id() -> str:
    return f"retrieval_release_{secrets.token_urlsafe(12)}"


def _fingerprint(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _acceptance_suite(value: Mapping[str, Any]) -> dict[str, Any]:
    required = (
        "acl_assertions",
        "hardware_profile",
        "thresholds",
        "samples",
        "quality_thresholds",
    )
    if any(not isinstance(value.get(key), Mapping) or not value[key] for key in required):
        raise PlatformError("validation_error", "retrieval acceptance suite is incomplete", {}, 422)
    thresholds = dict(value["thresholds"])
    if not _REQUIRED_METRICS <= thresholds.keys() or any(
        not isinstance(thresholds[name], (int, float)) for name in _REQUIRED_METRICS
    ):
        raise PlatformError(
            "validation_error", "retrieval acceptance thresholds are incomplete", {}, 422
        )
    samples = dict(value["samples"])
    if not _REQUIRED_SAMPLES <= samples.keys():
        raise PlatformError(
            "validation_error", "retrieval acceptance samples are incomplete", {}, 422
        )
    frozen_samples: dict[str, list[dict[str, str]]] = {}
    for name in _REQUIRED_SAMPLES:
        entries = samples[name]
        if not isinstance(entries, list) or not entries:
            raise PlatformError(
                "validation_error", "retrieval acceptance samples are incomplete", {}, 422
            )
        normalized: list[dict[str, str]] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise PlatformError(
                    "validation_error", "retrieval acceptance sample is invalid", {}, 422
                )
            item = {
                field: str(entry.get(field, "")).strip()
                for field in ("sample_id", "input", "expected")
            }
            if any(not value for value in item.values()):
                raise PlatformError(
                    "validation_error", "retrieval acceptance sample is invalid", {}, 422
                )
            normalized.append(item)
        frozen_samples[name] = normalized
    quality_thresholds = dict(value["quality_thresholds"])
    if not _REQUIRED_QUALITY_METRICS <= quality_thresholds.keys() or any(
        not isinstance(quality_thresholds[name], (int, float)) for name in _REQUIRED_QUALITY_METRICS
    ):
        raise PlatformError(
            "validation_error", "retrieval quality thresholds are incomplete", {}, 422
        )
    return {
        "acl_assertions": dict(value["acl_assertions"]),
        "hardware_profile": dict(value["hardware_profile"]),
        "thresholds": thresholds,
        "samples": frozen_samples,
        "quality_thresholds": quality_thresholds,
        "regression_tolerance": _regression_tolerance(value),
    }


def _generation_binding(connection: Connection, generation_id: str) -> dict[str, Any]:
    manifest = connection.execute(
        select(index_generations_table.c.manifest_json).where(
            index_generations_table.c.id == generation_id
        )
    ).scalar_one_or_none()
    if manifest is None:
        raise PlatformError(
            "validation_error", "retrieval release generation is unavailable", {}, 422
        )
    generation_manifest = dict(manifest or {})
    components = dict(generation_manifest.get("components", {}))
    configuration = dict(generation_manifest.get("indexing_configuration", {}))
    return {
        "component_manifest": components,
        "component_manifest_hash": _fingerprint(components),
        "generation_config": configuration,
        "generation_config_hash": str(
            configuration.get("config_hash") or _fingerprint(configuration)
        ),
    }


def _judge_with_gate(
    metrics: Mapping[str, Any],
    *,
    gate_row: Mapping[str, Any],
    gate_metrics: Sequence[Mapping[str, Any]],
    sample_count: int,
    baseline_metrics: Mapping[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """Judge a release run against one frozen gate version.

    Blocking 违规抛 ``release_gate_failed``；advisory 违规只记入判定输入，
    不阻断发布。返回写入库的判定记录（判定输入与结论）。
    """

    names = {str(entry["metric"]) for entry in gate_metrics}
    if not names <= metrics.keys():
        raise PlatformError("release_gate_failed", "retrieval metrics are incomplete", {}, 409)
    advisory: list[dict[str, Any]] = []

    def _violation(name: str, check: str, severity: Any, **details: Any) -> None:
        if severity == "blocking":
            raise PlatformError(
                "release_gate_failed",
                "retrieval release gate check failed",
                {"metric": name, "check": check, **details},
                409,
            )
        advisory.append({"metric": name, "check": check, **details})

    for entry in gate_metrics:
        name = str(entry["metric"])
        value = float(metrics[name])
        direction = str(entry["direction"])
        if direction == "below":
            if value > float(entry["absolute_threshold"]):
                _violation(name, "absolute_threshold", entry["severity"])
        elif value < float(entry["absolute_threshold"]):
            _violation(name, "absolute_threshold", entry["severity"])
        if sample_count < int(entry["min_samples"]):
            raise PlatformError(
                "release_gate_failed",
                "retrieval samples are below the gate minimum",
                {
                    "metric": name,
                    "check": "min_samples",
                    "min_samples": int(entry["min_samples"]),
                    "sample_count": sample_count,
                },
                409,
            )
        baseline = baseline_metrics.get(name)
        if baseline is None:
            continue
        regression = float(entry["allowed_regression"])
        if direction == "below":
            if value > float(baseline) * (1.0 + regression):
                _violation(
                    name,
                    "allowed_regression",
                    entry["severity"],
                    baseline=float(baseline),
                )
        elif value < float(baseline) * (1.0 - regression):
            _violation(name, "allowed_regression", entry["severity"], baseline=float(baseline))
    return {
        "gate_version_id": str(gate_row["id"]),
        "gate_version": str(gate_row["version"]),
        "hardware_profile": dict(gate_row["hardware_profile_json"] or {}),
        "concurrency": int(gate_row["concurrency"]),
        "sample_count": sample_count,
        "metrics": {name: metrics[name] for name in sorted(names)},
        "advisory_violations": advisory,
        "passed": True,
        "evaluated_at_utc": now.isoformat(),
    }


class RetrievalReleaseService:
    """Immutable retrieval-profile releases backed by indexing-owned metadata."""

    def __init__(self, engine: Engine, *, now: Callable[[], datetime] | None = None) -> None:
        self._engine = engine
        self._now = now or (lambda: datetime.now(UTC))

    def stage(
        self,
        *,
        generation_id: str,
        profile: RetrievalProfile,
        acceptance_suite: Mapping[str, Any],
        gate_version_id: str | None = None,
    ) -> Mapping[str, Any]:
        suite = _acceptance_suite(acceptance_suite)
        snapshot = {
            "profile_id": profile.profile_id,
            "version": profile.version,
            "top_k": profile.top_k,
            "candidate_limit": profile.candidate_limit,
            "dense_weight": profile.dense_weight,
            "sparse_weight": profile.sparse_weight,
            "reranker_release": profile.reranker_release,
            "tokenizer_version": profile.tokenizer_version,
            "score_threshold": profile.score_threshold,
            "retrieval_context_items_per_space": profile.retrieval_context_items_per_space,
            "retrieval_context_tokens_per_space": profile.retrieval_context_tokens_per_space,
            "retrieval_context_tokens_cap": profile.retrieval_context_tokens_cap,
            "expected_library_count": profile.expected_library_count,
            "route_tree": profile.route_tree,
            "route_graph": profile.route_graph,
            "config_snapshot": dict(profile.config_snapshot),
        }
        with self._engine.begin() as connection:
            binding = _generation_binding(connection, generation_id)
            active_generation_id = connection.execute(
                select(index_generation_heads_table.c.active_generation_id).where(
                    index_generation_heads_table.c.id == "instance"
                )
            ).scalar_one_or_none()
            gate: Mapping[str, Any] | None = None
            if gate_version_id is not None:
                gate, _ = load_gate_version(connection, gate_version_id)
            # 验收 run 记录（§7.4.1）显式携带活动与候选两个 index generation，
            # 以及本次 run 引用的全部 reranker release（当前 profile 模型是单个
            # 标识，清单形态保持与多阶段 reranker release 兼容）。
            evidence = {
                **suite,
                **binding,
                "candidate_generation_id": generation_id,
                "active_generation_id": (
                    str(active_generation_id) if active_generation_id is not None else None
                ),
                "reranker_releases": [profile.reranker_release],
                "gate_version_id": gate_version_id,
                "profile_config_hash": _fingerprint(snapshot),
                "results": None,
            }
            existing = (
                connection.execute(
                    select(retrieval_releases_table).where(
                        retrieval_releases_table.c.generation_id == generation_id,
                        retrieval_releases_table.c.profile_id == profile.profile_id,
                        retrieval_releases_table.c.version == profile.version,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                existing_evidence = dict(existing["acceptance_suite_json"] or {})
                if dict(existing["profile_json"] or {}) != snapshot or any(
                    existing_evidence.get(key) != evidence[key]
                    for key in (
                        "acl_assertions",
                        "hardware_profile",
                        "thresholds",
                        "samples",
                        "quality_thresholds",
                        "gate_version_id",
                        "component_manifest",
                        "component_manifest_hash",
                        "generation_config",
                        "generation_config_hash",
                        "profile_config_hash",
                    )
                ):
                    raise PlatformError(
                        "idempotency_key_conflict", "retrieval release conflicts", {}, 409
                    )
                return dict(existing)
            # 新 run 一律引用 gate 版本；只有迁移前的历史 staged 行可以没有 gate
            # 引用并沿用内嵌 suite 判定路径。
            if gate is None:
                raise PlatformError(
                    "validation_error",
                    "retrieval release gate version is required",
                    {},
                    422,
                )
            if dict(gate["hardware_profile_json"] or {}) != dict(suite["hardware_profile"]):
                raise PlatformError(
                    "validation_error",
                    "retrieval acceptance suite hardware profile does not match the gate",
                    {},
                    422,
                )
            release_id = _id()
            connection.execute(
                retrieval_releases_table.insert().values(
                    id=release_id,
                    generation_id=generation_id,
                    profile_id=profile.profile_id,
                    version=profile.version,
                    profile_json=snapshot,
                    acceptance_suite_json=evidence,
                    gate_version_id=gate_version_id,
                    state="staged",
                    created_at_utc=self._now(),
                )
            )
            return {
                "id": release_id,
                "generation_id": generation_id,
                "profile_json": snapshot,
                "gate_version_id": gate_version_id,
                "state": "staged",
            }

    def release(
        self,
        release_id: str,
        *,
        metrics: Mapping[str, Any],
        hardware_profile: Mapping[str, Any],
    ) -> None:
        if not _REQUIRED_METRICS <= metrics.keys() or any(
            not isinstance(metrics[name], (int, float)) for name in _REQUIRED_METRICS
        ):
            raise PlatformError("release_gate_failed", "retrieval metrics are incomplete", {}, 409)
        with self._engine.begin() as connection:
            release = (
                connection.execute(
                    select(retrieval_releases_table).where(
                        retrieval_releases_table.c.id == release_id,
                        retrieval_releases_table.c.state == "staged",
                    )
                )
                .mappings()
                .one_or_none()
            )
            if release is None:
                raise PlatformError(
                    "release_gate_failed", "retrieval release is not staged", {}, 409
                )
            binding = _generation_binding(connection, str(release["generation_id"]))
            frozen = dict(release["acceptance_suite_json"] or {})
            if dict(hardware_profile) != dict(frozen.get("hardware_profile") or {}):
                raise PlatformError(
                    "release_gate_failed",
                    "retrieval hardware profile does not match frozen acceptance evidence",
                    {},
                    409,
                )
            if any(
                frozen.get(key) != binding[key]
                for key in (
                    "component_manifest",
                    "component_manifest_hash",
                    "generation_config",
                    "generation_config_hash",
                )
            ):
                raise PlatformError(
                    "release_gate_failed", "retrieval release generation binding changed", {}, 409
                )
            baseline_row = connection.execute(
                select(retrieval_releases_table.c.acceptance_suite_json)
                .where(
                    retrieval_releases_table.c.generation_id == str(release["generation_id"]),
                    retrieval_releases_table.c.profile_id == release["profile_id"],
                    retrieval_releases_table.c.state == "released",
                    retrieval_releases_table.c.id != release_id,
                )
                .order_by(retrieval_releases_table.c.created_at_utc.desc())
                .limit(1)
            ).scalar_one_or_none()
            baseline_metrics = dict(
                dict(baseline_row or {}).get("results", {}).get("metrics", {}) or {}
            )
            gate_version_id = release["gate_version_id"]
            judgment: dict[str, Any] | None = None
            if gate_version_id is not None:
                gate_row, gate_metrics = load_gate_version(connection, str(gate_version_id))
                judgment = _judge_with_gate(
                    metrics,
                    gate_row=gate_row,
                    gate_metrics=gate_metrics,
                    sample_count=sum(
                        len(entries) for entries in dict(frozen.get("samples") or {}).values()
                    ),
                    baseline_metrics=baseline_metrics,
                    now=self._now(),
                )
            else:
                # 迁移前的历史 staged 行没有 gate 引用，沿用内嵌 suite 判定；
                # 既有 release 记录不受 gate 机制影响。
                suite = _acceptance_suite(frozen)
                thresholds = suite["thresholds"]
                quality_thresholds = suite["quality_thresholds"]
                if any(
                    float(metrics[name]) > float(thresholds[name]) for name in _REQUIRED_METRICS
                ):
                    raise PlatformError(
                        "release_gate_failed",
                        "retrieval metrics exceed acceptance thresholds",
                        {},
                        409,
                    )
                if not _REQUIRED_QUALITY_METRICS <= metrics.keys() or any(
                    float(metrics[name]) < float(quality_thresholds[name])
                    for name in _REQUIRED_QUALITY_METRICS
                ):
                    raise PlatformError(
                        "release_gate_failed",
                        "retrieval quality metrics do not meet acceptance thresholds",
                        {},
                        409,
                    )
                tolerance = _regression_tolerance(frozen)
                if baseline_row is not None:
                    for name in sorted(_REQUIRED_METRICS | _REQUIRED_QUALITY_METRICS):
                        baseline = baseline_metrics.get(name)
                        if baseline is None:
                            continue
                        if name in _HIGHER_IS_BETTER:
                            floor = float(baseline) * (1.0 - tolerance)
                            if float(metrics[name]) < floor:
                                raise PlatformError(
                                    "release_gate_failed",
                                    "retrieval quality metrics regress beyond tolerance",
                                    {"metric": name, "baseline": float(baseline)},
                                    409,
                                )
                        else:
                            ceiling = float(baseline) * (1.0 + tolerance)
                            if float(metrics[name]) > ceiling:
                                raise PlatformError(
                                    "release_gate_failed",
                                    "retrieval latency metrics regress beyond tolerance",
                                    {"metric": name, "baseline": float(baseline)},
                                    409,
                                )
            evidence = frozen
            evidence["results"] = {
                "hardware_profile": dict(hardware_profile),
                "metrics": {
                    name: metrics[name]
                    for name in sorted(_REQUIRED_METRICS | _REQUIRED_QUALITY_METRICS)
                },
                "passed": True,
                "evaluated_at_utc": self._now().isoformat(),
            }
            updated = connection.execute(
                update(retrieval_releases_table)
                .where(
                    retrieval_releases_table.c.id == release_id,
                    retrieval_releases_table.c.state == "staged",
                )
                .values(
                    state="released",
                    acceptance_suite_json=evidence,
                    gate_judgment_json=judgment,
                )
            ).rowcount
            assert updated == 1

    def resolve(self, profile: RetrievalProfile, generation_id: str) -> RetrievalProfile:
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(retrieval_releases_table).where(
                        retrieval_releases_table.c.generation_id == generation_id,
                        retrieval_releases_table.c.profile_id == profile.profile_id,
                        retrieval_releases_table.c.version == profile.version,
                        retrieval_releases_table.c.state == "released",
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise PlatformError(
                "retrieval_release_unavailable", "retrieval release is unavailable", {}, 409
            )
        snapshot = dict(row["profile_json"] or {})
        return RetrievalProfile(
            profile_id=str(snapshot["profile_id"]),
            version=str(snapshot["version"]),
            top_k=int(snapshot["top_k"]),
            candidate_limit=int(snapshot["candidate_limit"]),
            dense_weight=float(snapshot.get("dense_weight", 0.7)),
            sparse_weight=float(snapshot.get("sparse_weight", 0.3)),
            effort=profile.effort,
            reranker_release=str(snapshot["reranker_release"]),
            tokenizer_version=str(snapshot.get("tokenizer_version", "default")),
            score_threshold=snapshot.get("score_threshold"),
            retrieval_context_items_per_space=int(snapshot["retrieval_context_items_per_space"]),
            retrieval_context_tokens_per_space=int(snapshot["retrieval_context_tokens_per_space"]),
            retrieval_context_tokens_cap=int(snapshot["retrieval_context_tokens_cap"]),
            expected_library_count=int(snapshot.get("expected_library_count", 1)),
            route_tree=bool(snapshot.get("route_tree", False)),
            route_graph=bool(snapshot.get("route_graph", False)),
            release_id=str(row["id"]),
            config_snapshot={
                **dict(snapshot.get("config_snapshot", {})),
                "hash": _fingerprint(snapshot),
            },
        )

    def is_released_for_generation(self, generation_id: str, connection: Connection) -> bool:
        try:
            binding = _generation_binding(connection, generation_id)
        except PlatformError:
            return False
        releases = (
            connection.execute(
                select(retrieval_releases_table).where(
                    retrieval_releases_table.c.generation_id == generation_id,
                    retrieval_releases_table.c.state == "released",
                )
            )
            .mappings()
            .all()
        )
        for release in releases:
            evidence = dict(release["acceptance_suite_json"] or {})
            results = dict(evidence.get("results") or {})
            metrics = dict(results.get("metrics") or {})
            try:
                suite = _acceptance_suite(evidence)
                thresholds = suite["thresholds"]
                quality_thresholds = suite["quality_thresholds"]
            except PlatformError:
                continue
            if (
                evidence.get("component_manifest_hash") != binding["component_manifest_hash"]
                or evidence.get("generation_config_hash") != binding["generation_config_hash"]
                or evidence.get("profile_config_hash")
                != _fingerprint(dict(release["profile_json"] or {}))
                or results.get("passed") is not True
                or dict(results.get("hardware_profile") or {})
                != dict(evidence.get("hardware_profile") or {})
                or not _REQUIRED_METRICS | _REQUIRED_QUALITY_METRICS <= metrics.keys()
                or any(float(metrics[name]) > float(thresholds[name]) for name in _REQUIRED_METRICS)
                or any(
                    float(metrics[name]) < float(quality_thresholds[name])
                    for name in _REQUIRED_QUALITY_METRICS
                )
            ):
                continue
            return True
        return False


__all__ = ["RetrievalReleaseService"]
