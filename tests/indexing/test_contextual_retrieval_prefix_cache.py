from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from app.documents.indexing import IndexStagingRequest
from app.indexing.contextual import (
    CONTEXTUAL_MODEL,
    CONTEXTUAL_PROMPT_SCHEMA_VERSION,
    ContextualDocument,
    ContextualGeneration,
    ContextualProviderUnavailable,
    ContextualRetrievalService,
    contextual_target,
    plan_prefix_units,
)
from app.indexing.contextual_provider import DashScopeContextualRetriever
from app.indexing.generation import GenerationManager
from app.indexing.mineru import (
    OCRSamplePlan,
    _sample_ranges,
    classify_structure,
    signal_confidences,
    structure_signals,
)
from app.indexing.models import IndexChunk
from app.indexing.prefix_cache import (
    InMemoryPrefixCacheStore,
    PrefixCacheKey,
    PrefixCacheManager,
    PrefixCacheUnavailable,
    prefix_cleanup_eligibility,
)
from app.indexing.processing import ContentProcessor
from app.indexing.service import IndexingService
from app.platform.config import load_platform_settings


class FakeProvider:
    provider = "fake"
    model = CONTEXTUAL_MODEL
    model_revision = CONTEXTUAL_MODEL

    def __init__(self, *, fail_until: int = 0) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_until = fail_until
        self.lock = threading.Lock()
        self.barrier: threading.Barrier | None = None

    def generate(self, *, prompt: str, chunk_id: str, warmup: bool) -> ContextualGeneration:
        with self.lock:
            number = len(self.calls) + 1
            self.calls.append(
                {
                    "prompt": prompt,
                    "chunk_id": chunk_id,
                    "warmup": warmup,
                    "thread": threading.current_thread().ident,
                }
            )
        if self.barrier is not None and not warmup:
            self.barrier.wait(timeout=3)
        if number <= self.fail_until:
            raise ContextualProviderUnavailable("injected unavailable")
        cached = 0 if number == 1 else 80
        return ContextualGeneration(
            context=f"context for {chunk_id}",
            provider=self.provider,
            model=self.model,
            input_tokens=100,
            prompt_cache_hit_tokens=cached,
            prompt_cache_miss_tokens=100 - cached,
            output_tokens=10,
        )


def _request(**overrides: Any) -> IndexStagingRequest:
    values: dict[str, Any] = {
        "job_id": "job_1",
        "attempt_id": "attempt_1",
        "fencing_token": 1,
        "publication_id": "publication_1",
        "document_id": "document_1",
        "document_version_id": "version_1",
        "space_id": "space_1",
        "operation": "initial",
        "base_active_version_id": None,
        "expected_generation_id": "generation_1",
        "index_revision_at_start": 0,
        "object_manifest_ref": "object_1",
        "processing_config_snapshot": {"index_namespace": "instance_1"},
        "authorization_fence": {"actor_id": "user_1"},
        "input_manifest_hash": "hash_1",
        "processing_profile_version": "profile_1",
    }
    values.update(overrides)
    return IndexStagingRequest(**values)


def _chunk(number: int, *, text: str | None = None, **metadata: Any) -> IndexChunk:
    body = text or f"chunk body {number}"
    return IndexChunk(
        chunk_id=f"chunk_{number}",
        generation_id="generation_1",
        publication_id="publication_1",
        document_id="document_1",
        document_version_id="version_1",
        space_id="space_1",
        text=body,
        embedding_text=body,
        locator={},
        snippet=body,
        media_kind="text",
        manifest_hash="hash_1",
        metadata={"cr_unit": "chunk", **metadata},
    )


def _document(full_text: str = "full document text", **metadata: Any) -> ContextualDocument:
    values = {"file_name": "guide.md", "media_kind": "text/markdown", **metadata}
    return ContextualDocument(
        instance_id="instance_1",
        space_id="space_1",
        document_id="document_1",
        document_version_id="version_1",
        generation_id="generation_1",
        metadata=values,
        full_text=full_text,
    )


