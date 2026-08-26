"""ingestion-cost-rules：入库可计费处理量折算与配置类 CR 分流契约。"""

from __future__ import annotations

import json
from typing import Any

from app.documents.indexing import IndexStagingRequest
from app.documents.service import _metered_pages
from app.indexing import ContentProcessor
from app.indexing.contextual import (
    CONTEXTUAL_MODEL,
    ContextualGeneration,
    approximate_token_count,
)


def _request() -> IndexStagingRequest:
    return IndexStagingRequest(
        job_id="job_1",
        attempt_id="attempt_1",
        fencing_token=1,
        publication_id="publication_1",
        document_id="document_1",
        document_version_id="version_1",
        space_id="space_1",
        operation="initial",
        base_active_version_id=None,
        expected_generation_id="generation_initial",
        index_revision_at_start=0,
        object_manifest_ref="manifest_1",
        processing_config_snapshot={},
        authorization_fence={"actor_id": "user_1"},
        input_manifest_hash="hash_1",
        processing_profile_version="profile_1",
    )


class RecordingProvider:
    provider = "fake"
    model = CONTEXTUAL_MODEL
    model_revision = CONTEXTUAL_MODEL

    def __init__(self) -> None:
        self.calls: list[str] = []

    def generate(self, *, prompt: str, chunk_id: str, warmup: bool) -> ContextualGeneration:
        self.calls.append(chunk_id)
        return ContextualGeneration(
            context=f"context for {chunk_id}",
            provider=self.provider,
            model=self.model,
        )


