"""Contextual Retrieval domain contract and prefix-aware execution."""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from .models import IndexChunk
from .prefix_cache import PrefixCacheKey, PrefixCacheManager

CONTEXTUAL_MODEL = "ds-v4-flash"
CONTEXTUAL_PROMPT_SCHEMA_VERSION = "contextual-prefix-v1"
CONTEXTUAL_TOKENIZATION_VERSION = "unicode-approx-v1"
CONTEXTUAL_PREFIX_TOKEN_LIMIT = 30_000
CONTEXTUAL_WARMUP_CALLS = 2
CONTEXTUAL_PROVIDER_ATTEMPTS = 5

CONTEXTUAL_SYSTEM_INSTRUCTION = (
    "You contextualize one retrievable chunk for a knowledge-base index. Use the "
    "document metadata and document text to explain the chunk's subject, scope, "
    "and relationships. Do not answer the chunk or add facts that are absent."
)
CONTEXTUAL_OUTPUT_INSTRUCTION = (
    "Return one concise contextual paragraph in the document language. It will be "
    "prepended to the original chunk for embedding; do not repeat the entire chunk."
)

_TOKEN_WORD = re.compile(r"[A-Za-z0-9_]+", re.UNICODE)


class ContextualProviderUnavailable(Exception):
    """A retryable CR provider failure."""


class ContextualProviderRejected(Exception):
    """A non-retryable CR provider/schema failure."""


def approximate_token_count(text: str) -> int:
    """Stable offline estimate used only to group deterministic prefixes."""

    words = len(_TOKEN_WORD.findall(text))
    word_characters = sum(len(match) for match in _TOKEN_WORD.findall(text))
    other_characters = len(text) - word_characters
    return words + other_characters


@dataclass(frozen=True, slots=True)
class ContextualGeneration:
    context: str
    provider: str
    model: str
    input_tokens: int | None = None
    prompt_cache_hit_tokens: int | None = None
    prompt_cache_miss_tokens: int | None = None
    output_tokens: int | None = None
    provider_request_id: str | None = None


class ContextualRetrievalProvider(Protocol):
    provider: str
    model: str
    model_revision: str

    def generate(
        self,
        *,
        prompt: str,
        chunk_id: str,
        warmup: bool,
    ) -> ContextualGeneration: ...


@dataclass(frozen=True, slots=True)
class ContextualDocument:
    instance_id: str
    space_id: str
    document_id: str
    document_version_id: str
    generation_id: str
    metadata: Mapping[str, Any]
    full_text: str


@dataclass(frozen=True, slots=True)
class ContextualUsageFact:
    provider: str
    model: str
    operation: str
    chunk_id: str
    unit_id: str
    attempt: int
    result: str
    request_fingerprint: str
    cache_mode: str
    warmup: bool
    input_tokens: int | None = None
    prompt_cache_hit_tokens: int | None = None
    prompt_cache_miss_tokens: int | None = None
    output_tokens: int | None = None
    provider_request_id: str | None = None


@dataclass(frozen=True, slots=True)
class ContextualChunkResult:
    chunk_id: str
    context: str | None
    provider: str
    model: str
    cache_mode: str
    attempts: int
    degraded_reason: str | None = None
    input_tokens: int | None = None
    prompt_cache_hit_tokens: int | None = None
    prompt_cache_miss_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ContextualUnitResult:
    unit_id: str
    grouping: str
    chunks: tuple[ContextualChunkResult, ...]
    cache_mode: str
    cache_outage: bool
    cache_reason: str
    prefix_truncated: bool
    estimated_prefix_tokens: int
    warmup_chunk_ids: tuple[str, ...]
    concurrent_chunk_ids: tuple[str, ...]
    prompt_cache_hit_tokens: int
    prompt_cache_miss_tokens: int


@dataclass(frozen=True, slots=True)
class ContextualEnhancementOutput:
    contexts: Mapping[str, str]
    chunk_results: Mapping[str, ContextualChunkResult]
    units: tuple[ContextualUnitResult, ...]
    degradations: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class PrefixUnit:
    unit_id: str
    chunks: tuple[IndexChunk, ...]
    prefix: str
    grouping: str
    prefix_truncated: bool = False


def contextual_target(chunk: IndexChunk) -> bool:
    """Select only leaf chunks whose path has no equivalent context mechanism."""

    unit = str(chunk.metadata.get("cr_unit", "")).casefold()
    return unit in {"chunk", "symbol"} and not bool(chunk.metadata.get("table"))