def test_prompt_is_stable_prefix_then_dynamic_suffix_with_two_serial_warmups() -> None:
    provider = FakeProvider()
    service = ContextualRetrievalService(provider=provider, cache=PrefixCacheManager())
    chunks = [_chunk(index, text=f"body {index}") for index in range(1, 4)]

    output = service.enhance(_document(), chunks)

    assert [call["warmup"] for call in provider.calls] == [True, True, False]
    assert [call["thread"] for call in provider.calls[:2]] == [threading.main_thread().ident] * 2
    prompts = [call["prompt"] for call in provider.calls]
    prefixes = [prompt.split("\n\nCHUNK\n", 1)[0] for prompt in prompts]
    assert len(set(prefixes)) == 1
    assert "DOCUMENT METADATA" in prefixes[0]
    assert "full document text" in prefixes[0]
    assert "chunk body" not in prefixes[0]
    assert "timestamp" not in prefixes[0].casefold()
    assert "CHUNK\nordinal: 1" in prompts[0]
    assert "OUTPUT FORMAT" in prompts[0]
    assert output.units[0].warmup_chunk_ids == ("chunk_1", "chunk_2")
    assert output.units[0].concurrent_chunk_ids == ("chunk_3",)
    assert output.units[0].prompt_cache_hit_tokens == 160
    assert output.units[0].prompt_cache_miss_tokens == 140


def test_remaining_chunks_run_concurrently_only_after_two_successful_warmups() -> None:
    provider = FakeProvider()
    provider.barrier = threading.Barrier(3, timeout=3)
    service = ContextualRetrievalService(provider=provider, concurrency=3)
    chunks = [_chunk(index, text=f"body {index}") for index in range(1, 6)]

    output = service.enhance(_document(), chunks)

    assert output.units[0].warmup_chunk_ids == ("chunk_1", "chunk_2")
    assert output.units[0].concurrent_chunk_ids == ("chunk_3", "chunk_4", "chunk_5")
    assert len(output.contexts) == 5
    concurrent_threads = {call["thread"] for call in provider.calls[2:]}
    assert threading.main_thread().ident not in concurrent_threads
    assert len(concurrent_threads) == 3


def test_provider_short_retry_exhaustion_falls_back_to_raw_chunk_with_one_degradation() -> None:
    provider = FakeProvider(fail_until=5)
    usage: list[Any] = []
    service = ContextualRetrievalService(
        provider=provider,
        usage_sink=usage.append,
    )
    output = service.enhance(_document(), [_chunk(1), _chunk(2)])

    assert output.contexts == {}
    assert [item.attempts for item in output.chunk_results.values()] == [2, 0]
    assert len(usage) == 2
    assert {fact.result for fact in usage} == {"failed"}
    assert output.degradations == (
        {
            "kind": "contextual_retrieval_degraded",
            "reason": "provider_unavailable",
            "chunk_id": "chunk_1",
            "provider": "fake",
        },
        {
            "kind": "contextual_retrieval_degraded",
            "reason": "provider_unavailable",
            "chunk_id": "chunk_2",
            "provider": "fake",
        },
    )


def test_processor_paths_select_retrievable_leaves_and_local_paths_do_not_call_llm() -> None:
    provider = FakeProvider()
    processor = ContentProcessor(contextual_provider=provider)
    text = processor.process(
        _request(),
        "# Section\nfirst\n\n# Section\nsecond",
        media_kind="text/markdown",
        content_manifest_id="manifest_1",
        content_manifest_hash="hash_1",
    )
    assert len(provider.calls) == len(text.chunks)
    assert all("CONTEXTUAL RETRIEVAL" in chunk.embedding_text for chunk in text.chunks)
    assert all("ORIGINAL CHUNK" in chunk.embedding_text for chunk in text.chunks)
    assert text.receipt.processing_summary["cr"]["model"] == CONTEXTUAL_MODEL
    assert text.receipt.processing_summary["cr"]["model_revision"] == CONTEXTUAL_MODEL
    assert text.receipt.processing_summary["cr"]["provider"] == "fake"
    assert text.receipt.model_versions["cr"] == CONTEXTUAL_MODEL
    assert text.receipt.prompt_versions["cr_prefix"] == CONTEXTUAL_PROMPT_SCHEMA_VERSION
    assert text.receipt.processing_summary["tree"]["node_summary_model"] == CONTEXTUAL_MODEL
    assert all(
        item["model"] == CONTEXTUAL_MODEL
        for item in text.receipt.processing_summary["tree"]["node_summaries"]
    )

    provider.calls.clear()
    table = processor.process(
        _request(),
        "name,value\na,1",
        media_kind="text/csv",
        content_manifest_id="manifest_1",
        content_manifest_hash="hash_1",
    )
    assert provider.calls == []
    assert table.receipt.processing_summary["cr"]["applied"] is False


