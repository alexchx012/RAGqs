from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, Engine, select, update

from app.platform.errors import PlatformError

from .models import RetrievalProfile
from .schema import index_generations_table, retrieval_releases_table

_REQUIRED_METRICS = frozenset({"p50_ms", "p95_ms", "p99_ms", "error_rate", "vram_mb"})
_REQUIRED_SAMPLES = frozenset(
    {
        "phrase_query",
        "proper_noun_query",
        "quoted_exact_query",
        "real_question",
        "acl_filter",
        "sparse_exact_hit",
        "refusal",
    }
)
_REQUIRED_QUALITY_METRICS = frozenset({"hit_at_k", "mrr", "ndcg", "refusal"})


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


class RetrievalReleaseService:
    """Immutable retrieval-profile releases backed by indexing-owned metadata."""

    def __init__(self, engine: Engine, *, now: callable | None = None) -> None:
        self._engine = engine
        self._now = now or (lambda: datetime.now(UTC))

    def stage(
        self,
        *,
        generation_id: str,
        profile: RetrievalProfile,
        acceptance_suite: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        suite = _acceptance_suite(acceptance_suite)
        snapshot = {
            "profile_id": profile.profile_id,
            "version": profile.version,
            "top_k": profile.top_k,
            "candidate_limit": profile.candidate_limit,
            "effort": profile.effort,
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
            evidence = {
                **suite,
                **binding,
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
            release_id = _id()
            connection.execute(
                retrieval_releases_table.insert().values(
                    id=release_id,
                    generation_id=generation_id,
                    profile_id=profile.profile_id,
                    version=profile.version,
                    profile_json=snapshot,
                    acceptance_suite_json=evidence,
                    state="staged",
                    created_at_utc=self._now(),
                )
            )
            return {
                "id": release_id,
                "generation_id": generation_id,
                "profile_json": snapshot,
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
            thresholds = _acceptance_suite(dict(release["acceptance_suite_json"] or {}))[
                "thresholds"
            ]
            quality_thresholds = _acceptance_suite(dict(release["acceptance_suite_json"] or {}))[
                "quality_thresholds"
            ]
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
            if any(float(metrics[name]) > float(thresholds[name]) for name in _REQUIRED_METRICS):
                raise PlatformError(
                    "release_gate_failed", "retrieval metrics exceed acceptance thresholds", {}, 409
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
                .values(state="released", acceptance_suite_json=evidence)
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
            effort=str(snapshot["effort"]),
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
