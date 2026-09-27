"""fix-heading-misjudgment：文本路径标题判定的行为契约（无大小写信息行、混排全大写行、编号行）。"""

from __future__ import annotations

from app.documents.indexing import IndexStagingRequest
from app.indexing import ContentProcessor


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
        input_manifest_hash="manifest_hash_1",
        processing_profile_version="profile_1",
    )


def _chunk_text(text: str, *, media_kind: str = "text/plain") -> tuple[list, dict]:
    processor = ContentProcessor()
    chunks, summary, _ = processor._text_chunks(_request(), text, media_kind, "manifest_hash_1")
    return chunks, summary


# --- 子修 A: 无大小写信息行不作标题 ---


def test_cjk_short_line_stays_in_chunk_body_and_embedding() -> None:
    caption = "检索系统概述"
    long_paragraph = (
        "本段详细描述检索系统的整体架构，包括查询理解、多路召回、融合排序与重排等核心阶段"
        "的详细设计说明，以及各阶段之间的数据流转关系、性能预算分配和失败降级路径的完整约定，"
        "用于支撑后续章节的实现细节描述。"
    )
    chunks, summary = _chunk_text(f"{caption}\n\n{long_paragraph}")

    assert summary["tree"]["tree_indexed"] is False
    assert all(not chunk.metadata["section_path"] for chunk in chunks)
    assert any(caption in chunk.text for chunk in chunks)
    assert any(caption in chunk.embedding_text for chunk in chunks)


def test_digit_only_line_stays_in_chunk_body() -> None:
    long_paragraph = (
        "本段详细描述检索系统的整体架构，包括查询理解、多路召回、融合排序与重排等核心阶段"
        "的详细设计说明，以及各阶段之间的数据流转关系、性能预算分配和失败降级路径的完整约定，"
        "用于支撑后续章节的实现细节描述。"
    )
    chunks, _ = _chunk_text(f"2024\n\n{long_paragraph}")

    assert any("2024" in chunk.text for chunk in chunks)
    assert all(not chunk.metadata["section_path"] for chunk in chunks)


def test_all_cjk_short_lines_fall_back_with_content_intact() -> None:
    lines = [f"这是第{index}段内容，用于验证全短行文档的回退行为" for index in range(1, 6)]
    chunks, summary = _chunk_text("\n\n".join(lines))

    joined = "\n\n".join(chunk.text for chunk in chunks)
    for line in lines:
        assert line in joined
    assert summary["tree"]["tree_indexed"] is False


def test_title_case_english_line_still_a_heading() -> None:
    chunks, summary = _chunk_text(
        "The Quick Brown Fox\n\nbody paragraph follows here in english prose."
    )

    assert summary["tree"]["tree_indexed"] is True
    assert chunks[0].metadata["section_path"] == "The Quick Brown Fox"
    assert "The Quick Brown Fox" not in chunks[0].text


# --- 子修 B: 混排全大写行不作标题 ---


def test_all_caps_latin_heading_still_detected() -> None:
    chunks, summary = _chunk_text("INTRODUCTION\n\nbody paragraph follows here in english prose.")

    assert summary["tree"]["tree_indexed"] is True
    assert chunks[0].metadata["section_path"] == "INTRODUCTION"
    assert "INTRODUCTION" not in chunks[0].text


def test_upper_latin_with_cjk_not_heading() -> None:
    caption = "RAG系统"
    long_paragraph = (
        "本段详细描述检索系统的整体架构，包括查询理解、多路召回、融合排序与重排等核心阶段"
        "的详细设计说明，以及各阶段之间的数据流转关系、性能预算分配和失败降级路径的完整约定，"
        "用于支撑后续章节的实现细节描述。"
    )
    chunks, summary = _chunk_text(f"{caption}\n\n{long_paragraph}")

    assert summary["tree"]["tree_indexed"] is False
    assert all(not chunk.metadata["section_path"] for chunk in chunks)
    assert any(caption in chunk.text for chunk in chunks)
    assert any(caption in chunk.embedding_text for chunk in chunks)