def test_prefix_units_are_deterministic_and_use_expected_degradation_boundaries() -> None:
    sections = [
        _chunk(1, text="alpha " * 20, section_path="Chapter A"),
        _chunk(2, text="beta " * 20, section_path="Chapter A"),
        _chunk(3, text="gamma " * 20, section_path="Chapter B"),
        _chunk(4, text="delta " * 20, section_path="Chapter B"),
    ]
    document = _document("word " * 200)
    units = plan_prefix_units(
        document,
        sections,
        token_counter=lambda value: len(value.split()),
        token_limit=100,
    )
    assert [unit.grouping for unit in units] == [
        "section-parent-group",
        "section-parent-group",
    ]
    assert [unit.chunk_ids if hasattr(unit, "chunk_ids") else unit.chunks for unit in units] == [
        (sections[0], sections[1]),
        (sections[2], sections[3]),
    ]
    assert (
        plan_prefix_units(
            document,
            sections,
            token_counter=lambda value: len(value.split()),
            token_limit=100,
        )
        == units
    )

    symbols = [
        _chunk(1, text="class A: " + "body " * 50, cr_unit="symbol", symbol="A"),
        _chunk(2, text="def b(): " + "body " * 50, cr_unit="symbol", symbol="b"),
    ]
    code_units = plan_prefix_units(
        document,
        symbols,
        token_counter=lambda value: len(value.split()),
        token_limit=80,
    )
    assert [unit.grouping for unit in code_units] == ["top-level-symbol"] * 2


def test_cache_keys_are_scoped_and_only_changed_prefix_entries_invalidate() -> None:
    store = InMemoryPrefixCacheStore()
    manager = PrefixCacheManager(store)
    key = PrefixCacheKey(
        "instance_1",
        "space_1",
        "document_1",
        "version_1",
        "unit_1",
        CONTEXTUAL_MODEL,
        CONTEXTUAL_PROMPT_SCHEMA_VERSION,
        "metadata-v1",
        "tokenizer-v1",
    )
    assert manager.begin_prefix(key, prefix="prefix-a", generation_id="generation_1").mode == "warm"
    manager.complete_prefix(
        key,
        prefix="prefix-a",
        generation_id="generation_1",
        now_utc=datetime(2026, 8, 23, tzinfo=UTC),
    )
    assert manager.begin_prefix(key, prefix="prefix-a", generation_id="generation_2").mode == "warm"
    assert store.load(key, "generation_1") is not None
    assert manager.begin_prefix(key, prefix="prefix-b", generation_id="generation_2").mode == "warm"
    assert store.load(key, "generation_2") is None

    changed_model = PrefixCacheKey(
        key.instance_id,
        key.space_id,
        key.document_id,
        key.document_version_id,
        key.unit_id,
        "deepseek-v4-flash-0731@revision-2",
        key.prompt_schema_version,
        key.metadata_version,
        key.tokenization_config_version,
    )
    manager.complete_prefix(
        changed_model,
        prefix="prefix-b",
        generation_id="generation_2",
        now_utc=datetime(2026, 8, 23, tzinfo=UTC),
    )
    assert store.load(changed_model, "generation_2") is not None


def test_cache_outage_bypasses_cache_without_invalidating_or_blocking_calls() -> None:
    class OutageStore(InMemoryPrefixCacheStore):
        def __init__(self) -> None:
            super().__init__()
            self.loads = 0

        def load(self, key: PrefixCacheKey, generation_id: str):
            self.loads += 1
            raise PrefixCacheUnavailable("cache outage")

    store = OutageStore()
    provider = FakeProvider()
    service = ContextualRetrievalService(provider=provider, cache=PrefixCacheManager(store))
    output = service.enhance(_document(), [_chunk(1), _chunk(2)])

    assert output.units[0].cache_mode == "no-cache"
    assert output.units[0].cache_outage is True
    assert len(provider.calls) == 2
    assert store.loads == 1


