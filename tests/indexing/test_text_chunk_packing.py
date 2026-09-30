"""整段装箱与 PDF 内容级页定位的行为测试（fix-chunk-packing-and-pdf-locators）。

覆盖 delta specs：text-chunking（装箱目标/表格原子/均衡切分/锚语义）与
pdf-page-locators（内容级搜索优先、位置对应回退）。
"""

from __future__ import annotations

from app.indexing.processing import ContentProcessor, _pack_section_paragraphs
from tests.indexing.test_contracts import _request


def _text_chunks(processor: ContentProcessor, text: str):
    chunks, _summary, _degradations = processor._text_chunks(
        _request(), text, "text/markdown", "manifest_hash_1"
    )
    return chunks


# ---------------------------------------------------------------------------
# text-chunking：装箱目标配置
# ---------------------------------------------------------------------------


def test_processor_defaults_to_640_char_packing_target() -> None:
    assert ContentProcessor()._text_chunk_target_chars == 640


# ---------------------------------------------------------------------------
# text-chunking：相邻段落装箱
# ---------------------------------------------------------------------------


def test_small_paragraphs_pack_into_fewest_chunks_under_target() -> None:
    processor = ContentProcessor(text_chunk_target_chars=80)
    text = "\n\n".join(["a" * 30, "b" * 30, "c" * 30])

    chunks = _text_chunks(processor, text)

    assert [chunk.text for chunk in chunks] == ["a" * 30 + "\n\n" + "b" * 30, "c" * 30]
    assert all(len(chunk.text) <= 80 for chunk in chunks)


def test_merged_chunk_keeps_paragraph_separator_and_first_anchor() -> None:
    processor = ContentProcessor(text_chunk_target_chars=80)
    text = "\n\n".join(["a" * 30, "b" * 30, "c" * 30])

    chunks = _text_chunks(processor, text)

    assert [chunk.locator.get("paragraph") for chunk in chunks] == [1, 3]
    assert "\n\n" in chunks[0].text


def test_single_chunk_section_has_no_paragraph_anchor() -> None:
    processor = ContentProcessor(text_chunk_target_chars=80)

    chunks = _text_chunks(processor, "a" * 30)

    assert len(chunks) == 1
    assert "paragraph" not in chunks[0].locator


# ---------------------------------------------------------------------------
# text-chunking：表格段原子性
# ---------------------------------------------------------------------------


def test_table_paragraph_stays_atomic_between_prose() -> None:
    # 直接测装箱函数；端到端表格路径见 test_parent_child_chunking。
    table = "| 表头 |\n| --- |\n| 行一 |\n| 行二 |"

    packed = _pack_section_paragraphs(["a" * 30, table, "b" * 30], target=8000, maximum=8000)

    assert packed == [("a" * 30, 1), (table, 2), ("b" * 30, 3)]


def test_table_paragraph_is_not_merged_even_under_target() -> None:
    table = "| alpha |\n| --- |\n| beta |"

    packed = _pack_section_paragraphs(["a" * 10, table, "b" * 10], target=8000, maximum=8000)

    assert packed == [("a" * 10, 1), (table, 2), ("b" * 10, 3)]


# ---------------------------------------------------------------------------
# text-chunking：超长段均衡切分
# ---------------------------------------------------------------------------


def test_overlong_paragraph_splits_balanced_without_tail_fragment() -> None:
    processor = ContentProcessor(text_child_chunk_max_chars=8000)

    chunks = _text_chunks(processor, "x" * 16001)

    assert [len(chunk.text) for chunk in chunks] == [5334, 5334, 5333]
    assert all(len(chunk.text) <= 8000 for chunk in chunks)
    assert "".join(chunk.text for chunk in chunks) == "x" * 16001
    assert {chunk.locator["paragraph"] for chunk in chunks} == {1}