def _text_with_tokens(target: int) -> str:
    # "a " 折算为 2 token（1 词 + 1 空格字符），可精确构造目标 token 数。
    text = "a " * (target // 2) + ("a" if target % 2 else "")
    assert approximate_token_count(text) == target
    return text


# --- A1/A2/A3/A4: _metered_pages 折算规则 ---


def test_metered_pages_code_folds_tokens_at_500_per_page() -> None:
    assert (
        _metered_pages(
            page_count=0,
            image_count=0,
            summary={"metering": {"class": "code", "token_count": 1000}},
        )
        == 2
    )
    # 非配置类代码文件不足 500 token 仍按 1 页计。
    assert (
        _metered_pages(
            page_count=0, image_count=0, summary={"metering": {"class": "code", "token_count": 300}}
        )
        == 1
    )


def test_metered_pages_structured_table_route_is_free() -> None:
    assert (
        _metered_pages(page_count=1, image_count=0, summary={"metering": {"class": "table"}}) == 0
    )


def test_metered_pages_config_threshold_at_500_tokens() -> None:
    assert (
        _metered_pages(
            page_count=0,
            image_count=0,
            summary={"metering": {"class": "config", "token_count": 400}},
        )
        == 0
    )
    assert (
        _metered_pages(
            page_count=0,
            image_count=0,
            summary={"metering": {"class": "config", "token_count": 700}},
        )
        == 2
    )


def test_metered_pages_internvl_image_is_free_other_providers_charge() -> None:
    assert (
        _metered_pages(
            page_count=0,
            image_count=1,
            summary={"metering": {"class": "image", "image_provider": "internvl"}},
        )
        == 0
    )
    assert (
        _metered_pages(
            page_count=0,
            image_count=1,
            summary={"metering": {"class": "image", "image_provider": "qwen-vl"}},
        )
        == 1
    )


def test_metered_pages_document_carriers_unchanged() -> None:
    assert _metered_pages(page_count=2, image_count=2) == 4
    assert (
        _metered_pages(page_count=2, image_count=2, summary={"metering": {"class": "document"}})
        == 4
    )


# --- A1/A2/A3: process() 产出的 metering 事实 ---


def _summary(processor: ContentProcessor, *, media_kind: str, content: bytes | str) -> dict:
    output = processor.process(
        _request(),
        content,
        media_kind=media_kind,
        content_manifest_id="manifest_1",
        content_manifest_hash="hash_1",
    )
    return dict(output.receipt.processing_summary)


def test_code_file_meters_tokens() -> None:
    summary = _summary(
        ContentProcessor(),
        media_kind="text/x-python",
        content="x = 1\n" + _text_with_tokens(1000),
    )
    assert summary["metering"]["class"] == "code"
    assert summary["metering"]["token_count"] >= 1000
    assert _metered_pages(page_count=0, image_count=0, summary=summary) == -(
        -summary["metering"]["token_count"] // 500
    )


def test_csv_and_structured_table_routes_meter_zero_pages() -> None:
    processor = ContentProcessor()
    csv_summary = _summary(processor, media_kind="text/csv", content="name,value\na,1\n")
    assert csv_summary["metering"] == {"class": "table"}
    assert _metered_pages(page_count=0, image_count=0, summary=csv_summary) == 0

    rows = json.dumps([{"name": f"item{i}", "value": i} for i in range(3)])
    json_summary = _summary(processor, media_kind="application/json", content=rows)
    assert json_summary["metering"] == {"class": "table"}
    assert _metered_pages(page_count=0, image_count=0, summary=json_summary) == 0


def test_internvl_image_meters_zero_other_image_meters_one() -> None:
    for provider, expected in (("internvl", 0), ("qwen-vl", 1)):
        processor = ContentProcessor(
            image_describer=lambda content, context, provider=provider: {
                "text": f"description by {provider}",
                "provider": provider,
                "degraded": False,
                "reason": "ok",
            }
        )
        summary = _summary(processor, media_kind="image/png", content=b"image-bytes")
        assert summary["metering"] == {"class": "image", "image_provider": provider}
        assert _metered_pages(page_count=0, image_count=1, summary=summary) == expected


# --- A4/A13/A14: 配置类文件按 token 大小分流 CR ---


def _config_text(tokens: int) -> str:
    return json.dumps({"settings": _text_with_tokens(tokens)})


def test_small_config_file_skips_contextual_retrieval() -> None:
    provider = RecordingProvider()
    processor = ContentProcessor(contextual_provider=provider)
    output = processor.process(
        _request(),
        _config_text(400),
        media_kind="application/json",
        content_manifest_id="manifest_1",
        content_manifest_hash="hash_1",
    )
    summary = output.receipt.processing_summary
    assert summary["metering"]["class"] == "config"
    assert summary["metering"]["token_count"] >= 400
    assert summary["contextual_input_ready"] is False
    assert "cr" not in summary or summary["cr"] == {"applied": False, "unit": "key_path"}
    assert provider.calls == []
    for chunk in output.chunks:
        assert "CONTEXTUAL RETRIEVAL" not in chunk.embedding_text
        assert "contextual_retrieval" not in chunk.metadata
    assert _metered_pages(page_count=0, image_count=0, summary=summary) == 0


def test_large_config_file_runs_contextual_retrieval_and_meters_two_pages() -> None:
    provider = RecordingProvider()
    processor = ContentProcessor(contextual_provider=provider)
    output = processor.process(
        _request(),
        _config_text(700),
        media_kind="application/json",
        content_manifest_id="manifest_1",
        content_manifest_hash="hash_1",
    )
    summary = output.receipt.processing_summary
    assert summary["metering"]["class"] == "config"
    assert summary["metering"]["token_count"] >= 700
    assert summary["cr"]["applied"] is True
    assert provider.calls
    assert _metered_pages(page_count=0, image_count=0, summary=summary) == 2


def test_schema_like_config_small_file_skips_cr() -> None:
    provider = RecordingProvider()
    processor = ContentProcessor(contextual_provider=provider)
    output = processor.process(
        _request(),
        json.dumps({"type": "object", "title": _text_with_tokens(200)}),
        media_kind="application/json",
        content_manifest_id="manifest_1",
        content_manifest_hash="hash_1",
    )
    summary = output.receipt.processing_summary
    assert summary["metering"] == {
        "class": "config",
        "token_count": summary["metering"]["token_count"],
    }
    assert summary["contextual_input_ready"] is False
    assert provider.calls == []
    assert _metered_pages(page_count=0, image_count=0, summary=summary) == 0


# --- A2/A3: 折算结果进入 quota_debit ---


def _quota_service(recorded: list[dict[str, Any]]) -> Any:
    class _Calendar:
        def lock_or_verify(self, connection):
            del connection
            return object()

    class _Quota:
        calendar = _Calendar()

        def record(self, connection, **values) -> str:
            del connection
            recorded.append(values)
            return "debit_1"

    return _Quota()


def test_publication_quota_debit_uses_metering_class() -> None:
    from datetime import UTC, datetime

    from app.documents.service import DocumentsService

    for summary, expected_pages in (
        ({"metering": {"class": "table"}, "page_count": 1, "image_count": 0}, 0),
        (
            {"metering": {"class": "image", "image_provider": "internvl"}, "image_count": 1},
            0,
        ),
        ({"metering": {"class": "code", "token_count": 1000}}, 2),
        ({"metering": {"class": "config", "token_count": 400}}, 0),
    ):
        recorded: list[dict[str, Any]] = []
        service = DocumentsService.__new__(DocumentsService)
        service._quota_service = _quota_service(recorded)
        result = service._record_publication_quota(
            None,
            job={
                "id": "job_1",
                "created_by_user_id": "user_1",
                "quota_role_snapshot": "user",
                "quota_department_id_snapshot": None,
                "quota_exempt_reason": None,
                "replay_generation": 0,
            },
            publication={"id": "publication_1"},
            document={"space_id": "space_1"},
            receipt={
                "processing_summary": {
                    **summary,
                    "processing_list": {
                        "processing_list_id": "processing_list_1",
                        "frozen": True,
                    },
                },
            },
            published_at=datetime(2026, 8, 26, tzinfo=UTC),
        )
        assert result["pages"] == expected_pages
        assert recorded[-1]["pages"] == expected_pages
