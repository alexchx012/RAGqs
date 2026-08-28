"""indexing-retrieval-gaps 契约测试（审计 B4、B9-locator、07/08 子审计点名项）。

A1: PDF 内嵌图片计量（figure 块计数 → image_count → pages+images 折算；
    internvl 只记用量不扣；降级不虚计）。
A2: locator 契约（建树文档 paragraph 锚点；PDF 页内字符偏移 span；
    扫描/basic 行为不变）。
A3: 代际冲突自动重暂存（retry_wait + generation_conflict 记账）。
A4: 交叉引用反向链接（reverse_links 元数据通道）。
A5: 代码路径共享符号名进 sparse_text。
"""

from __future__ import annotations

from app.indexing.mineru import _page_facts
from app.indexing.processing import ContentProcessor, parse_reverse_links
from tests.indexing.test_contracts import _request

# ---------------------------------------------------------------------------
# A1: PDF 内嵌图片计量
# ---------------------------------------------------------------------------


def test_page_facts_counts_figures_and_reconstructs_text() -> None:
    """middle.json figure 块计数与页文本重建（B4 判定权威 = MinerU）。"""

    middle = {
        "pdf_info": [
            {
                "preproc_blocks": [
                    {
                        "type": "title",
                        "lines": [{"spans": [{"content": "Overview", "score": 0.95}]}],
                    },
                    {"type": "image", "lines": []},
                ]
            },
            {
                "preproc_blocks": [
                    {
                        "type": "text",
                        "lines": [
                            {"spans": [{"content": "Body", "score": 0.9}]},
                            {"spans": [{"content": "text", "score": 0.9}]},
                        ],
                    },
                    {"type": "image", "lines": []},
                    {"type": "image", "lines": []},
                ]
            },
        ]
    }
    texts, figures = _page_facts(middle)
    assert figures == {1: 1, 2: 2}
    assert texts[1] == "Overview"
    assert texts[2] == "Body\ntext"


def test_pdf_embedded_images_meter_as_pages_plus_images() -> None:
    """A1: figure 计数进 summary image_count，文档载体按 pages+images 折算扣页。"""

    processor = ContentProcessor(
        mineru=lambda content: {
            "text": "# Report\n\nBody content",
            "page_count": 3,
            "has_text_layer": True,
            "chunks": [{"page": 1, "span": "0:12"}],
            "image_count": 4,
        },
    )

    output = processor.process(
        _request(),
        b"pdf",
        media_kind="application/pdf",
        content_manifest_id="manifest_1",
        content_manifest_hash="manifest_hash_1",
    )

    summary = output.receipt.processing_summary
    assert summary["image_count"] == 4
    assert summary["metering"]["class"] == "document"
    assert summary["metering"]["embedded_image_count"] == 4
    # 无 describer（none provider）→ 照计不豁免：折算 = pages + images。
    assert summary["metering"]["embedded_image_provider"] == "none"

    from app.documents.service import _metered_pages

    assert (
        _metered_pages(
            page_count=summary["page_count"],
            image_count=summary["image_count"],
            summary=summary,
        )
        == 3 + 4
    )


def test_pdf_embedded_images_internvl_records_without_debit() -> None:
    """A1: internvl 只记用量不扣页——折算回到纯 pages。"""

    from app.documents.service import _metered_pages

    assert (
        _metered_pages(
            page_count=3,
            image_count=4,
            summary={
                "metering": {
                    "class": "document",
                    "embedded_image_count": 4,
                    "embedded_image_provider": "internvl",
                }
            },
        )
        == 3
    )


def test_pdf_degraded_probe_does_not_fabricate_images() -> None:
    """A1: MinerU 降级路径（无 image_count 事实）不虚计图片。"""

    from app.documents.service import _metered_pages

    assert (
        _metered_pages(
            page_count=2,
            image_count=0,
            summary={"metering": {"class": "document"}},
        )
        == 2
    )


# ---------------------------------------------------------------------------
# A2: locator 契约
# ---------------------------------------------------------------------------


def test_multi_paragraph_section_gets_paragraph_anchor() -> None:
    """A2: 同 section 多 chunk 可区分（paragraph 锚点，切块同遍产出）。"""

    paragraph_a = "Alpha paragraph. " + "alpha detail. " * 600
    paragraph_b = "Beta paragraph. " + "beta detail. " * 600
    text = f"# Guide\n\n{paragraph_a}\n\n{paragraph_b}"
    processor = ContentProcessor(mineru=None)

    output = processor.process(
        _request(),
        text.encode("utf-8"),
        media_kind="text/markdown",
        content_manifest_id="manifest_1",
        content_manifest_hash="manifest_hash_1",
    )

    assert len(output.chunks) >= 2
    locators = [chunk.locator for chunk in output.chunks]
    assert all(locator.get("section_path") == "Guide" for locator in locators)
    # 全部 chunk 都带 paragraph 锚点，且不同段落的锚点可区分。
    assert all(locator.get("paragraph") is not None for locator in locators)
    paragraphs = {locator["paragraph"] for locator in locators}
    assert len(paragraphs) == 2
    assert paragraphs == {1, 2}


