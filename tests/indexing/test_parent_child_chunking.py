"""文本路径父子切块（parent-child-chunking）：两级结构、尺寸不变量、CR 落位与表格保留。"""

from __future__ import annotations

from typing import Any

import pytest

from app.indexing.processing import ContentProcessor, _heading, _sections
from tests.indexing.test_contextual_retrieval_prefix_cache import FakeProvider
from tests.indexing.test_contracts import _request


def _text_chunks(processor: ContentProcessor, text: str):
    """Exercise the basic path explicitly, including documents with section signals."""
    chunks, _summary, _degradations = processor._text_chunks(
        _request(), text, "text/markdown", "manifest_hash_1", structure_class="basic"
    )
    return chunks


def _paragraphs(prefix: str, count: int, size: int) -> list[str]:
    return [f"{prefix}{index}" + "字" * (size - len(f"{prefix}{index}")) for index in range(count)]


# ---------------------------------------------------------------------------
# 两级结构
# ---------------------------------------------------------------------------


def test_processor_defaults_follow_parent_child_sizes() -> None:
    processor = ContentProcessor()

    assert processor._text_chunk_target_chars == 640
    assert processor._text_child_chunk_max_chars == 1600
    assert processor._text_parent_chunk_target_chars == 2560


def test_every_child_carries_addressable_parent_within_its_section() -> None:
    first = "\n\n".join(_paragraphs("甲", 12, 500))
    second = "\n\n".join(_paragraphs("乙", 12, 500))
    text = f"# 第一章\n\n{first}\n\n# 第二章\n\n{second}"

    chunks = _text_chunks(ContentProcessor(), text)

    assert len(chunks) > 2
    parents: dict[str, list[Any]] = {}
    for chunk in chunks:
        parent_id = chunk.metadata["parent_id"]
        assert parent_id
        assert chunk.metadata["cr_parent_group"] == parent_id
        assert chunk.text in chunk.metadata["parent_text"]
        parents.setdefault(parent_id, []).append(chunk)
    assert len(parents) > 2
    for siblings in parents.values():
        # 父块不跨章节：同一父块的全部子块取自同一 section，且共享同一份父块正文。
        assert len({sibling.metadata["section_path"] for sibling in siblings}) == 1
        assert len({sibling.metadata["parent_text"] for sibling in siblings}) == 1
        assert len(siblings[0].metadata["parent_text"]) <= 2560
    assert any(len(siblings) > 1 for siblings in parents.values())


def test_parent_ids_are_unique_across_sections() -> None:
    text = "# 甲\n\n" + "短段" * 10 + "\n\n# 乙\n\n" + "短段" * 10

    chunks = _text_chunks(ContentProcessor(), text)

    assert [chunk.metadata["parent_id"] for chunk in chunks] == ["parent_1", "parent_2"]


def test_children_never_exceed_child_maximum_even_for_one_long_paragraph() -> None:
    chunks = _text_chunks(ContentProcessor(), "长" * 5000)

    assert len(chunks) == 4
    assert all(len(chunk.text) <= 1600 for chunk in chunks)
    assert "".join(chunk.text for chunk in chunks) == "长" * 5000


@pytest.mark.parametrize("paragraph_sizes", [(900, 900), (400, 400, 400, 400), (5000, 900, 900)])
def test_child_target_above_maximum_still_keeps_each_child_under_maximum(
    paragraph_sizes: tuple[int, ...],
) -> None:
    processor = ContentProcessor(
        text_chunk_target_chars=2000,
        text_child_chunk_max_chars=1600,
    )
    paragraphs = [chr(0x4E00 + index) * size for index, size in enumerate(paragraph_sizes)]
    text = "\n\n".join(paragraphs)

    chunks = _text_chunks(processor, text)

    assert all(len(chunk.text) <= 1600 for chunk in chunks)
    assert "".join(chunk.text.replace("\n\n", "") for chunk in chunks) == "".join(paragraphs)
    if paragraph_sizes == (900, 900):
        assert [len(chunk.text) for chunk in chunks] == [900, 900]