def normalized_metadata(metadata: Mapping[str, Any]) -> str:
    import json

    stable = {str(key): metadata[key] for key in sorted(metadata, key=str)}
    return json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _source_text(document_text: str) -> str:
    return document_text.strip()


def stable_prefix(
    metadata: Mapping[str, Any], source_text: str, *, source_label: str = "DOCUMENT FULL TEXT"
) -> str:
    return (
        "SYSTEM INSTRUCTION\n"
        f"{CONTEXTUAL_SYSTEM_INSTRUCTION}\n\n"
        "DOCUMENT METADATA\n"
        f"{normalized_metadata(metadata)}\n\n"
        f"{source_label}\n"
        "<<<BEGIN\n"
        f"{_source_text(source_text)}\n"
        "END>>>"
    )


def dynamic_suffix(chunk_ordinal: int, chunk: IndexChunk) -> str:
    return (
        "CHUNK\n"
        f"ordinal: {chunk_ordinal}\n"
        f"id: {chunk.chunk_id}\n"
        "<<<BEGIN\n"
        f"{chunk.text}\n"
        "END>>>\n\n"
        "OUTPUT FORMAT\n"
        f"{CONTEXTUAL_OUTPUT_INSTRUCTION}"
    )


def compose_prompt(prefix: str, suffix: str) -> str:
    return f"{prefix}\n\n{suffix}"


def _fit_source(
    metadata: Mapping[str, Any],
    source: str,
    *,
    limit: int,
    token_counter: Callable[[str], int],
    source_label: str,
) -> tuple[str, bool]:
    candidate = stable_prefix(metadata, source, source_label=source_label)
    if token_counter(candidate) <= limit:
        return candidate, False
    empty = stable_prefix(metadata, "", source_label=source_label)
    overhead = token_counter(empty)
    if overhead >= limit:
        raise ValueError("contextual prefix limit cannot fit prompt metadata")
    low, high = 0, len(source)
    while low < high:
        middle = (low + high + 1) // 2
        fitted = stable_prefix(metadata, source[:middle], source_label=source_label)
        if token_counter(fitted) <= limit:
            low = middle
        else:
            high = middle - 1
    return stable_prefix(metadata, source[:low], source_label=source_label), True


def plan_prefix_units(
    document: ContextualDocument,
    chunks: Sequence[IndexChunk],
    *,
    token_counter: Callable[[str], int],
    token_limit: int = CONTEXTUAL_PREFIX_TOKEN_LIMIT,
) -> tuple[PrefixUnit, ...]:
    if not chunks:
        return ()
    normal = _fit_source(
        document.metadata,
        document.full_text,
        limit=token_limit,
        token_counter=token_counter,
        source_label="DOCUMENT FULL TEXT",
    )
    if normal[1] is False:
        return (PrefixUnit("unit_1", tuple(chunks), normal[0], "document"),)

    code_path = any(str(chunk.metadata.get("cr_unit")) == "symbol" for chunk in chunks)
    hierarchical = any(
        str(chunk.metadata.get("section_path") or "").strip()
        or str(chunk.metadata.get("cr_parent_group") or "").strip()
        for chunk in chunks
    )

    def symbol_boundary(chunk: IndexChunk) -> str:
        return f"symbol:{chunk.metadata.get('symbol', '')}"

    def section_boundary(chunk: IndexChunk) -> str:
        section = str(chunk.metadata.get("section_path") or "").strip()
        if section:
            return f"section:{section}"
        return f"parent:{chunk.metadata.get('cr_parent_group', '')}"

    def chunk_boundary(chunk: IndexChunk) -> str:
        return f"chunk:{chunk.chunk_id}"

    if code_path:
        grouping = "top-level-symbol"
        boundary = symbol_boundary
        label = "TOP-LEVEL SYMBOL TEXT"
    elif hierarchical:
        grouping = "section-parent-group"
        boundary = section_boundary
        label = "SECTION/PARENT TEXT"
    else:
        grouping = "chunk-boundary-greedy"
        boundary = chunk_boundary
        label = "SOURCE TEXT SEGMENT"
    boundary_is_unit = code_path or hierarchical

    groups: list[tuple[list[IndexChunk], str]] = []
    for chunk in chunks:
        identity = boundary(chunk)
        if groups and groups[-1][1] == identity:
            groups[-1][0].append(chunk)
        else:
            groups.append(([chunk], identity))

    units: list[PrefixUnit] = []
    pending: list[IndexChunk] = []

    def flush() -> None:
        nonlocal pending
        if not pending:
            return
        source = "\n\n".join(chunk.text for chunk in pending)
        prefix, truncated = _fit_source(
            document.metadata,
            source,
            limit=token_limit,
            token_counter=token_counter,
            source_label=label,
        )
        units.append(
            PrefixUnit(
                f"unit_{len(units) + 1}",
                tuple(pending),
                prefix,
                grouping,
                truncated,
            )
        )
        pending = []

    def fits(items: Sequence[IndexChunk]) -> bool:
        source = "\n\n".join(item.text for item in items)
        return token_counter(stable_prefix(document.metadata, source, source_label=label)) <= (
            token_limit
        )

    for group_chunks, _identity in groups:
        if boundary_is_unit:
            if fits(group_chunks):
                pending.extend(group_chunks)
                flush()
                continue
            pending.clear()
            for chunk in group_chunks:
                pending.append(chunk)
                if not fits(pending) and len(pending) > 1:
                    pending.pop()
                    flush()
                    pending.append(chunk)
            flush()
            continue
        pending.extend(group_chunks)
        if not fits(pending):
            if len(pending) > len(group_chunks):
                pending = pending[: -len(group_chunks)]
                flush()
                pending.extend(group_chunks)
            elif len(pending) > 1:
                pending.pop()
                flush()
                pending.append(group_chunks[-1])
    flush()
    return tuple(units)