def test_two_overlong_paragraphs_keep_distinguishable_anchors() -> None:
    processor = ContentProcessor(text_child_chunk_max_chars=8000)
    text = "\n\n".join(["a" * 9000, "b" * 9000])

    chunks = _text_chunks(processor, text)

    assert len(chunks) == 4
    assert {chunk.locator["paragraph"] for chunk in chunks} == {1, 2}
    assert [chunk.locator["paragraph"] for chunk in chunks] == [1, 1, 2, 2]


# ---------------------------------------------------------------------------
# pdf-page-locators：内容级页定位
# ---------------------------------------------------------------------------


def _pdf_processor(mineru_result: dict) -> ContentProcessor:
    return ContentProcessor(mineru=lambda content: mineru_result)


def _process_pdf(processor: ContentProcessor):
    return processor.process(
        _request(),
        b"pdf",
        media_kind="application/pdf",
        content_manifest_id="manifest_1",
        content_manifest_hash="manifest_hash_1",
    )


def test_pdf_page_locator_prefers_content_hit_over_positional_match() -> None:
    """snippet 在页重建文本命中时，页码来自内容而非 chunk 序号。"""

    processor = _pdf_processor(
        {
            "text": "# First\nfirst\n\n# Second\nsecond",
            "page_count": 2,
            "has_text_layer": True,
            # 位置对应会把 chunk 1 指到 page 1；内容命中应纠正为 page 2。
            "chunks": [{"page": 1, "span": "0:5"}, {"page": 1, "span": "0:5"}],
            "page_texts": {"1": "first", "2": "second"},
        }
    )

    output = _process_pdf(processor)

    assert output.chunks[0].locator == {"page": 1, "span": "0:5"}
    assert output.chunks[1].locator == {"page": 2, "span": "0:6"}


def test_pdf_page_locator_takes_first_hit_across_pages() -> None:
    processor = _pdf_processor(
        {
            "text": "# Only\nsecond",
            "page_count": 2,
            "has_text_layer": True,
            "chunks": [{"page": 2, "span": "0:6"}],
            "page_texts": {"1": "second prefix", "2": "second"},
        }
    )

    output = _process_pdf(processor)

    assert output.chunks[0].locator == {"page": 1, "span": "0:6"}


def test_pdf_page_locator_falls_back_to_positional_when_content_misses() -> None:
    processor = _pdf_processor(
        {
            "text": "# Only\nunfindable content",
            "page_count": 2,
            "has_text_layer": True,
            "chunks": [{"page": 2, "span": "0:9"}],
            "page_texts": {"1": "different text"},
        }
    )

    output = _process_pdf(processor)

    assert output.chunks[0].locator == {"page": 2, "span": "0:9"}


def test_pdf_page_locator_probe_stops_at_paragraph_boundary() -> None:
    """装箱块首段短于 80 字时，probe 不得跨越 \\n\\n 段界——页重建文本
    按单换行连接，跨段 probe 必然 miss 而回落位置对应。"""

    processor = _pdf_processor(
        {
            "text": "# Guide\nintro line\n\nsecond paragraph marker here",
            "page_count": 2,
            "has_text_layer": True,
            # 位置对应会把合并块指到 page 2；首段内容命中应纠正为 page 1。
            "chunks": [{"page": 2, "span": "0:9"}],
            "page_texts": {"1": "intro line\nsecond paragraph marker here", "2": "other"},
        }
    )

    output = _process_pdf(processor)

    assert output.chunks[0].locator == {"page": 1, "span": "0:40"}


def test_pdf_page_locator_skips_content_search_without_text_layer() -> None:
    processor = _pdf_processor(
        {
            "text": "scanned",
            "page_count": 3,
            "has_text_layer": False,
            "chunks": [{"page": 3, "span": "0:7"}],
            "page_texts": {"1": "scanned"},
        }
    )

    output = _process_pdf(processor)

    assert output.chunks[0].locator == {"page": 3}