def test_generation_rollback_and_lease_prevent_prefix_cleanup_until_window_or_gc() -> None:
    clock = [datetime(2026, 8, 23, tzinfo=UTC)]
    manager = GenerationManager(now=lambda: clock[0], rollback_days=7)
    staging = manager.create_staging([], base_revision=0)
    active = manager.release(staging.generation_id, current_revision=0)
    retired = next(item for item in manager.list_generations() if item.status == "retired")

    eligible, reasons = prefix_cleanup_eligibility(
        retired,
        generation_manager=manager,
        now_utc=clock[0] + timedelta(days=1),
        gc_authorized=True,
    )
    assert not eligible
    assert reasons == ("rollback_candidate",)

    clock[0] += timedelta(days=8)
    eligible, reasons = prefix_cleanup_eligibility(
        retired,
        generation_manager=manager,
        now_utc=clock[0],
    )
    assert not eligible
    assert reasons == ("cleanup_not_authorized",)
    eligible, reasons = prefix_cleanup_eligibility(
        retired,
        generation_manager=manager,
        now_utc=clock[0],
        gc_authorized=True,
    )
    assert eligible and reasons == ()

    manager.set_component_state(
        retired.generation_id,
        "public_graph",
        "ready",
        manifest={"source_manifest_hash": "manifest", "source_head_fence": 1},
    )

    lease = manager.acquire_graph_reader_lease(
        generation_id=retired.generation_id,
        source_head_fence=1,
        manifest_hash="manifest",
        validate_source_head=lambda: True,
    )
    eligible, reasons = prefix_cleanup_eligibility(
        retired,
        generation_manager=manager,
        now_utc=clock[0],
        gc_authorized=True,
    )
    assert not eligible
    assert reasons == ("active_query_lease",)
    manager.release_graph_reader_lease(lease.lease_id)
    assert prefix_cleanup_eligibility(
        manager.get_generation(active.generation_id),
        generation_manager=manager,
        now_utc=clock[0],
    ) == (False, ("generation_active",))


def test_dashscope_transport_reports_cache_tokens_and_fixed_model() -> None:
    captured: list[dict[str, Any]] = []

    def transport(url, payload, headers, options):
        captured.append(
            {
                "url": url,
                "payload": payload,
                "headers": headers,
                "options": options,
            }
        )
        return {
            "id": "provider_call_1",
            "choices": [{"message": {"content": "context"}}],
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 15,
                "prompt_tokens_details": {"cached_tokens": 80},
            },
        }

    provider = DashScopeContextualRetriever(
        base_url="https://dashscope.example/v1",
        api_key="secret",
        transport=transport,
    )
    result = provider.generate(prompt="prompt", chunk_id="chunk_1", warmup=True)
    assert result.context == "context"
    assert result.prompt_cache_hit_tokens == 80
    assert result.prompt_cache_miss_tokens == 40
    assert result.output_tokens == 15
    import json

    payload = json.loads(captured[0]["payload"])
    assert payload["model"] == CONTEXTUAL_MODEL
    assert payload["temperature"] == 0
    assert captured[0]["headers"]["Authorization"] == "Bearer secret"


def test_processing_list_is_frozen_and_only_marks_llm_eligible_chunks() -> None:
    output = ContentProcessor().process(
        _request(),
        "name,value\na,1",
        media_kind="text/csv",
        content_manifest_id="manifest_1",
        content_manifest_hash="hash_1",
    )
    processing_list = output.receipt.processing_summary["processing_list"]
    assert processing_list["processing_list_id"] == ("processing_list:publication_1:attempt_1")
    assert processing_list["frozen"] is True
    assert all(not item["contextual_retrieval"] for item in processing_list["items"])
    assert contextual_target(_chunk(1)) is True


def test_structure_line_confidence_comes_from_middle_json_and_keeps_classification() -> None:
    markdown = "# Chapter One\nbody\n\n1.1 Section\nbody"
    middle = {
        "pdf_info": [
            {
                "blocks": [
                    {"text": "Chapter One", "score": 0.82},
                    {"spans": [{"content": "1.1 Section", "score": 0.96}]},
                ]
            }
        ]
    }
    scores = signal_confidences(markdown, middle)
    result = classify_structure(
        markdown,
        sample_pages=(1,),
        signal_scores=scores,
        confidence_threshold=0.9,
    )
    assert scores == {"chapter one": 0.82, "section": 0.96}
    assert result["class"] != "basic"
    assert result["signal_low_confidence"] is True
    assert result["signal_confidence_available"] is True
    assert result["confidence_threshold"] == 0.9