class ContextualRetrievalService:
    """Run CR per leaf chunk while warming one deterministic prefix per unit."""

    def __init__(
        self,
        *,
        provider: ContextualRetrievalProvider,
        cache: PrefixCacheManager | None = None,
        token_counter: Callable[[str], int] = approximate_token_count,
        concurrency: int = 4,
        max_attempts: int = CONTEXTUAL_PROVIDER_ATTEMPTS,
        token_limit: int = CONTEXTUAL_PREFIX_TOKEN_LIMIT,
        prompt_schema_version: str = CONTEXTUAL_PROMPT_SCHEMA_VERSION,
        tokenization_config_version: str = CONTEXTUAL_TOKENIZATION_VERSION,
        now: Callable[[], datetime] | None = None,
        usage_sink: Callable[[ContextualUsageFact], None] | None = None,
    ) -> None:
        if concurrency < 1 or max_attempts < 1 or token_limit < 1:
            raise ValueError("contextual retrieval settings are invalid")
        self._provider = provider
        self._cache = cache
        self._token_counter = token_counter
        self._concurrency = concurrency
        self._max_attempts = max_attempts
        self._token_limit = token_limit
        self._prompt_schema_version = prompt_schema_version
        self._tokenization_config_version = tokenization_config_version
        self._now = now or (lambda: datetime.now(UTC))
        self._usage_sink = usage_sink
        self._usage_facts: list[ContextualUsageFact] = []
        self._usage_lock = threading.Lock()

    def enhance(
        self, document: ContextualDocument, chunks: Sequence[IndexChunk]
    ) -> ContextualEnhancementOutput:
        targets = tuple(chunk for chunk in chunks if contextual_target(chunk))
        if not targets:
            return ContextualEnhancementOutput({}, {}, (), ())
        units = plan_prefix_units(
            document,
            targets,
            token_counter=self._token_counter,
            token_limit=self._token_limit,
        )
        contexts: dict[str, str] = {}
        chunk_results: dict[str, ContextualChunkResult] = {}
        unit_results: list[ContextualUnitResult] = []
        degradations: list[Mapping[str, Any]] = []
        ordinals = {chunk.chunk_id: index + 1 for index, chunk in enumerate(chunks)}
        for unit in units:
            result = self._run_unit(document, unit, ordinals)
            self._flush_usage()
            unit_results.append(result)
            for item in result.chunks:
                chunk_results[item.chunk_id] = item
                if item.context is not None:
                    contexts[item.chunk_id] = item.context
                else:
                    degradations.append(
                        {
                            "kind": "contextual_retrieval_degraded",
                            "reason": item.degraded_reason,
                            "chunk_id": item.chunk_id,
                            "provider": item.provider,
                        }
                    )
        return ContextualEnhancementOutput(
            contexts,
            chunk_results,
            tuple(unit_results),
            tuple(degradations),
        )

    def _cache_key(self, document: ContextualDocument, unit: PrefixUnit) -> PrefixCacheKey:
        return PrefixCacheKey(
            instance_id=document.instance_id,
            space_id=document.space_id,
            document_id=document.document_id,
            document_version_id=document.document_version_id,
            unit_id=unit.unit_id,
            model_revision=self._provider.model_revision,
            prompt_schema_version=self._prompt_schema_version,
            metadata_version=normalized_metadata(document.metadata),
            tokenization_config_version=self._tokenization_config_version,
        )

    def _run_unit(
        self, document: ContextualDocument, unit: PrefixUnit, ordinals: Mapping[str, int]
    ) -> ContextualUnitResult:
        cache_mode = "call"
        cache_outage = False
        cache_reason = "cache-disabled"
        key = self._cache_key(document, unit)
        if self._cache is not None:
            decision = self._cache.begin_prefix(
                key,
                prefix=unit.prefix,
                generation_id=document.generation_id,
            )
            cache_mode = decision.mode
            cache_outage = decision.outage
            cache_reason = decision.reason

        completed: list[ContextualChunkResult] = []
        warmup_ids: list[str] = []
        concurrent_ids: list[str] = []
        provider_failed = False
        warmup_count = min(CONTEXTUAL_WARMUP_CALLS, len(unit.chunks))
        for chunk in unit.chunks[:warmup_count]:
            result = self._call_chunk(unit, chunk, ordinals[chunk.chunk_id], cache_mode, True)
            completed.append(result)
            warmup_ids.append(chunk.chunk_id)
            if result.context is None:
                provider_failed = True
                break

        remaining = (
            unit.chunks[warmup_count:] if not provider_failed else unit.chunks[len(completed) :]
        )
        warmups_completed = (
            not provider_failed
            and warmup_count == CONTEXTUAL_WARMUP_CALLS
            and len(completed) == warmup_count
        )
        if self._cache is not None and warmups_completed and cache_mode != "no-cache":
            self._cache.complete_prefix(
                key,
                prefix=unit.prefix,
                generation_id=document.generation_id,
                now_utc=self._now(),
            )
        if not provider_failed and remaining:
            with ThreadPoolExecutor(max_workers=min(self._concurrency, len(remaining))) as pool:
                futures = [
                    pool.submit(
                        self._call_chunk,
                        unit,
                        chunk,
                        ordinals[chunk.chunk_id],
                        cache_mode,
                        False,
                    )
                    for chunk in remaining
                ]
                completed.extend(future.result() for future in futures)
            concurrent_ids.extend(chunk.chunk_id for chunk in remaining)
        else:
            completed.extend(self._fallback_result(chunk, cache_mode) for chunk in remaining)

        ordered = tuple(
            next(item for item in completed if item.chunk_id == chunk.chunk_id)
            for chunk in unit.chunks
        )
        return ContextualUnitResult(
            unit.unit_id,
            unit.grouping,
            ordered,
            cache_mode,
            cache_outage,
            cache_reason,
            unit.prefix_truncated,
            self._token_counter(unit.prefix),
            tuple(warmup_ids),
            tuple(concurrent_ids),
            self._usage_hit(ordered),
            self._usage_miss(ordered),
        )

    @staticmethod
    def _usage_hit(
        results: Sequence[ContextualChunkResult] | Mapping[str, ContextualChunkResult],
    ) -> int:
        values = results.values() if isinstance(results, Mapping) else results
        return sum(int(getattr(item, "prompt_cache_hit_tokens", 0) or 0) for item in values)

    @staticmethod
    def _usage_miss(
        results: Sequence[ContextualChunkResult] | Mapping[str, ContextualChunkResult],
    ) -> int:
        values = results.values() if isinstance(results, Mapping) else results
        return sum(int(getattr(item, "prompt_cache_miss_tokens", 0) or 0) for item in values)

    def _call_chunk(
        self,
        unit: PrefixUnit,
        chunk: IndexChunk,
        ordinal: int,
        cache_mode: str,
        warmup: bool,
    ) -> ContextualChunkResult:
        prompt = compose_prompt(unit.prefix, dynamic_suffix(ordinal, chunk))
        fingerprint = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        attempts = 0
        reason: str | None = None
        while attempts < self._max_attempts:
            attempts += 1
            try:
                generation = self._provider.generate(
                    prompt=prompt,
                    chunk_id=chunk.chunk_id,
                    warmup=warmup,
                )
                context = generation.context.strip()
                if not context:
                    raise ContextualProviderRejected("empty contextual response")
                fact = ContextualUsageFact(
                    provider=generation.provider,
                    model=generation.model,
                    operation="contextual_retrieval",
                    chunk_id=chunk.chunk_id,
                    unit_id=unit.unit_id,
                    attempt=attempts,
                    result="succeeded",
                    request_fingerprint=fingerprint,
                    cache_mode=cache_mode,
                    warmup=warmup,
                    input_tokens=generation.input_tokens,
                    prompt_cache_hit_tokens=generation.prompt_cache_hit_tokens,
                    prompt_cache_miss_tokens=generation.prompt_cache_miss_tokens,
                    output_tokens=generation.output_tokens,
                    provider_request_id=generation.provider_request_id,
                )
                self._record_usage(fact)
                return ContextualChunkResult(
                    chunk.chunk_id,
                    context,
                    generation.provider,
                    generation.model,
                    cache_mode,
                    attempts,
                    input_tokens=generation.input_tokens,
                    prompt_cache_hit_tokens=generation.prompt_cache_hit_tokens,
                    prompt_cache_miss_tokens=generation.prompt_cache_miss_tokens,
                    output_tokens=generation.output_tokens,
                )
            except ContextualProviderUnavailable as exc:
                reason = "provider_unavailable"
                self._record_usage(
                    ContextualUsageFact(
                        provider=self._provider.provider,
                        model=self._provider.model,
                        operation="contextual_retrieval",
                        chunk_id=chunk.chunk_id,
                        unit_id=unit.unit_id,
                        attempt=attempts,
                        result="failed",
                        request_fingerprint=fingerprint,
                        cache_mode=cache_mode,
                        warmup=warmup,
                    )
                )
                del exc
            except ContextualProviderRejected as exc:
                reason = "provider_rejected"
                self._record_usage(
                    ContextualUsageFact(
                        provider=self._provider.provider,
                        model=self._provider.model,
                        operation="contextual_retrieval",
                        chunk_id=chunk.chunk_id,
                        unit_id=unit.unit_id,
                        attempt=attempts,
                        result="failed",
                        request_fingerprint=fingerprint,
                        cache_mode=cache_mode,
                        warmup=warmup,
                    )
                )
                del exc
                break
        fallback = self._fallback_result(chunk, cache_mode)
        return ContextualChunkResult(
            fallback.chunk_id,
            None,
            self._provider.provider,
            self._provider.model,
            cache_mode,
            attempts,
            reason or "provider_unavailable",
        )

    def _fallback_result(self, chunk: IndexChunk, cache_mode: str) -> ContextualChunkResult:
        return ContextualChunkResult(
            chunk.chunk_id,
            None,
            self._provider.provider,
            self._provider.model,
            cache_mode,
            0,
            "provider_unavailable",
        )

    def _record_usage(self, fact: ContextualUsageFact) -> None:
        with self._usage_lock:
            self._usage_facts.append(fact)

    def _flush_usage(self) -> None:
        with self._usage_lock:
            facts = tuple(self._usage_facts)
            self._usage_facts.clear()
        if self._usage_sink is None:
            return
        for fact in facts:
            self._usage_sink(fact)


__all__ = [
    "CONTEXTUAL_MODEL",
    "CONTEXTUAL_OUTPUT_INSTRUCTION",
    "CONTEXTUAL_PREFIX_TOKEN_LIMIT",
    "CONTEXTUAL_PROMPT_SCHEMA_VERSION",
    "CONTEXTUAL_PROVIDER_ATTEMPTS",
    "CONTEXTUAL_SYSTEM_INSTRUCTION",
    "CONTEXTUAL_TOKENIZATION_VERSION",
    "CONTEXTUAL_WARMUP_CALLS",
    "ContextualChunkResult",
    "ContextualDocument",
    "ContextualEnhancementOutput",
    "ContextualGeneration",
    "ContextualProviderRejected",
    "ContextualProviderUnavailable",
    "ContextualRetrievalProvider",
    "ContextualRetrievalService",
    "ContextualUnitResult",
    "ContextualUsageFact",
    "PrefixUnit",
    "approximate_token_count",
    "compose_prompt",
    "contextual_target",
    "dynamic_suffix",
    "plan_prefix_units",
    "stable_prefix",
]