@pytest.mark.parametrize(
    "child_target,child_max,parent_target", [(640, 1600, 2560), (100, 200, 800)]
)
def test_tree_leaf_chunks_keep_flat_metadata_and_tree_packing(
    child_target: int, child_max: int, parent_target: int
) -> None:
    processor = ContentProcessor(
        text_chunk_target_chars=child_target,
        text_chunk_max_chars=8000,
        text_child_chunk_max_chars=child_max,
        text_parent_chunk_target_chars=parent_target,
    )
    text = "# 章\n\n" + "\n\n".join(_paragraphs("段", 6, 500))

    chunks, summary, _ = processor._text_chunks(
        _request(), text, "text/markdown", "manifest_hash_1"
    )

    assert summary["tree"]["tree_indexed"] is True
    assert all("parent_id" not in chunk.metadata for chunk in chunks)
    assert all("parent_text" not in chunk.metadata for chunk in chunks)
    assert [len(chunk.text) for chunk in chunks] == [1504, 1504]
    assert all(chunk.metadata["section_path"] == "章" for chunk in chunks)
    assert [chunk.metadata["cr_parent_group"] for chunk in chunks] == ["1:1", "1:2"]
    assert all(chunk.snippet == chunk.text for chunk in chunks)
    assert all("parent_paragraphs" not in chunk.locator for chunk in chunks)


def test_tree_long_paragraph_uses_tree_maximum_and_keeps_full_snippet() -> None:
    chunks, _, _ = ContentProcessor(text_child_chunk_max_chars=200)._text_chunks(
        _request(), "# 章\n\n" + "长" * 16001, "text/markdown", "manifest_hash_1"
    )

    assert [len(chunk.text) for chunk in chunks] == [5334, 5334, 5333]
    assert all(chunk.snippet == chunk.text for chunk in chunks)
    assert "".join(chunk.text for chunk in chunks) == "长" * 16001


@pytest.mark.parametrize("structure_class", ["basic", "tree", "partial"])
def test_mineru_structure_class_controls_parent_child_path_before_packing(
    structure_class: str,
) -> None:
    text = "# 第一章\n\n" + "\n\n".join(_paragraphs("甲", 6, 500))
    text += "\n\n# 第二章\n\n" + "\n\n".join(_paragraphs("乙", 6, 500))
    processor = ContentProcessor(
        mineru=lambda _: {"text": text, "structure": {"class": structure_class}}
    )

    output = processor.process(
        _request(),
        b"pdf",
        media_kind="application/pdf",
        content_manifest_id="manifest_1",
        content_manifest_hash="manifest_hash_1",
    )

    if structure_class == "basic":
        assert [len(chunk.text) for chunk in output.chunks] == [500] * 12
        assert all("parent_id" in chunk.metadata for chunk in output.chunks)
        assert all(
            not ("甲" in chunk.metadata["parent_text"] and "乙" in chunk.metadata["parent_text"])
            for chunk in output.chunks
        )
        assert output.receipt.processing_summary["tree"]["tree_indexed"] is False
    else:
        assert [len(chunk.text) for chunk in output.chunks] == [1504] * 4
        assert all("parent_id" not in chunk.metadata for chunk in output.chunks)
        assert output.receipt.processing_summary["tree"]["tree_indexed"] is True


def test_short_paragraphs_still_pack_toward_child_target() -> None:
    text = "\n\n".join(["短" * 250, "短" * 250, "短" * 250])

    chunks = _text_chunks(ContentProcessor(), text)

    # 250+2+250 ≤ 640 合并；再加第三段超过 640，另起子块。
    assert [len(chunk.text) for chunk in chunks] == [502, 250]
    # 两个子块合起来不超过父块目标，归同一父块。
    assert {chunk.metadata["parent_id"] for chunk in chunks} == {"parent_1"}


def test_multi_child_parent_locator_carries_parent_paragraph_range() -> None:
    text = "# 章\n\n" + "\n\n".join(_paragraphs("段", 6, 500))

    chunks = _text_chunks(ContentProcessor(), text)

    first_parent = [c for c in chunks if c.metadata["parent_id"] == "parent_1"]
    assert len(first_parent) > 1
    ranges = {tuple(chunk.locator["parent_paragraphs"]) for chunk in first_parent}
    assert len(ranges) == 1
    start, end = ranges.pop()
    assert start == first_parent[0].locator["paragraph"]
    assert end >= first_parent[-1].locator["paragraph"]
    assert all(chunk.locator["section_path"] == "章" for chunk in first_parent)


def test_single_child_parent_keeps_existing_locator_shape() -> None:
    chunks = _text_chunks(ContentProcessor(), "# 章\n\n只有一段")

    assert chunks[0].locator == {"section_path": "章"}


# ---------------------------------------------------------------------------
# CR 与嵌入文本
# ---------------------------------------------------------------------------