def test_independent_title_line_is_a_structure_signal() -> None:
    markdown = "lead paragraph\n\n独立标题行\n\nfollowing paragraph"
    signals = structure_signals(markdown)
    assert signals == [{"line": 3, "text": "独立标题行", "kind": "independent_title", "level": 1}]


def test_mineru_sample_ranges_are_zero_based() -> None:
    assert _sample_ranges(OCRSamplePlan(10, (1, 2, 6, 7))) == [(0, 1), (5, 6)]


def test_contextual_provider_settings_parse_from_environment() -> None:
    settings = load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
            "RAG_BUSINESS_TIMEZONE": "Asia/Shanghai",
            "RAG_INDEX_CONTEXTUAL_RETRIEVAL_PROVIDER": "dashscope",
            "RAG_INDEX_CONTEXTUAL_RETRIEVAL_BASE_URL": "https://dashscope.example/v1",
            "RAG_INDEX_CONTEXTUAL_RETRIEVAL_API_KEY": "secret",
            "RAG_INDEX_CONTEXTUAL_RETRIEVAL_CONCURRENCY": "6",
            "RAG_INDEX_CONTEXTUAL_RETRIEVAL_PREFIX_TOKEN_LIMIT": "30000",
            "RAG_INDEX_CONTEXTUAL_PREFIX_CACHE_PROVIDER": "memory",
        }
    )
    assert settings.index.contextual_retrieval_provider == "dashscope"
    assert settings.index.contextual_retrieval_model == CONTEXTUAL_MODEL
    assert settings.index.contextual_retrieval_concurrency == 6
    assert settings.index.contextual_retrieval_prefix_token_limit == 30_000
    assert settings.index.contextual_prefix_cache_provider == "memory"


def test_contextual_provider_usage_records_cache_tokens_for_reconciliation() -> None:
    class UsageSubmission:
        def __init__(self) -> None:
            self.prepared = []
            self.dispatched = []
            self.completed = []

        def prepare_provider_call(self, **values):
            self.prepared.append(values)
            return f"provider_call_{len(self.prepared)}"

        def mark_dispatching(self, provider_call_id, **values):
            self.dispatched.append((provider_call_id, values))
            return True

        def complete_provider_call(self, **values):
            self.completed.append(values)

    submission = UsageSubmission()
    provider = FakeProvider()
    processor = ContentProcessor(
        contextual_provider=provider,
        usage_submission=submission,
        text_chunk_max_chars=5,
    )
    ownership = {
        "actor_user_id": "user_1",
        "actor_role_snapshot": "user",
        "actor_department_id_snapshot": None,
        "quota_subject_user_id": "user_1",
        "cost_center_key": "user:user_1",
        "space_id": "space_1",
        "source_space_ids": ["space_1"],
    }
    output = processor.process(
        _request(
            usage_ownership=ownership,
            usage_deadline_at_utc=datetime(2026, 8, 24, tzinfo=UTC),
        ),
        "# A\none\n\n# B\ntwo",
        media_kind="text/markdown",
        content_manifest_id="manifest_1",
        content_manifest_hash="hash_1",
    )

    assert output.receipt.processing_summary["cr"]["applied"] is True
    assert len(submission.completed) == len(provider.calls)
    assert submission.completed[0]["measurement"].prompt_cache_miss_tokens == 100
    assert submission.completed[-1]["measurement"].prompt_cache_hit_tokens == 80
    assert all(item["result"] == "succeeded" for item in submission.completed)


def test_index_changes_invalidate_affected_prefix_facts_without_gc() -> None:
    store = InMemoryPrefixCacheStore()
    cache = PrefixCacheManager(store)
    service = IndexingService(prefix_cache=cache)
    key = PrefixCacheKey(
        "instance_1",
        "space_1",
        "document_1",
        "version_1",
        "unit_1",
        CONTEXTUAL_MODEL,
        CONTEXTUAL_PROMPT_SCHEMA_VERSION,
        "metadata-v1",
        "tokenizer-v1",
    )
    cache.complete_prefix(
        key,
        prefix="prefix",
        generation_id="generation_1",
        now_utc=datetime(2026, 8, 23, tzinfo=UTC),
    )

    service._cleanup_generation_publication("generation_1", "document_1", "version_1")

    assert store.load(key, "generation_1") is None
