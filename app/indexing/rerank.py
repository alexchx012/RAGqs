from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.platform.errors import PlatformError

from .models import RetrievalHit, RetrievalProfile

DEFAULT_LIBRARY_CANDIDATE_LIMIT = 45
DEFAULT_COARSE_KEEP_PER_LIBRARY = 25


class RerankerModelPort(Protocol):
    """Remote cross-encoder port; never loads or runs a model locally."""

    provider_name: str

    def score(self, query: str, hits: Sequence[RetrievalHit]) -> tuple[float, ...]: ...


@dataclass(frozen=True, slots=True)
class RerankerRelease:
    """Immutable release identity for one reranker deployment."""

    provider: str
    coarse_model: str
    coarse_revision: str
    final_model: str
    final_revision: str
    quantization: str
    tokenizer_version: str
    candidate_limit: int
    coarse_keep_per_library: int
    score_threshold: float | None = None
    model_checksum: str = "unspecified"
    max_input_tokens: int = 8192
    config_version: str = "1"
    hardware_profile: Mapping[str, Any] = field(
        default_factory=lambda: {"accelerator": "unspecified"}
    )

    def __post_init__(self) -> None:
        for value, name in (
            (self.provider, "provider"),
            (self.coarse_model, "coarse_model"),
            (self.coarse_revision, "coarse_revision"),
            (self.final_model, "final_model"),
            (self.final_revision, "final_revision"),
            (self.quantization, "quantization"),
            (self.tokenizer_version, "tokenizer_version"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise PlatformError(
                    "validation_error", f"reranker release {name} is required", {}, 422
                )
        if not 40 <= self.candidate_limit <= 50:
            raise PlatformError(
                "validation_error", "library candidate limit must be 40-50", {}, 422
            )
        if not 20 <= self.coarse_keep_per_library <= 30:
            raise PlatformError(
                "validation_error", "coarse keep per library must be 20-30", {}, 422
            )
        if self.coarse_keep_per_library > self.candidate_limit:
            raise PlatformError("validation_error", "coarse keep exceeds candidate limit", {}, 422)
        if self.score_threshold is not None and not 0.0 <= self.score_threshold <= 1.0:
            raise PlatformError("validation_error", "reranker threshold is invalid", {}, 422)
        if not isinstance(self.model_checksum, str) or not self.model_checksum.strip():
            raise PlatformError("validation_error", "reranker checksum is required", {}, 422)
        if self.max_input_tokens < 1:
            raise PlatformError("validation_error", "reranker max input is invalid", {}, 422)
        if not isinstance(self.config_version, str) or not self.config_version.strip():
            raise PlatformError("validation_error", "reranker config version is required", {}, 422)
        if not isinstance(self.hardware_profile, Mapping) or not self.hardware_profile:
            raise PlatformError(
                "validation_error", "reranker hardware profile is required", {}, 422
            )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "coarse_model": self.coarse_model,
            "coarse_revision": self.coarse_revision,
            "final_model": self.final_model,
            "final_revision": self.final_revision,
            "quantization": self.quantization,
            "tokenizer_version": self.tokenizer_version,
            "candidate_limit": self.candidate_limit,
            "coarse_keep_per_library": self.coarse_keep_per_library,
            "score_threshold": self.score_threshold,
            "model_checksum": self.model_checksum,
            "max_input_tokens": self.max_input_tokens,
            "config_version": self.config_version,
            "hardware_profile": dict(self.hardware_profile),
        }


class StubRerankerModel:
    """Deterministic development/CI model: preserves input order, no fake scores."""

    def __init__(self, *, provider_name: str = "none", environment: str = "test") -> None:
        self.provider_name = provider_name
        self._environment = environment

    def score(self, query: str, hits: Sequence[RetrievalHit]) -> tuple[float, ...]:
        del query
        if self._environment not in {"development", "test", "ci"}:
            raise PlatformError(
                "reranker_unavailable",
                "RERANKER_PROVIDER=none is not allowed in production",
                {},
                503,
            )
        return ()


class TwoStageReranker:
    """0.6B per-library coarse filter followed by one cross-library 8B final pass.

    Only final (8B) scores become ``rerank_score`` and only they can drive the
    profile threshold. When either stage fails, candidates keep their entering
    (already per-library truncated) order and no threshold is applied.
    """

    def __init__(
        self,
        *,
        release: RerankerRelease,
        coarse_model: RerankerModelPort,
        final_model: RerankerModelPort,
    ) -> None:
        self._release = release
        self._coarse = coarse_model
        self._final = final_model

    @property
    def release(self) -> RerankerRelease:
        return self._release

    def rerank(
        self,
        query: str,
        hits: Sequence[RetrievalHit],
        profile: RetrievalProfile,
    ) -> tuple[Sequence[RetrievalHit], Mapping[str, Any] | None]:
        if not hits:
            return tuple(hits), None
        by_library = self._group_by_library(hits)
        truncated: list[RetrievalHit] = []
        for library in sorted(by_library):
            # Provider order is not a cross-library ranking contract. Establish
            # each library's raw-score order before applying its independent cap.
            ordered = sorted(by_library[library], key=lambda hit: (-hit.score, hit.chunk.chunk_id))
            truncated.extend(ordered[: self._release.candidate_limit])
        try:
            coarse_scores = self._coarse.score(query, truncated)
        except PlatformError as error:
            return tuple(truncated), self._degradation("coarse_unavailable", error)
        except Exception as error:  # provider transports raise arbitrary errors
            return tuple(truncated), self._degradation("coarse_unavailable", error)
        if coarse_scores:
            if len(coarse_scores) != len(truncated):
                return tuple(truncated), self._degradation(
                    "coarse_invalid_output", "coarse score count mismatch"
                )
            try:
                coarse_scores = tuple(float(score) for score in coarse_scores)
            except (TypeError, ValueError):
                return tuple(truncated), self._degradation(
                    "coarse_invalid_output", "coarse score invalid"
                )
            if any(not math.isfinite(score) for score in coarse_scores):
                return tuple(truncated), self._degradation(
                    "coarse_invalid_output", "coarse score invalid"
                )
            scored = sorted(
                zip(coarse_scores, truncated, strict=True),
                key=lambda pair: (-pair[0], pair[1].chunk.chunk_id),
            )
            kept_by_library: dict[str, list[RetrievalHit]] = {}
            for _score, hit in scored:
                library = hit.source or "unknown"
                bucket = kept_by_library.setdefault(library, [])
                if len(bucket) < self._release.coarse_keep_per_library:
                    bucket.append(hit)
            merged = [
                hit for library in sorted(kept_by_library) for hit in kept_by_library[library]
            ]
        else:
            merged = truncated
        try:
            final_scores = self._final.score(query, merged)
        except PlatformError as error:
            return tuple(truncated), self._degradation("final_unavailable", error)
        except Exception as error:
            return tuple(truncated), self._degradation("final_unavailable", error)
        if not final_scores or len(final_scores) != len(merged):
            return tuple(truncated), self._degradation(
                "final_invalid_output", "final score mismatch"
            )
        try:
            final_scores = tuple(float(score) for score in final_scores)
        except (TypeError, ValueError):
            return tuple(truncated), self._degradation(
                "final_invalid_output", "final score invalid"
            )
        if any(not math.isfinite(score) for score in final_scores):
            return tuple(truncated), self._degradation(
                "final_invalid_output", "final score invalid"
            )
        reranked = tuple(
            RetrievalHit(
                chunk=hit.chunk,
                score=hit.score,
                source=hit.source,
                rerank_score=float(score),
            )
            for score, hit in zip(final_scores, merged, strict=True)
        )
        reranked = tuple(sorted(reranked, key=lambda hit: (-hit.rerank_score, hit.chunk.chunk_id)))
        threshold = self._release.score_threshold
        if threshold is not None:
            reranked = tuple(hit for hit in reranked if (hit.rerank_score or 0.0) >= threshold)
        del profile
        return reranked, None

    @staticmethod
    def _group_by_library(hits: Sequence[RetrievalHit]) -> dict[str, list[RetrievalHit]]:
        grouped: dict[str, list[RetrievalHit]] = {}
        for hit in hits:
            grouped.setdefault(hit.source or "unknown", []).append(hit)
        return grouped

    def _degradation(self, reason: str, error: Exception | str) -> Mapping[str, Any]:
        detail = error.code if isinstance(error, PlatformError) else "unavailable"
        return {
            "code": "rerank_degraded",
            "kind": "rerank_degraded",
            "reason": reason,
            "provider": self._final.provider_name,
            "fallback": "preserve_candidate_order",
            "threshold": "not_applied",
            "detail": detail,
            "release": self._release.to_mapping(),
        }
