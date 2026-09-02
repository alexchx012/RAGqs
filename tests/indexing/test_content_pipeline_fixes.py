"""fix-review-findings-2026-09（indexing-content）内容管线验收测试。

A60 结构化表格管线：MinerU 真实适配器从 middle.json 产出 tables、CSV 行组
超长不切断单行、CSV/xlsx chunk 补 block/total_blocks；
A61 结构化分流判据：深嵌套走代码路径、schema 特征组合收紧（"type" 单键不再
误入）、YAML 子集支持嵌套映射/列表、XML 保留层级、Python 模块级非符号代码
不随符号切分丢弃。
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook

from app.indexing.mineru import MinerUAdapter
from app.indexing.processing import ContentProcessor
from app.platform.errors import PlatformError
from tests.indexing.test_contracts import _request

# ---------------------------------------------------------------------------
# A60: MinerU 结构化表格
# ---------------------------------------------------------------------------


def _table_block(headers_html: str, rows_html: str, caption: str) -> dict[str, Any]:
    return {
        "type": "table",
        "bbox": [10, 10, 200, 120],
        "blocks": [
            {
                "type": "table_body",
                "lines": [
                    {
                        "spans": [
                            {
                                "bbox": [12, 12, 198, 118],
                                "type": "table",
                                "score": 0.99,
                                "html": f"<table>{headers_html}{rows_html}</table>",
                            }
                        ]
                    }
                ],
            },
            {
                "type": "table_caption",
                "lines": [{"spans": [{"type": "text", "content": caption, "score": 1.0}]}],
            },
        ],
    }


def _middle_with_tables() -> dict[str, Any]:
    first = _table_block(
        "<tr><td>区域</td><td>销售额</td></tr>",
        "<tr><td>华东</td><td>10</td></tr><tr><td>华南</td><td>20</td></tr>",
        "表1 区域销售额",
    )
    second = _table_block(
        "<tr><td>项目</td><td>数值</td></tr>",
        "<tr><td>延迟</td><td>12ms</td></tr>",
        "表2 指标",
    )
    return {
        "pdf_info": [
            {
                "page_idx": 0,
                # middle.json 在 para_blocks 与页级 tables 便捷列表中重复携带
                # 同一表块——适配器按表体去重。
                "para_blocks": [
                    {
                        "type": "text",
                        "lines": [{"spans": [{"content": "Intro", "type": "text"}]}],
                    },
                    first,
                ],
                "tables": [first],
            },
            {
                "page_idx": 1,
                "para_blocks": [second],
                "tables": [second],
            },
        ],
        "_backend": "pipeline",
        "_version_name": "2.5.1",
    }


def _mineru_runner(middle: dict[str, Any], markdown: str = "# Report\n\nBody content"):
    def runner(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        output_dir = Path(command[command.index("-o") + 1])
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "doc.md").write_text(markdown, encoding="utf-8")
        (output_dir / "middle.json").write_text(
            json.dumps(middle, ensure_ascii=False), encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 0)

    return runner


def test_mineru_adapter_extracts_structured_tables_from_middle_json() -> None:
    adapter = MinerUAdapter(runner=_mineru_runner(_middle_with_tables()))

    facts = adapter.parse(b"pdf", media_kind="application/pdf", page_count=2)

    assert facts["tables"] == [
        {
            "page": 1,
            "page_start": 1,
            "page_end": 1,
            "title": "表1 区域销售额",
            "headers": ["区域", "销售额"],
            "rows": [["华东", "10"], ["华南", "20"]],
        },
        {
            "page": 2,
            "page_start": 2,
            "page_end": 2,
            "title": "表2 指标",
            "headers": ["项目", "数值"],
            "rows": [["延迟", "12ms"]],
        },
    ]


def test_mineru_tables_feed_inline_table_to_markdown_chunks() -> None:
    adapter = MinerUAdapter(runner=_mineru_runner(_middle_with_tables()))
    output = ContentProcessor(mineru=adapter).process(
        _request(),
        b"pdf",
        media_kind="application/pdf",
        content_manifest_id="manifest_1",
        content_manifest_hash="manifest_hash_1",
        page_count=2,
    )

    table_chunks = [chunk for chunk in output.chunks if chunk.media_kind == "table/markdown"]
    assert len(table_chunks) == 2
    assert table_chunks[0].text.splitlines()[0] == "| 区域 | 销售额 |"
    assert table_chunks[0].locator == {"page": 1}
    assert table_chunks[0].metadata["table_title"] == "表1 区域销售额"
    assert table_chunks[0].metadata["block"] == 1
    assert table_chunks[0].metadata["total_blocks"] == 2
    assert table_chunks[1].locator == {"page": 2}
    assert output.receipt.processing_summary["table_count"] == 2


def test_mineru_middle_without_table_blocks_keeps_tables_empty() -> None:
    middle = {
        "pdf_info": [
            {
                "page_idx": 0,
                "para_blocks": [{"type": "text", "lines": [{"spans": [{"content": "Only text"}]}]}],
                "tables": [],
            }
        ]
    }
    adapter = MinerUAdapter(runner=_mineru_runner(middle))

    facts = adapter.parse(b"pdf", media_kind="application/pdf", page_count=1)

    assert facts["tables"] == []


# ---------------------------------------------------------------------------
# A60: CSV 行组切分与 block 元数据
# ---------------------------------------------------------------------------


def test_csv_row_group_overflow_keeps_whole_rows() -> None:
    long_cell = "x" * 25
    processor = ContentProcessor(text_chunk_max_chars=20)
    output = processor.process(
        _request(),
        f"name,note\na,{long_cell}\nb,short\nc,tail",
        media_kind="text/csv",
        content_manifest_id="manifest_1",
        content_manifest_hash="manifest_hash_1",
    )

    assert len(output.chunks) >= 2
    values: list[str] = []
    for chunk in output.chunks:
        for line in chunk.text.splitlines():
            # 每行都是完整的「name: .. | note: ..」数据行——没有半行残片。
            assert line.startswith("name: ")
            assert " | note: " in line
            values.append(line.split(" | note: ", 1)[1])
    # 三个单元格值各完整出现一次，超长单元格未被按字符截断。
    assert sorted(values) == sorted([long_cell, "short", "tail"])


def test_csv_chunks_carry_block_and_total_blocks_metadata() -> None:
    processor = ContentProcessor(text_chunk_max_chars=12)
    output = processor.process(
        _request(),
        "name,value\na,1\nb,2\nc,3",
        media_kind="text/csv",
        content_manifest_id="manifest_1",
        content_manifest_hash="manifest_hash_1",
    )

    total = len(output.chunks)
    assert total >= 2
    for position, chunk in enumerate(output.chunks, start=1):
        assert chunk.metadata["block"] == position
        assert chunk.metadata["total_blocks"] == total
        assert chunk.metadata["row_start"] <= chunk.metadata["row_end"]
    row_groups = output.receipt.processing_summary["row_groups"]
    assert [group["block"] for group in row_groups] == list(range(1, total + 1))


def test_xlsx_chunks_carry_block_and_total_blocks_metadata() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["name", "value"])
    for index in range(45):
        sheet.append([f"item{index:02d}", str(index)])
    stream = io.BytesIO()
    workbook.save(stream)

    output = ContentProcessor().process(
        _request(),
        stream.getvalue(),
        media_kind="xlsx",
        content_manifest_id="manifest_1",
        content_manifest_hash="manifest_hash_1",
    )

    assert len(output.chunks) == 2
    for position, chunk in enumerate(output.chunks, start=1):
        assert chunk.metadata["block"] == position
        assert chunk.metadata["total_blocks"] == 2
    row_groups = output.receipt.processing_summary["row_groups"]
    assert [group["block"] for group in row_groups] == [1, 2]


# ---------------------------------------------------------------------------
# A61: 结构化分流判据
# ---------------------------------------------------------------------------


def _process_structured(content: str, media_kind: str) -> Any:
    return ContentProcessor().process(
        _request(),
        content,
        media_kind=media_kind,
        content_manifest_id="manifest_1",
        content_manifest_hash="manifest_hash_1",
    )


def test_deeply_nested_json_routes_to_config_path() -> None:
    output = _process_structured('{"alpha": {"beta": {"gamma": {"delta": "value"}}}}', "json")

    summary = output.receipt.processing_summary
    assert summary["metering"]["class"] == "config"
    assert output.chunks[0].metadata["symbol"] == "module"
    assert "keys" not in output.chunks[0].metadata


def test_top_level_type_key_no_longer_routes_to_code_path() -> None:
    output = _process_structured('{"type": "notification", "title": "Alert"}', "json")

    chunk = output.chunks[0]
    assert "type: notification" in chunk.text
    assert "title: Alert" in chunk.text
    assert chunk.metadata["keys"] == ["type", "title"]
    assert output.receipt.processing_summary["cr"]["unit"] == "key_path"


def test_schema_feature_combination_still_routes_to_code_path() -> None:
    output = _process_structured(
        '{"type": "object", "properties": {"name": {"type": "string"}}}', "json"
    )

    assert output.receipt.processing_summary["metering"]["class"] == "config"
    assert output.chunks[0].metadata["symbol"] == "module"


def test_yaml_subset_parses_nested_mappings_and_lists() -> None:
    output = _process_structured(
        "---\n"
        "# service settings\n"
        "server:\n"
        "  host: localhost\n"
        "  port: 8080\n"
        "paths:\n"
        "  - /api\n"
        "  - /web\n",
        "yaml",
    )

    chunk = output.chunks[0]
    assert "server.host: localhost" in chunk.text
    assert "server.port: 8080" in chunk.text
    assert "paths[0]" in chunk.text
    assert "paths[1]" in chunk.text
    assert chunk.metadata["keys"] == ["server.host", "server.port", "paths[0]", "paths[1]"]


def test_yaml_list_of_mappings_becomes_structured_table() -> None:
    output = _process_structured(
        "- name: alpha\n  role: primary\n- name: beta\n  role: secondary\n", "yaml"
    )

    assert output.receipt.processing_summary["metering"]["class"] == "table"
    assert output.chunks[0].metadata["headers"] == ["name", "role"]


def test_inconsistent_yaml_fails_with_structured_parse_error() -> None:
    with pytest.raises(PlatformError) as error:
        _process_structured("server:\n    host: localhost\n  port: 8080\n", "yaml")

    assert error.value.code == "structured_parse_failed"


def test_xml_parsing_preserves_hierarchy() -> None:
    shallow = _process_structured(
        "<config><host>db.local</host><item>a</item><item>b</item></config>",
        "application/xml",
    )

    chunk = shallow.chunks[0]
    assert "config.host: db.local" in chunk.text
    assert "config.item[0]" in chunk.text
    assert "config.item[1]" in chunk.text

    deep = _process_structured(
        "<config><db><host>db.local</host><port>5432</port></db></config>",
        "application/xml",
    )
    # 层级保留后，3 层深的 XML 按深嵌套判据走代码/配置路径，不再键值扁平化。
    assert deep.receipt.processing_summary["metering"]["class"] == "config"
    assert deep.chunks[0].metadata["symbol"] == "module"


def test_python_module_level_statements_survive_symbol_chunking() -> None:
    source = "import json\n\nDEFAULT_TIMEOUT = 30\n\n\ndef handler(event):\n    return json.dumps(event)\n"
    output = _process_structured(source, "text/x-python")

    module_chunks = [chunk for chunk in output.chunks if chunk.metadata["symbol"] == "module"]
    symbol_chunks = [chunk for chunk in output.chunks if chunk.metadata["symbol"] == "handler"]
    assert len(module_chunks) == 1
    assert "import json" in module_chunks[0].text
    assert "DEFAULT_TIMEOUT = 30" in module_chunks[0].text
    assert len(symbol_chunks) == 1
    assert "def handler" in symbol_chunks[0].text


def test_python_decorator_lines_stay_with_the_symbol() -> None:
    source = "import functools\n\n@functools.lru_cache()\ndef cached():\n    return 1\n"
    output = _process_structured(source, "text/x-python")

    module_chunks = [chunk for chunk in output.chunks if chunk.metadata["symbol"] == "module"]
    cached_chunks = [chunk for chunk in output.chunks if chunk.metadata["symbol"] == "cached"]
    assert "import functools" in module_chunks[0].text
    assert "@functools.lru_cache()" in cached_chunks[0].text
