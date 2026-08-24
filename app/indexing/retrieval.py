from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from app.platform.errors import PlatformError
from app.platform.observability import ObservabilitySample, sample_success

from .generation import GenerationManager
from .models import (
    DocumentVisibilityFact,
    GenerationReferenceLease,
    IndexChunk,
    NarrowingScope,
    ProviderSearchPage,
    RetrievalHit,
    RetrievalProfile,
    RetrievalResult,
    RetrievalScope,
)
from .observability import (
    CANDIDATE_FILTER_ROUTE,
    CANDIDATE_REPLENISH_ROUTE,
    GRAPH_QUERY_SKIP_ROUTE,
    GRAPH_STALE_DURATION_ROUTE,
    record_index_observation,
)
from .providers import SparseIndexProvider
from .routing import MetadataPrefilter, RuleQueryRouter


def intersect_scopes(
    allowed: RetrievalScope | Mapping[str, Any],
    narrowing: NarrowingScope | Mapping[str, Any] | None,
) -> RetrievalScope:
    """Return the server scope intersected with an optional client narrowing scope."""

    server = RetrievalScope.from_value(allowed)
    client = NarrowingScope.from_value(narrowing)
    if client is None:
        return server
    spaces = server.space_ids if client.space_ids is None else server.space_ids & client.space_ids
    documents = server.document_ids
    if client.document_ids is not None:
        documents = client.document_ids if documents is None else documents & client.document_ids
    by_space: dict[str, frozenset[str]] = {}
    for space_id in spaces:
        server_documents = server.documents_by_space.get(space_id)
        if server_documents is None:
            if client.document_ids is not None:
                by_space[space_id] = frozenset(client.document_ids)
            continue
        if documents is not None:
            by_space[space_id] = frozenset(server_documents & documents)
        else:
            by_space[space_id] = server_documents
    return RetrievalScope(spaces, documents, by_space)


class AllowedRetrievalScopePort(Protocol):
    def allowed_retrieval_scope(self, principal: Any) -> RetrievalScope | Mapping[str, Any]: ...


class VisibilityFactsPort(Protocol):
    def get_visibility_fact(self, candidate: IndexChunk, principal: Any) -> Any: ...

    def get_visibility_facts(
        self, candidates: Sequence[IndexChunk], principal: Any
    ) -> Mapping[tuple[str, str], Any]: ...


class Reranker(Protocol):
    def rerank(
        self,
        query: str,
        hits: Sequence[RetrievalHit],
        profile: RetrievalProfile,
    ) -> tuple[Sequence[RetrievalHit], Mapping[str, Any] | None]: ...


class TreeRouter(Protocol):
    def __call__(
        self,
        query: str,
        candidates: Sequence[RetrievalHit],
        *,
        max_documents: int,
        rag_call_limit: int,
    ) -> Any: ...


class GraphRouter(Protocol):
    def __call__(
        self,
        query: str,
        candidates: Sequence[RetrievalHit],
        *,
        rag_call_limit: int,
        reader_lease: Any,
    ) -> Any: ...


class GraphReader(Protocol):
    def acquire_current_reader_lease(self, *, generation_id: str) -> Any: ...

    def release_reader_lease(self, lease: Any) -> None: ...


_EFFORT_LIMITS = {
    "quick": (1, 5),
    "think": (4, 7),
    "deep": (10, 9),
}
_VECTOR_CANDIDATE_RATIO = 0.7
SPARSE_EXACT_MATCH_ROUTE = "sparse_exact_match"


