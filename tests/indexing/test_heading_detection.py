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


# --- 子修 C: 编号行需延续标题序列且独占段落 ---


def _cjk_body(seed: str) -> str:
    return (
        f"{seed}本段详细描述检索系统的整体架构，包括查询理解、多路召回、融合排序与重排等"
        "核心阶段的详细设计说明，以及各阶段之间的数据流转关系、性能预算分配和失败降级路径"
        "的完整约定，用于支撑后续章节的实现细节描述。"
    )


def test_consecutive_numbered_section_headings_detected() -> None:
    text = "\n\n".join(
        [
            "1. 总体方案",
            _cjk_body(""),
            "2. 详细设计",
            _cjk_body(""),
        ]
    )
    chunks, summary = _chunk_text(text)

    assert summary["tree"]["tree_indexed"] is True
    paths = {chunk.metadata["section_path"] for chunk in chunks}
    assert paths == {"总体方案", "详细设计"}
    assert all("1. 总体方案" not in chunk.text for chunk in chunks)
    assert all("2. 详细设计" not in chunk.text for chunk in chunks)


def test_adjacent_numbered_steps_not_headings() -> None:
    steps = (
        "1. 打开配置面板并进入检索参数设置页面\n"
        "2. 调整召回数量与融合权重后保存生效\n"
        "3. 在预览页验证候选列表是否符合预期"
    )
    text = "\n\n".join(["# 配置说明", _cjk_body(""), steps, _cjk_body("收尾。")])
    chunks, _ = _chunk_text(text)

    paths = " | ".join(chunk.metadata["section_path"] for chunk in chunks)
    assert "配置说明" in paths
    for line in steps.splitlines():
        assert any(line in chunk.text for chunk in chunks)
        assert any(line in chunk.embedding_text for chunk in chunks)
        assert line not in paths


def test_out_of_order_first_number_not_heading() -> None:
    text = "\n\n".join(["3. 系统设计", _cjk_body("")])
    chunks, summary = _chunk_text(text)

    assert summary["tree"]["tree_indexed"] is False
    assert any("3. 系统设计" in chunk.text for chunk in chunks)
    assert all(not chunk.metadata["section_path"] for chunk in chunks)


def test_restarted_number_not_heading() -> None:
    text = "\n\n".join(
        [
            "1. 总体方案",
            _cjk_body(""),
            "1. 又一个总体方案",
            _cjk_body(""),
        ]
    )
    chunks, _ = _chunk_text(text)

    assert any("1. 又一个总体方案" in chunk.text for chunk in chunks)
    assert all("1. 又一个总体方案" not in chunk.metadata["section_path"] for chunk in chunks)


def test_multilevel_numbered_headings_progress() -> None:
    text = "\n\n".join(
        [
            "1. 总体方案",
            _cjk_body(""),
            "1.1 架构",
            _cjk_body(""),
            "1.2 部署",
            _cjk_body(""),
            "2. 详细设计",
            _cjk_body(""),
        ]
    )
    chunks, summary = _chunk_text(text)

    assert summary["tree"]["tree_indexed"] is True
    paths = " | ".join(chunk.metadata["section_path"] for chunk in chunks)
    for expected in ("总体方案", "总体方案 / 架构", "总体方案 / 部署", "详细设计"):
        assert expected in paths
    assert all("1.1 架构" not in chunk.text for chunk in chunks)
    assert all("2. 详细设计" not in chunk.text for chunk in chunks)


def test_markdown_hash_lines_without_heading_shape_stay_body() -> None:
    body_lines = ["#hashtag", "#include <stdio.h>", "#!/bin/bash", "####### deep"]
    text = "\n\n".join(["# 真标题", _cjk_body(""), *body_lines])
    chunks, _ = _chunk_text(text)

    paths = " | ".join(chunk.metadata["section_path"] for chunk in chunks)
    assert paths == "真标题"
    joined = "\n".join(chunk.text for chunk in chunks)
    for line in body_lines:
        assert line in joined


def test_rejected_numbered_line_does_not_advance_outline() -> None:
    text = "\n\n".join(
        [
            "1. 总体方案",
            _cjk_body(""),
            "1. 重复编号",
            _cjk_body(""),
            "2. 详细设计",
            _cjk_body(""),
        ]
    )
    chunks, _ = _chunk_text(text)

    assert any("1. 重复编号" in chunk.text for chunk in chunks)
    paths = {chunk.metadata["section_path"] for chunk in chunks}
    assert "详细设计" in paths
    assert all("1. 重复编号" not in path for path in paths)


def test_isolated_numbered_line_at_text_end_judged() -> None:
    text = "\n\n".join(["1. 总体方案", _cjk_body(""), "1. 尾部重复编号"])
    chunks, _ = _chunk_text(text)

    assert any("1. 尾部重复编号" in chunk.text for chunk in chunks)
    assert all("1. 尾部重复编号" not in chunk.metadata["section_path"] for chunk in chunks)


# --- 端到端形态：MinerU 风格混合文档 ---


def test_mineru_style_mixed_document_keeps_all_content_blocks() -> None:
    """版面模型只把章节标题标成 ``#``；题注、操作步骤、短段落是裸文本，
    全部内容块必须留在 chunk 正文与稠密向量文本中。"""

    long_paragraph = (
        "本章描述检索系统的整体架构与各子系统的职责划分，包括查询理解、多路召回、融合排序"
        "与重排等核心阶段的详细设计说明，以及各阶段之间的数据流转关系、性能预算分配和失败"
        "降级路径的完整约定，用于支撑后续章节的实现细节描述。"
    )
    medium_paragraph = (
        "系统采用双路召回架构，稠密通路负责语义匹配，稀疏通路负责关键词精确命中，融合层按"
        "权重合并候选并做去重处理，最终输出带证据片段的有序候选列表。"
    )
    caption = "图 3-2 混合检索架构图"
    steps = "1. 打开配置面板并进入检索参数设置页面\n" "2. 调整召回数量与融合权重后保存生效"
    closing = "交叉编码器对候选做精排，截断时保留每个文档至少一条证据片段。"
    markdown = "\n\n".join(
        [
            "# 检索系统设计方案",
            long_paragraph,
            caption,
            medium_paragraph,
            steps,
            "## 3.1 重排策略",
            closing,
        ]
    )

    chunks, summary = _chunk_text(markdown, media_kind="application/pdf")

    assert summary["tree"]["tree_indexed"] is True
    joined_text = "\n".join(chunk.text for chunk in chunks)
    joined_embedding = "\n".join(chunk.embedding_text for chunk in chunks)
    for block in (long_paragraph, caption, medium_paragraph, closing):
        assert block in joined_text
        assert block in joined_embedding
    for line in steps.splitlines():
        assert line in joined_text
        assert line in joined_embedding
    joined_paths = " | ".join(chunk.metadata["section_path"] for chunk in chunks)
    assert "检索系统设计方案" in joined_paths
    assert "检索系统设计方案 / 3.1 重排策略" in joined_paths
    assert caption not in joined_paths
    assert "1. 打开配置面板并进入检索参数设置页面" not in joined_paths