def test_only_children_are_emitted_and_cr_runs_once_per_child() -> None:
    provider = FakeProvider()
    text = "\n\n".join(_paragraphs("段", 6, 500))

    output = ContentProcessor(contextual_provider=provider).process(
        _request(),
        text,
        media_kind="text/markdown",
        content_manifest_id="manifest_1",
        content_manifest_hash="manifest_hash_1",
    )

    assert sorted(call["chunk_id"] for call in provider.calls) == sorted(
        chunk.chunk_id for chunk in output.chunks
    )
    for chunk in output.chunks:
        assert chunk.embedding_text.startswith(
            f"CONTEXTUAL RETRIEVAL\ncontext for {chunk.chunk_id}"
        )
        assert chunk.embedding_text.endswith(f"ORIGINAL CHUNK\n{chunk.text}")
        # 父块只作为 metadata 随子块存放，自身不产出嵌入文本或检索单元。
        assert chunk.metadata["parent_text"] not in chunk.embedding_text or (
            chunk.metadata["parent_text"] == chunk.text
        )
    assert len({chunk.chunk_id for chunk in output.chunks}) == len(output.chunks)


def test_shared_section_path_goes_to_sparse_and_metadata_not_embedding_text() -> None:
    seen: list[dict[str, object]] = []

    class Compressor:
        def compress(self, text: str, *, context: dict[str, object]) -> str:
            seen.append(dict(context))
            return text

    chunks = _text_chunks(
        ContentProcessor(compressor=Compressor()),
        "# 共享标题\n\n" + "\n\n".join(_paragraphs("段", 4, 500)),
    )

    assert seen and all("section_path" not in context for context in seen)
    for chunk in chunks:
        assert "共享标题" not in chunk.embedding_text
        assert chunk.sparse_text is not None and chunk.sparse_text.startswith("共享标题\n")
        assert chunk.metadata["section_path"] == "共享标题"


# ---------------------------------------------------------------------------
# snippet
# ---------------------------------------------------------------------------


def test_snippet_is_the_full_child_text_beyond_500_chars() -> None:
    chunks = _text_chunks(ContentProcessor(), "长" * 1200)

    assert len(chunks[0].text) == 1200
    assert chunks[0].snippet == chunks[0].text


# ---------------------------------------------------------------------------
# 表格
# ---------------------------------------------------------------------------


def test_table_rows_are_not_headings() -> None:
    for line in ("| Name | Qty |", "| --- | --- |", "| Apple | 3 |", "| 名称 | 数量 |"):
        assert _heading(line) is False
    assert _heading("Introduction") is True


def test_latin_table_header_stays_in_body_not_section_path() -> None:
    table = "| Name | Qty |\n| --- | --- |\n| Apple | 3 |"

    assert _sections(table) == [("", table)]


def test_table_is_one_atomic_block_end_to_end() -> None:
    table = "| Name | Qty |\n| --- | --- |\n| Apple | 3 |\n| Pear | 5 |"
    text = f"# 库存\n\n前文说明。\n{table}\n后文说明。"

    chunks = _text_chunks(ContentProcessor(), text)

    table_chunks = [chunk for chunk in chunks if chunk.text.startswith("| Name")]
    assert len(table_chunks) == 1
    assert table_chunks[0].text == table
    # 表格是原子单元：不与相邻正文合并，自成父块。
    assert table_chunks[0].metadata["parent_text"] == table
    assert all(chunk.metadata["section_path"] == "库存" for chunk in chunks)
    assert any(chunk.text == "前文说明。" for chunk in chunks)


def test_long_table_splits_by_rows_and_repeats_header() -> None:
    header = "| 区域名称 | 销售额说明 |\n| --- | --- |"
    rows = [f"| 华东地区第{index:03d}号门店 | 本季度销售额稳定增长 |" for index in range(200)]
    table = "\n".join([header, *rows])
    assert len(table) > 1600

    chunks = _text_chunks(ContentProcessor(), table)

    assert len(chunks) > 1
    seen_rows: list[str] = []
    for chunk in chunks:
        assert chunk.text.startswith(header + "\n")
        assert len(chunk.text) <= 1600
        seen_rows.extend(chunk.text.splitlines()[2:])
        assert chunk.metadata["parent_text"] == chunk.text
    assert seen_rows == rows


def test_single_overlong_table_row_stays_whole_with_header() -> None:
    header = "| 名称 | 说明 |\n| --- | --- |"
    long_row = "| 甲 | " + "长" * 2000 + " |"
    table = "\n".join([header, "| 乙 | 短 |", long_row, "| 丙 | 短 |"])

    chunks = _text_chunks(ContentProcessor(), table)

    assert [chunk.text for chunk in chunks] == [
        f"{header}\n| 乙 | 短 |",
        f"{header}\n{long_row}",
        f"{header}\n| 丙 | 短 |",
    ]
