"""Configurable retrieval profiles for reusable RAG applications."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.providers.contracts import RetrievalRequest, RetrievalResult, RetrieverProvider

DEFAULT_RELAXED_FILTER_PRESERVE_KEYS = ("space_id", "spaceId", "tenant_id", "tenantId")


@dataclass(frozen=True)
class RetrievalProfile:
    """Named retrieval composition settings."""

    name: str
    description: str
    top_k_multiplier: int = 1
    relaxed_filter_fallback: bool = False
    relaxed_filter_preserve_keys: tuple[str, ...] = DEFAULT_RELAXED_FILTER_PRESERVE_KEYS


class RetrievalProfileRegistry:
    """Registry for named retrieval profiles."""

    def __init__(self) -> None:
        self._profiles: dict[str, RetrievalProfile] = {}

    def register(self, profile: RetrievalProfile) -> None:
        name = _normalize_profile_id(profile.name)
        if not name:
            raise ValueError("retrieval profile name must not be empty")
        if name in self._profiles:
            raise ValueError(f"retrieval profile already registered: {name}")
        self._profiles[name] = RetrievalProfile(
            name=name,
            description=profile.description,
            top_k_multiplier=max(1, profile.top_k_multiplier),
            relaxed_filter_fallback=profile.relaxed_filter_fallback,
            relaxed_filter_preserve_keys=profile.relaxed_filter_preserve_keys,
        )

    def get(self, name: str) -> RetrievalProfile:
        normalized = _normalize_profile_id(name)
        if normalized not in self._profiles:
            raise KeyError(normalized)
        return self._profiles[normalized]

    def names(self) -> list[str]:
        return list(self._profiles)


@dataclass
class RequestTransformingRetriever:
    """Retriever wrapper that adjusts request shape for a profile branch."""

    retriever: RetrieverProvider
    profile_name: str
    branch_name: str
    top_k_multiplier: int = 1
    preserve_filter_keys: tuple[str, ...] | None = None

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        filters = dict(request.filters)
        dropped_filters: list[str] = []
        if self.preserve_filter_keys is not None:
            preserved = set(self.preserve_filter_keys)
            dropped_filters = sorted(key for key in filters if key not in preserved)
            filters = {key: value for key, value in filters.items() if key in preserved}

        transformed_request = RetrievalRequest(
            query=request.query,
            top_k=_multiply_top_k(request.top_k, self.top_k_multiplier),
            filters=filters,
        )
        result = self.retriever.retrieve(transformed_request)
        debug = dict(result.debug)
        debug.update(
            {
                "profile": self.profile_name,
                "profile_branch": self.branch_name,
                "top_k_multiplier": self.top_k_multiplier,
                "original_top_k": request.top_k,
                "effective_top_k": transformed_request.top_k,
            }
        )
        if self.preserve_filter_keys is not None:
            debug["preserved_filter_keys"] = list(self.preserve_filter_keys)
            debug["dropped_filters"] = dropped_filters

        return RetrievalResult(
            query=result.query,
            rewritten_query=result.rewritten_query,
            documents=result.documents,
            sources=result.sources,
            debug=debug,
        )


def build_default_retrieval_profile_registry(
    *,
    high_recall_top_k_multiplier: int = 2,
    relaxed_filter_preserve_keys: Sequence[str] | str = DEFAULT_RELAXED_FILTER_PRESERVE_KEYS,
) -> RetrievalProfileRegistry:
    """Return built-in retrieval profiles."""

    preserve_keys = parse_filter_key_list(relaxed_filter_preserve_keys)
    registry = RetrievalProfileRegistry()
    registry.register(
        RetrievalProfile(
            name="default",
            description="Single strict vector retriever with request filters preserved.",
        )
    )
    registry.register(
        RetrievalProfile(
            name="high_recall",
            description=(
                "Widen strict vector retrieval and add a relaxed-filter fallback while "
                "preserving space and tenant isolation keys."
            ),
            top_k_multiplier=max(1, high_recall_top_k_multiplier),
            relaxed_filter_fallback=True,
            relaxed_filter_preserve_keys=preserve_keys or DEFAULT_RELAXED_FILTER_PRESERVE_KEYS,
        )
    )
    return registry


def build_retrievers_for_profile(
    base_retriever: RetrieverProvider,
    profile: RetrievalProfile,
) -> tuple[RetrieverProvider, list[RetrieverProvider]]:
    """Build primary and additional retrievers for a profile."""

    if profile.name == "default" and profile.top_k_multiplier == 1:
        return base_retriever, []

    primary = RequestTransformingRetriever(
        retriever=base_retriever,
        profile_name=profile.name,
        branch_name="strict",
        top_k_multiplier=profile.top_k_multiplier,
    )
    additional: list[RetrieverProvider] = []
    if profile.relaxed_filter_fallback:
        additional.append(
            RequestTransformingRetriever(
                retriever=base_retriever,
                profile_name=profile.name,
                branch_name="relaxed_filters",
                top_k_multiplier=profile.top_k_multiplier,
                preserve_filter_keys=profile.relaxed_filter_preserve_keys,
            )
        )
    return primary, additional


def parse_filter_key_list(value: Sequence[str] | str) -> tuple[str, ...]:
    """Parse a comma-separated filter key list into a stable tuple."""

    if isinstance(value, str):
        candidates = value.split(",")
    else:
        candidates = list(value)
    keys: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).strip()
        if not key or key in seen:
            continue
        keys.append(key)
        seen.add(key)
    return tuple(keys)


def _multiply_top_k(top_k: int | None, multiplier: int) -> int | None:
    if top_k is None:
        return None
    return max(1, top_k) * max(1, multiplier)


def _normalize_profile_id(value: str) -> str:
    return str(value).strip().lower().replace("-", "_")