def allocate_candidate_quotas(limit: int, kinds: Sequence[str]) -> tuple[int, ...]:
    """Library baseline: vector 0.7 / BM25 0.3. A single provider keeps the full limit."""

    if limit < 1:
        raise PlatformError("validation_error", "candidate limit is invalid", {}, 422)
    if len(kinds) <= 1:
        return (limit,)
    vector = max(1, int(round(limit * _VECTOR_CANDIDATE_RATIO)))
    sparse = max(1, limit - vector)
    quotas: list[int] = []
    for kind in kinds:
        if kind == "dense":
            quotas.append(vector)
        elif kind == "sparse":
            quotas.append(sparse)
        else:
            quotas.append(max(1, limit // len(kinds)))
    return tuple(quotas)


def _backend_kind(provider: Any, *, fallback: str) -> str:
    kind = getattr(provider, "backend_kind", None)
    if kind in {"dense", "sparse"}:
        return str(kind)
    name = str(getattr(provider, "provider_name", "")).casefold()
    if name in {"milvus", "dense", "dense-memory"}:
        return "dense"
    if name in {"meilisearch", "opensearch", "sparse", "sparse-memory"}:
        return "sparse"
    return fallback


class TokenCounter(Protocol):
    def __call__(self, text: str) -> int: ...


def _conservative_token_count(text: str) -> int:
    """A local fallback that cannot undercount whitespace-free text."""

    return len(text)


@dataclass(frozen=True, slots=True)
class RerankResult:
    hits: tuple[RetrievalHit, ...]
    degradation: Mapping[str, Any] | None = None


class RetrievalRequest:
    """Keeps one generation reference lease for a request and its citations."""

    def __init__(self, service: RetrievalService, lease: GenerationReferenceLease) -> None:
        self._service = service
        self._lease = lease

    @property
    def generation_id(self) -> str:
        return self._lease.generation_id

    @property
    def lease_id(self) -> str:
        return self._lease.lease_id

    def __enter__(self) -> RetrievalRequest:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        self._service._generation.release_reference_lease(self._lease.lease_id)

    def search(
        self,
        query: str,
        *,
        principal: Any = None,
        narrowing_scope: NarrowingScope | Mapping[str, Any] | None = None,
        profile: RetrievalProfile | None = None,
    ) -> RetrievalResult:
        return self._service._search_with_lease(
            query,
            lease=self._lease,
            principal=principal,
            narrowing_scope=narrowing_scope,
            profile=profile,
        )

    def resolve_citation(self, hit: RetrievalHit, *, principal: Any = None) -> Mapping[str, Any]:
        return CitationService(
            self._service._visibility,
            self._service._generation,
            identity_access=self._service._identity,
        ).resolve(hit, principal=principal, generation_lease=self._lease)


class NoopReranker:
    """Development/CI adapter; it preserves provider order and reports degradation."""

    def __init__(self, *, environment: str = "test") -> None:
        self._environment = environment

    def rerank(
        self,
        query: str,
        hits: Sequence[RetrievalHit],
        profile: RetrievalProfile,
    ) -> tuple[Sequence[RetrievalHit], Mapping[str, Any] | None]:
        del query, profile
        if not hits:
            return (), None
        if self._environment not in {"development", "test", "ci"}:
            raise PlatformError(
                "reranker_unavailable",
                "RERANKER_PROVIDER=none is not allowed in production",
                {},
                503,
            )
        return tuple(hits), {
            "code": "rerank_degraded",
            "kind": "rerank_degraded",
            "provider": "none",
            "fallback": "preserve_candidate_order",
            "threshold": "not_applied",
        }


class ScoreReranker:
    """Small deterministic reranker adapter for tests and local deployments."""

    def rerank(
        self,
        query: str,
        hits: Sequence[RetrievalHit],
        profile: RetrievalProfile,
    ) -> tuple[Sequence[RetrievalHit], Mapping[str, Any] | None]:
        del query, profile
        ordered = sorted(hits, key=lambda hit: (-hit.score, hit.chunk.chunk_id))
        return (
            tuple(RetrievalHit(hit.chunk, hit.score, hit.source, hit.score) for hit in ordered),
            None,
        )


def _page(value: Any) -> ProviderSearchPage:
    if isinstance(value, ProviderSearchPage):
        return value
    if isinstance(value, Mapping):
        return ProviderSearchPage(
            tuple(value.get("items", value.get("candidates", ()))),
            value.get("cursor"),
        )
    if isinstance(value, tuple) and len(value) == 2:
        return ProviderSearchPage(tuple(value[0]), value[1])
    raise PlatformError("retrieval_degradation", "provider returned an invalid page", {}, 409)


def _provider_candidate(value: IndexChunk | Mapping[str, Any]) -> tuple[IndexChunk, float]:
    if isinstance(value, IndexChunk):
        return value, 0.0
    score = value.get("score", value.get("provider_score", 0.0))
    try:
        numeric_score = float(score)
    except (TypeError, ValueError) as exc:
        raise PlatformError("retrieval_degradation", "provider score is invalid", {}, 409) from exc
    if not math.isfinite(numeric_score):
        raise PlatformError("retrieval_degradation", "provider score is invalid", {}, 409)
    return IndexChunk.from_mapping(value), numeric_score


def _fact(value: Any, candidate: IndexChunk) -> DocumentVisibilityFact | None:
    if value is None:
        return None
    if isinstance(value, DocumentVisibilityFact):
        return value
    if isinstance(value, Mapping):
        required = (
            "document_id",
            "space_id",
            "lifecycle_status",
            "active_version_id",
            "active_publication_id",
            "publication_status",
            "manifest_hash",
            "readable",
        )
        if any(key not in value for key in required):
            return None
        return DocumentVisibilityFact(
            document_id=str(value["document_id"]),
            space_id=str(value["space_id"]),
            lifecycle_status=str(value["lifecycle_status"]),
            active_version_id=(
                str(value["active_version_id"]) if value["active_version_id"] is not None else None
            ),
            active_publication_id=(
                str(value["active_publication_id"])
                if value["active_publication_id"] is not None
                else None
            ),
            publication_status=(
                str(value["publication_status"])
                if value["publication_status"] is not None
                else None
            ),
            manifest_hash=(
                str(value["manifest_hash"]) if value["manifest_hash"] is not None else None
            ),
            readable=bool(value["readable"]),
        )
    return None


class RetrievalService:
    """Unified retrieval port with server-owned scope and cursor replenishment."""

    def __init__(
        self,
        generation_manager: GenerationManager,
        providers: Sequence[SparseIndexProvider],
        *,
        identity_access: AllowedRetrievalScopePort | Any | None = None,
        visibility_facts: VisibilityFactsPort | Callable[[IndexChunk, Any], Any] | None = None,
        reranker: Reranker | None = None,
        environment: str = "test",
        profile_resolver: Callable[[RetrievalProfile, str], RetrievalProfile] | None = None,
        query_router: RuleQueryRouter | None = None,
        tree_router: TreeRouter | None = None,
        graph_router: GraphRouter | None = None,
        graph_reader: GraphReader | None = None,
        token_counter: TokenCounter | None = None,
        exact_match_metrics: Any = None,
    ) -> None:
        if not providers:
            raise ValueError("at least one retrieval provider is required")
        self._generation = generation_manager
        self._providers = tuple(providers)
        self._sparse_sources = frozenset(
            str(getattr(provider, "provider_name", ""))
            for provider in self._providers
            if _backend_kind(provider, fallback="sparse") == "sparse"
        )
        self._identity = identity_access
        self._visibility = visibility_facts
        self._reranker = reranker or NoopReranker(environment=environment)
        self._profile_resolver = profile_resolver
        self._query_router = query_router or RuleQueryRouter()
        self._tree_router = tree_router
        self._graph_router = graph_router
        self._graph_reader = graph_reader
        self._token_counter = token_counter or _conservative_token_count
        self._exact_match_metrics = exact_match_metrics

    def open_request(self) -> RetrievalRequest:
        return RetrievalRequest(self, self._generation.acquire_reference_lease())

    def _allowed_scope(self, principal: Any) -> RetrievalScope:
        if self._identity is None:
            return RetrievalScope(frozenset())
        if callable(self._identity):
            return RetrievalScope.from_value(self._identity(principal))
        getter = getattr(self._identity, "allowed_retrieval_scope", None)
        if getter is None:
            getter = getattr(self._identity, "get_allowed_retrieval_scope", None)
        if getter is None:
            raise PlatformError(
                "authorization_scope_unavailable", "Allowed retrieval scope is unavailable", {}, 503
            )
        return RetrievalScope.from_value(getter(principal))

    def _visibility_fact(
        self, candidate: IndexChunk, principal: Any
    ) -> DocumentVisibilityFact | None:
        if self._visibility is None:
            return None
        if callable(self._visibility):
            return _fact(self._visibility(candidate, principal), candidate)
        getter = getattr(self._visibility, "get_visibility_fact", None)
        if getter is None:
            getter = getattr(self._visibility, "visibility_fact", None)
        if getter is None:
            raise PlatformError(
                "visibility_unavailable", "Document visibility facts are unavailable", {}, 503
            )
        return _fact(getter(candidate, principal), candidate)

    def _visibility_facts(
        self, candidates: Sequence[IndexChunk], principal: Any
    ) -> Mapping[tuple[str, str], DocumentVisibilityFact | None]:
        if not candidates:
            return {}
        if self._visibility is None:
            return {}
        if callable(self._visibility):
            return {
                (candidate.space_id, candidate.document_id): _fact(
                    self._visibility(candidate, principal), candidate
                )
                for candidate in candidates
            }
        getter = getattr(self._visibility, "get_visibility_facts", None)
        if callable(getter):
            values = getter(candidates, principal)
            return {
                (candidate.space_id, candidate.document_id): _fact(
                    values.get((candidate.space_id, candidate.document_id)), candidate
                )
                for candidate in candidates
            }
        return {
            (candidate.space_id, candidate.document_id): self._visibility_fact(candidate, principal)
            for candidate in candidates
        }

    @staticmethod
    def _visible(
        candidate: IndexChunk,
        fact: DocumentVisibilityFact | None,
        scope: RetrievalScope,
        generation_id: str,
    ) -> bool:
        return bool(
            candidate.indexable
            and candidate.generation_id == generation_id
            and scope.allows(space_id=candidate.space_id, document_id=candidate.document_id)
            and fact is not None
            and fact.document_id == candidate.document_id
            and fact.space_id == candidate.space_id
            and fact.lifecycle_status == "active"
            and fact.active_version_id == candidate.document_version_id
            and fact.active_publication_id == candidate.publication_id
            and fact.publication_status == "active"
            and fact.manifest_hash == candidate.manifest_hash
            and fact.readable
        )

    @staticmethod
    def _document_candidates(
        hits: Sequence[RetrievalHit], max_documents: int
    ) -> tuple[RetrievalHit, ...]:
        candidates: list[RetrievalHit] = []
        document_ids: set[tuple[str, str]] = set()
        for hit in hits:
            document_key = (hit.chunk.space_id, hit.chunk.document_id)
            if document_key in document_ids:
                continue
            document_ids.add(document_key)
            candidates.append(hit)
            if len(candidates) == max_documents:
                break
        return tuple(candidates)

    @staticmethod
    def _routing_degradation(route: str, error: Exception) -> Mapping[str, Any]:
        return {
            "code": f"{route}_degraded",
            "reason": error.code if isinstance(error, PlatformError) else "unavailable",
        }

    def _observe_sparse_exact_match(
        self,
        query: str,
        generation_id: str,
        hits: Sequence[RetrievalHit],
    ) -> None:
        """Sample sparse exact-text matches into the shared observability read path.

        Best-effort only: recording must never alter, delay, or fail retrieval, and
        the sample carries no query or chunk text — only a stable hash decides
        sampling, and the recorded route template identifies the signal.
        """

        metrics = self._exact_match_metrics
        if metrics is None or not hits:
            return
        rate = float(getattr(metrics, "success_sample_rate", 0.0) or 0.0)
        if rate <= 0:
            return
        normalized_query = query.strip().casefold()
        if not normalized_query:
            return
        try:
            observed_at = datetime.now(UTC)
            for hit in hits:
                if hit.chunk.text.strip().casefold() != normalized_query:
                    continue
                stable_key = hashlib.sha256(
                    f"{generation_id}:{hit.chunk.dedupe_key}:{normalized_query}".encode()
                ).hexdigest()
                selected, weight = sample_success(stable_key, rate)
                if not selected:
                    continue
                metrics.record(
                    ObservabilitySample(
                        observed_at_utc=observed_at,
                        route_template=SPARSE_EXACT_MATCH_ROUTE,
                        method="POST",
                        outcome_class="success",
                        status_family="2xx",
                        latency_ms=0,
                        sample_weight=weight,
                    )
                )
        except Exception:
            return

    def _collect_provider_hits(
        self,
        provider: Any,
        query: str,
        *,
        generation_id: str,
        scope: RetrievalScope,
        quota: int,
        principal: Any,
        metadata_prefilter: MetadataPrefilter | None = None,
    ) -> list[RetrievalHit]:
        hits: list[RetrievalHit] = []
        cursor: str | None = None
        cursors_seen: set[str | None] = set()
        provider_seen = 0
        page_number = 0
        while provider_seen < quota:
            if cursor in cursors_seen:
                raise PlatformError(
                    "retrieval_degradation",
                    "provider cursor did not advance",
                    {},
                    409,
                )
            cursors_seen.add(cursor)
            page = _page(
                provider.search(
                    query,
                    tuple(sorted(scope.space_ids)),
                    quota,
                    cursor,
                    generation_id=generation_id,
                )
            )
            if not page.items and page.cursor is None:
                break
            candidates = tuple(_provider_candidate(raw) for raw in page.items)
            facts = self._visibility_facts(
                tuple(candidate for candidate, _ in candidates), principal
            )
            rejected = 0
            for candidate, provider_score in candidates:
                fact = facts.get((candidate.space_id, candidate.document_id))
                if not self._visible(candidate, fact, scope, generation_id):
                    rejected += 1
                    continue
                if metadata_prefilter is not None and not metadata_prefilter.matches(
                    candidate.metadata
                ):
                    continue
                provider_seen += 1
                source = str(getattr(provider, "provider_name", ""))
                score = (
                    0.0
                    if _backend_kind(provider, fallback="sparse") == "sparse"
                    else provider_score
                )
                hits.append(RetrievalHit(candidate, score, source))
                if provider_seen >= quota:
                    break
            record_index_observation(
                self._exact_match_metrics,
                CANDIDATE_FILTER_ROUTE,
                success=True,
                count=rejected,
            )
            if page_number > 0:
                record_index_observation(
                    self._exact_match_metrics,
                    CANDIDATE_REPLENISH_ROUTE,
                    success=True,
                    count=len(candidates),
                )
            if page.cursor is None:
                break
            cursor = page.cursor
            page_number += 1
        return hits

    def _collect_hybrid_hits(
        self,
        query: str,
        generation_id: str,
        scope: RetrievalScope,
        profile: RetrievalProfile,
        principal: Any,
        metadata_prefilter: MetadataPrefilter | None,
    ) -> tuple[list[RetrievalHit], list[Mapping[str, Any]]]:
        kinds = tuple(
            _backend_kind(provider, fallback="dense" if index == 0 else "sparse")
            for index, provider in enumerate(self._providers)
        )
        quotas = allocate_candidate_quotas(profile.candidate_limit, kinds)
        degradations: list[Mapping[str, Any]] = []
        collected: list[tuple[str, list[RetrievalHit]]] = []
        failures: list[str] = []
        failure_reasons: dict[str, str] = {}

        def run(index: int) -> tuple[int, str, list[RetrievalHit] | Exception]:
            provider = self._providers[index]
            kind = kinds[index]
            try:
                return (
                    index,
                    kind,
                    self._collect_provider_hits(
                        provider,
                        query,
                        generation_id=generation_id,
                        scope=scope,
                        quota=quotas[index],
                        principal=principal,
                        metadata_prefilter=metadata_prefilter,
                    ),
                )
            except Exception as error:
                return index, kind, error

        if len(self._providers) == 1:
            outcomes = [run(0)]
        else:
            with ThreadPoolExecutor(max_workers=len(self._providers)) as pool:
                outcomes = list(pool.map(run, range(len(self._providers))))
        outcomes.sort(key=lambda item: item[0])
        for _index, kind, payload in outcomes:
            if isinstance(payload, Exception):
                failures.append(kind)
                failure_reasons[kind] = (
                    payload.code if isinstance(payload, PlatformError) else "unavailable"
                )
                continue
            collected.append((kind, payload))
            if kind == "sparse":
                self._observe_sparse_exact_match(query, generation_id, payload)
        if not collected:
            raise PlatformError(
                "retrieval_failed",
                "dense and sparse retrieval failed",
                {
                    "failed": failures,
                    "failed_libraries": failures,
                    "failure_reasons": failure_reasons,
                    "retryable": True,
                    "query_failure": {
                        "code": "retrieval_query_failed",
                        "failed_libraries": failures,
                        "retryable": True,
                    },
                },
                503,
            )
        if failures:
            degradations.append(
                {
                    "code": "retrieval_degraded",
                    "kind": "retrieval_degraded",
                    "failed": tuple(failures),
                    "failed_libraries": tuple(failures),
                    "failure_reasons": dict(failure_reasons),
                }
            )
        hits: list[RetrievalHit] = []
        seen_keys: set[tuple[str, str, str]] = set()
        for _kind, provider_hits in collected:
            for hit in provider_hits:
                if hit.chunk.dedupe_key in seen_keys:
                    continue
                seen_keys.add(hit.chunk.dedupe_key)
                hits.append(hit)
        return hits, degradations

    def search(
        self,
        query: str,
        *,
        principal: Any = None,
        narrowing_scope: NarrowingScope | Mapping[str, Any] | None = None,
        profile: RetrievalProfile | None = None,
    ) -> RetrievalResult:
        with self.open_request() as request:
            return request.search(
                query,
                principal=principal,
                narrowing_scope=narrowing_scope,
                profile=profile,
            )

    def _search_with_lease(
        self,
        query: str,
        *,
        lease: GenerationReferenceLease,
        principal: Any = None,
        narrowing_scope: NarrowingScope | Mapping[str, Any] | None = None,
        profile: RetrievalProfile | None = None,
    ) -> RetrievalResult:
        if not isinstance(query, str) or not query.strip():
            raise PlatformError("validation_error", "query is required", {}, 422)
        route = self._query_router.route(query)
        selected = profile or RetrievalProfile()
        if self._profile_resolver is not None:
            selected = self._profile_resolver(
                RetrievalProfile(profile_id=selected.profile_id, version=selected.version),
                lease.generation_id,
            )
        scope = intersect_scopes(self._allowed_scope(principal), narrowing_scope)
        if scope.is_empty:
            return RetrievalResult(
                (), lease.generation_id, selected, route_output=route.to_mapping()
            )
        hits: list[RetrievalHit] = []
        degradations: list[Mapping[str, Any]] = []
        seen_route_keys: set[tuple[str, str, str]] = set()
        # Every route output keeps the default hybrid retrieval running. Split
        # questions are additive logical retrieval units, never exclusive routes.
        for search_query in route.search_queries():
            query_hits, query_degradations = self._collect_hybrid_hits(
                search_query,
                lease.generation_id,
                scope,
                selected,
                principal,
                route.metadata_prefilter,
            )
            degradations.extend(query_degradations)
            for hit in query_hits:
                if hit.chunk.dedupe_key in seen_route_keys:
                    continue
                seen_route_keys.add(hit.chunk.dedupe_key)
                hits.append(hit)
        candidate_hits = tuple(hits)
        rerank_degraded = False
        try:
            reranked, degradation = self._reranker.rerank(query, hits, selected)
        except Exception:
            reranked = tuple(hits)
            degradation = {
                "code": "rerank_degraded",
                "kind": "rerank_degraded",
                "reason": "provider_unavailable",
                "fallback": "preserve_candidate_order",
                "threshold": "not_applied",
            }
        if degradation is not None:
            degradations.append(degradation)
            rerank_degraded = True
        # Deterministic order across rerankers: equal scores fall back to a
        # stable chunk_id tie-break (A6).
        if not rerank_degraded:
            reranked = tuple(
                sorted(
                    reranked,
                    key=lambda hit: (
                        -(hit.rerank_score if hit.rerank_score is not None else hit.score),
                        hit.chunk.chunk_id,
                    ),
                )
            )
        # Re-read lifecycle/publication/ACL facts after reranking. A document can
        # be retired while an upstream provider or cross-encoder is running.
        if self._visibility is not None and reranked:
            final_facts = self._visibility_facts(tuple(hit.chunk for hit in reranked), principal)
            reranked = tuple(
                hit
                for hit in reranked
                if self._visible(
                    hit.chunk,
                    final_facts.get((hit.chunk.space_id, hit.chunk.document_id)),
                    scope,
                    lease.generation_id,
                )
            )
        rag_call_limit, tree_document_limit = _EFFORT_LIMITS[selected.effort]
        tree_candidates = self._document_candidates(reranked, tree_document_limit)
        has_final_scores = bool(tree_candidates) and all(
            hit.rerank_score is not None for hit in tree_candidates
        )
        if reranked and not has_final_scores and not rerank_degraded:
            degradations.append(
                {
                    "code": "rerank_degraded",
                    "kind": "rerank_degraded",
                    "reason": "final_score_unavailable",
                    "fallback": "preserve_candidate_order",
                    "threshold": "not_applied",
                }
            )
        if (
            selected.route_tree
            and selected.effort != "quick"
            and self._tree_router is not None
            and has_final_scores
        ):
            try:
                tree_outcome = self._tree_router(
                    query,
                    tree_candidates,
                    max_documents=tree_document_limit,
                    rag_call_limit=rag_call_limit,
                )
                if tree_outcome is not None:
                    skipped = bool(getattr(tree_outcome, "skipped", False))
                    reason = getattr(tree_outcome, "reason", None)
                    if skipped and reason:
                        if str(reason) in {"budget_exhausted", "cost_unavailable"}:
                            degradations.append(
                                {
                                    "code": "retrieval_degraded",
                                    "kind": "retrieval_degraded",
                                    "detail": {"reason": str(reason)},
                                }
                            )
                        else:
                            degradations.append(
                                {
                                    "code": "retrieval_degraded",
                                    "kind": "retrieval_degraded",
                                    "reason": str(reason),
                                    "stage": "tree",
                                }
                            )
                    for document in tuple(getattr(tree_outcome, "documents", ())):
                        status = str(getattr(document, "status", ""))
                        if status == "missing_document_identity":
                            degradations.append(
                                {
                                    "code": "retrieval_degraded",
                                    "kind": "retrieval_degraded",
                                    "reason": "missing_document_identity",
                                    "stage": "tree",
                                }
                            )
                        elif status == "degraded":
                            degradations.append(
                                {
                                    "code": "retrieval_degraded",
                                    "kind": "retrieval_degraded",
                                    "reason": str(
                                        getattr(document, "reason", None) or "unavailable"
                                    ),
                                    "stage": "tree",
                                }
                            )
            except Exception as error:
                degradations.append(self._routing_degradation("tree", error))
        if selected.route_graph:
            graph_lease = None
            try:
                if self._graph_router is None:
                    raise PlatformError(
                        "graph_unavailable",
                        "public graph routing is unavailable",
                        {},
                        503,
                    )
                if self._graph_reader is None:
                    raise PlatformError(
                        "reader_unavailable",
                        "graph reader lease is unavailable",
                        {},
                        503,
                    )
                else:
                    graph_lease = self._graph_reader.acquire_current_reader_lease(
                        generation_id=lease.generation_id
                    )
                    self._graph_router(
                        query,
                        tree_candidates,
                        rag_call_limit=rag_call_limit,
                        reader_lease=graph_lease,
                    )
            except Exception as error:
                degradations.append(self._routing_degradation("graph", error))
                record_index_observation(
                    self._exact_match_metrics,
                    GRAPH_QUERY_SKIP_ROUTE,
                    success=False,
                )
                if isinstance(error, PlatformError) and error.code == "graph_stale":
                    stale_duration_ms = error.details.get("stale_duration_ms", 0)
                    if isinstance(stale_duration_ms, int):
                        record_index_observation(
                            self._exact_match_metrics,
                            GRAPH_STALE_DURATION_ROUTE,
                            success=True,
                            latency_ms=stale_duration_ms,
                        )
            finally:
                if graph_lease is not None:
                    self._graph_reader.release_reader_lease(graph_lease)
        # A reranker fallback has no final score, so a profile threshold is not
        # applicable. Thresholding a fallback would silently turn a degraded
        # retrieval into an empty context.
        filtered = [
            hit
            for hit in reranked
            # A rerank fallback has no final 8B score, so no threshold applies;
            # a healthy rerank thresholds ONLY on the 8B score (never the raw
            # per-library score), while sparse exact-match hits keep their
            # knowledge-graph-sparse-index exemption from the final threshold.
            if rerank_degraded
            or selected.score_threshold is None
            or hit.source in self._sparse_sources
            or (hit.rerank_score is not None and hit.rerank_score >= selected.score_threshold)
        ]
        budgeted: list[RetrievalHit] = []
        deferred: list[RetrievalHit] = []
        used_by_space: dict[str, int] = {}
        used_total = 0

        def include(hit: RetrievalHit) -> bool:
            nonlocal used_total
            tokens = self._token_counter(hit.chunk.text)
            if not isinstance(tokens, int) or tokens < 1:
                raise PlatformError("retrieval_degradation", "token counter is invalid", {}, 409)
            space_used = used_by_space.get(hit.chunk.space_id, 0)
            if (
                space_used + tokens > selected.retrieval_context_tokens_per_space
                or used_total + tokens > selected.retrieval_context_tokens_cap
            ):
                degradations.append(
                    {
                        "code": "retrieval_context_budget_exceeded",
                        "space_id": hit.chunk.space_id,
                        "token_count": tokens,
                    }
                )
                return False
            used_by_space[hit.chunk.space_id] = space_used + tokens
            used_total += tokens
            budgeted.append(hit)
            return True

        selected_per_space: dict[str, int] = {}
        selected_per_library: dict[str, int] = {}
        library_quota_enabled = len({hit.source or "unknown" for hit in filtered}) > 1
        for hit in filtered:
            library = hit.source or "unknown"
            library_count = selected_per_library.get(library, 0)
            if (
                library_quota_enabled
                and library_count >= selected.retrieval_context_items_per_space
            ):
                deferred.append(hit)
                continue
            count = selected_per_space.get(hit.chunk.space_id, 0)
            if not library_quota_enabled and count >= selected.retrieval_context_items_per_space:
                deferred.append(hit)
                continue
            if include(hit):
                selected_per_space[hit.chunk.space_id] = count + 1
                selected_per_library[library] = library_count + 1
        if any(
            selected_per_space.get(space_id, 0) < selected.retrieval_context_items_per_space
            for space_id in scope.space_ids
        ):
            for hit in deferred:
                if len(budgeted) >= selected.top_k:
                    break
                library = hit.source or "unknown"
                if (
                    library_quota_enabled
                    and selected_per_library.get(library, 0)
                    >= selected.retrieval_context_items_per_space
                ):
                    continue
                if include(hit):
                    selected_per_library[library] = selected_per_library.get(library, 0) + 1
        result_limit = selected.top_k if not library_quota_enabled else len(budgeted)
        return RetrievalResult(
            tuple(budgeted[:result_limit]),
            lease.generation_id,
            selected,
            tuple(degradations),
            candidate_hits,
            route.to_mapping(),
        )


class CitationService:
    """Re-checks document facts before returning immutable citation metadata."""

    def __init__(
        self,
        visibility_facts: VisibilityFactsPort | Callable[[IndexChunk, Any], Any],
        generation_manager: GenerationManager | None = None,
        *,
        identity_access: AllowedRetrievalScopePort | Any | None = None,
    ) -> None:
        self._visibility = visibility_facts
        self._generation = generation_manager
        self._identity = identity_access

    def _allowed_scope(self, principal: Any) -> RetrievalScope:
        if self._identity is None:
            return RetrievalScope(frozenset())
        if callable(self._identity):
            return RetrievalScope.from_value(self._identity(principal))
        getter = getattr(self._identity, "allowed_retrieval_scope", None)
        if getter is None:
            getter = getattr(self._identity, "get_allowed_retrieval_scope", None)
        if getter is None:
            raise PlatformError(
                "authorization_scope_unavailable", "Allowed retrieval scope is unavailable", {}, 503
            )
        return RetrievalScope.from_value(getter(principal))

    def resolve(
        self,
        hit: RetrievalHit,
        *,
        principal: Any = None,
        generation_lease: GenerationReferenceLease | None = None,
    ) -> Mapping[str, Any]:
        candidate = hit.chunk
        lease = (
            generation_lease
            if generation_lease is not None
            else (
                self._generation.acquire_reference_lease() if self._generation is not None else None
            )
        )
        try:
            generation_id = lease.generation_id if lease is not None else candidate.generation_id
            if candidate.generation_id != generation_id:
                return {"state": "unavailable"}
            if callable(self._visibility):
                fact = _fact(self._visibility(candidate, principal), candidate)
            else:
                getter = getattr(self._visibility, "get_visibility_fact", None)
                if getter is None:
                    getter = getattr(self._visibility, "visibility_fact", None)
                fact = _fact(getter(candidate, principal), candidate) if getter else None
            if fact is None or not RetrievalService._visible(
                candidate,
                fact,
                self._allowed_scope(principal),
                generation_id,
            ):
                return {"state": "unavailable"}
            return {
                "state": "available",
                "document_id": candidate.document_id,
                "document_version_id": candidate.document_version_id,
                "publication_id": candidate.publication_id,
                "generation_id": candidate.generation_id,
                "chunk_id": candidate.chunk_id,
                "locator": dict(candidate.locator),
                "snippet": candidate.snippet,
            }
        finally:
            if lease is not None and generation_lease is None:
                self._generation.release_reference_lease(lease.lease_id)


__all__ = [
    "AllowedRetrievalScopePort",
    "CitationService",
    "NoopReranker",
    "allocate_candidate_quotas",
    "GraphRouter",
    "GraphReader",
    "RetrievalRequest",
    "RetrievalService",
    "RerankResult",
    "ScoreReranker",
    "TokenCounter",
    "TreeRouter",
    "VisibilityFactsPort",
    "intersect_scopes",
]