def test_single_chunk_section_keeps_existing_locator_shape() -> None:
    """A2: 单 chunk section 不加锚（既有 locator 形状不变）。"""

    processor = ContentProcessor(mineru=None)
    output = processor.process(
        _request(),
        b"# Guide\n\nWord content",
        media_kind="text/markdown",
        content_manifest_id="manifest_1",
        content_manifest_hash="manifest_hash_1",
    )
    assert output.chunks[0].locator == {"section_path": "Guide"}


def test_pdf_span_refines_to_snippet_page_offset() -> None:
    """A2: 有文本层 PDF 的 span 为 snippet 在页内重建文本的真实字符偏移
    （同页多处重复可消歧），未命中回退 mineru 页幅 span。"""

    page_text = "Revenue rose sharply. Revenue rose sharply."
    processor = ContentProcessor(
        mineru=lambda content: {
            "text": "# Report\n\nRevenue rose sharply. Revenue rose sharply.",
            "page_count": 1,
            "has_text_layer": True,
            "chunks": [{"page": 1, "span": "0:35"}],
            "page_texts": {"1": page_text},
        },
    )

    output = processor.process(
        _request(),
        b"pdf",
        media_kind="application/pdf",
        content_manifest_id="manifest_1",
        content_manifest_hash="manifest_hash_1",
    )

    span = output.chunks[0].locator["span"]
    start, end = (int(part) for part in span.split(":"))
    # snippet 对应文本与页内偏移一致（页文本坐标系）。
    assert page_text[start:end] == output.chunks[0].snippet


def test_scanned_pdf_keeps_page_only_locator() -> None:
    """A2: 无文本层（扫描）PDF：只保留 page，无合成 span。"""

    processor = ContentProcessor(
        mineru=lambda content: {
            "text": "# Report\n\nBody",
            "page_count": 1,
            "has_text_layer": False,
            "chunks": [{"page": 1, "span": None}],
        },
    )
    output = processor.process(
        _request(),
        b"pdf",
        media_kind="application/pdf",
        content_manifest_id="manifest_1",
        content_manifest_hash="manifest_hash_1",
    )
    assert output.chunks[0].locator == {"page": 1}


# ---------------------------------------------------------------------------
# A4: 交叉引用反向链接
# ---------------------------------------------------------------------------


def test_reverse_links_parser_extracts_figure_table_section_refs() -> None:
    links = parse_reverse_links(
        "架构如图 3 所示；详见 4.2 节。数据见表 7。figure 2 shows it. see section 5.1"
    )
    assert {"kind": "figure", "ref": "3"} in [dict(link) for link in links]
    assert {"kind": "section", "ref": "4.2"} in [dict(link) for link in links]
    assert {"kind": "table", "ref": "7"} in [dict(link) for link in links]
    assert {"kind": "figure", "ref": "2"} in [dict(link) for link in links]
    assert {"kind": "section", "ref": "5.1"} in [dict(link) for link in links]


def test_text_chunks_carry_reverse_links_metadata() -> None:
    """A4: 含交叉引用的 chunk 元数据带解析出的 reverse_links。"""

    processor = ContentProcessor(mineru=None)
    output = processor.process(
        _request(),
        b"# Guide\n\nThe architecture is shown in figure 3 and table 2.",
        media_kind="text/markdown",
        content_manifest_id="manifest_1",
        content_manifest_hash="manifest_hash_1",
    )
    links = output.chunks[0].metadata["reverse_links"]
    assert {"kind": "figure", "ref": "3"} in links
    assert {"kind": "table", "ref": "2"} in links


def test_text_without_refs_has_no_reverse_links_key() -> None:
    processor = ContentProcessor(mineru=None)
    output = processor.process(
        _request(),
        b"# Guide\n\nPlain content without references.",
        media_kind="text/markdown",
        content_manifest_id="manifest_1",
        content_manifest_hash="manifest_hash_1",
    )
    assert "reverse_links" not in output.chunks[0].metadata


# ---------------------------------------------------------------------------
# A5: 代码符号名进 sparse_text
# ---------------------------------------------------------------------------


def test_code_chunks_sparse_text_includes_symbol_name() -> None:
    """A5: 符号名进 sparse_text——BM25 可检索命中符号名查询（含长符号续块）。"""

    long_body = "x = 1\n" * 200
    source = f"function processItems() {{\n{long_body}}}\n"
    processor = ContentProcessor(mineru=None)

    output = processor.process(
        _request(),
        source.encode("utf-8"),
        media_kind="text/javascript",
        content_manifest_id="manifest_1",
        content_manifest_hash="manifest_hash_1",
    )

    assert output.chunks
    for chunk in output.chunks:
        assert chunk.sparse_text.startswith("processItems") or "processItems" in chunk.sparse_text
    # 符号名查询在 sparse 字段可命中。
    assert any("processItems" in chunk.sparse_text for chunk in output.chunks)


def test_text_path_sparse_text_unchanged() -> None:
    """A5: 文本路径 sparse 字段不变（section_path 前缀为既有行为）。"""

    processor = ContentProcessor(mineru=None)
    output = processor.process(
        _request(),
        b"# Guide\n\nWord content here",
        media_kind="text/markdown",
        content_manifest_id="manifest_1",
        content_manifest_hash="manifest_hash_1",
    )
    chunk = output.chunks[0]
    assert chunk.sparse_text == f"Guide\n{chunk.text}"
