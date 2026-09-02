"""MinerU pipeline adapter for document structure probing and parsing.

The adapter is the real transport boundary for MinerU. It always uses the
``pipeline`` backend, samples first/middle/tail pages for structure signals,
falls back to the basic text path when no signal is found, and returns typed
parse facts for the staging/receipt contract. Process failures surface as the
stable ``mineru_parse_failed`` error; no partial success is reported.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from app.platform.errors import PlatformError

from .processing import OCRSamplePlan

_Runner = Callable[..., subprocess.CompletedProcess[bytes]]
_UsageSink = Callable[[Mapping[str, Any]], None]

_MEDIA_SUFFIXES: tuple[tuple[frozenset[str], str], ...] = (
    (frozenset({"pdf"}), ".pdf"),
    (
        frozenset(
            {
                "doc",
                "docx",
                "msword",
                "word",
                "application/msword",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            }
        ),
        ".docx",
    ),
    (
        frozenset(
            {
                "ppt",
                "pptx",
                "powerpoint",
                "application/vnd.ms-powerpoint",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            }
        ),
        ".pptx",
    ),
    (frozenset({"image", "png", "jpg", "jpeg", "webp", "bmp"}), ".png"),
    (frozenset({"html", "url", "web_url", "text/html"}), ".html"),
)

_HEADING = re.compile(r"^(#{1,6})\s+(\S.*)$")
_NUMBERED = re.compile(r"^(\d+(?:\.\d+)*)(?:[.)]\s+|\s+)(\S.*)$")
_SENTENCE_END = ("。", "！", "？", "；", "，", ",", ".", "!", "?", ";", ":")


def media_suffix(media_kind: str) -> str:
    kind = media_kind.strip().casefold()
    for names, suffix in _MEDIA_SUFFIXES:
        if kind in names or kind.startswith("image/"):
            return suffix
    return ".bin"


def _walk_scores(value: Any) -> list[float]:
    scores: list[float] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key == "score":
                try:
                    scores.append(float(child))
                except (TypeError, ValueError):
                    pass
            else:
                scores.extend(_walk_scores(child))
    elif isinstance(value, list):
        for child in value:
            scores.extend(_walk_scores(child))
    return scores


def _page_scores(middle: Mapping[str, Any], page_count: int) -> dict[int, float]:
    """Aggregate MinerU middle.json block scores to one minimum score per page."""

    pages = middle.get("pdf_info")
    result: dict[int, float] = {}
    if isinstance(pages, list):
        for index, page in enumerate(pages, start=1):
            scores = _walk_scores(page)
            if scores:
                result[index] = min(scores)
    if not result and isinstance(middle.get("score"), (int, float, str)):
        try:
            result[1] = float(middle["score"])
        except (TypeError, ValueError):
            pass
    del page_count
    return result


def _signal_text(line: str) -> str:
    value = line.strip()
    value = re.sub(r"^#{1,6}\s+", "", value)
    value = re.sub(r"^(\d+(?:\.\d+)*)(?:[.)]\s+|\s+)", "", value)
    return " ".join(value.casefold().split())


def _has_title_shape(value: str) -> bool:
    if any("\u4e00" <= character <= "\u9fff" for character in value):
        return not any(character.isspace() for character in value) and len(value) <= 30
    words = value.split()
    return bool(words) and all(
        word == word.upper() or (word[0].isalpha() and word[0].isupper()) for word in words
    )


def _walk_scored_text(value: Any) -> list[tuple[str, float]]:
    scored: list[tuple[str, float]] = []
    if isinstance(value, Mapping):
        raw_score = value.get("score")
        if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool):
            for field in ("text", "content"):
                if isinstance(value.get(field), str) and value[field].strip():
                    scored.append((_signal_text(str(value[field])), float(raw_score)))
        for child in value.values():
            scored.extend(_walk_scored_text(child))
    elif isinstance(value, list):
        for child in value:
            scored.extend(_walk_scored_text(child))
    return scored


def signal_confidences(markdown: str, middle: Mapping[str, Any]) -> dict[str, float]:
    """Map structure-line text to the local middle.json score that produced it."""

    signal_text = {_signal_text(str(signal["text"])) for signal in structure_signals(markdown)}
    return {text: score for text, score in _walk_scored_text(middle) if text in signal_text}


def structure_signals(markdown: str) -> list[dict[str, Any]]:
    """Return machine-readable heading signals used by structure classification."""

    signals: list[dict[str, Any]] = []
    lines = markdown.splitlines()
    for index, line in enumerate(lines):
        line_number = index + 1
        heading = _HEADING.match(line.strip())
        if heading:
            signals.append(
                {
                    "line": line_number,
                    "text": line.strip(),
                    "kind": "markdown_heading",
                    "level": len(heading.group(1)),
                }
            )
            continue
        numbered = _NUMBERED.match(line.strip())
        if numbered:
            signals.append(
                {
                    "line": line_number,
                    "text": line.strip(),
                    "kind": "numbered_section",
                    "number": numbered.group(1),
                    "level": numbered.group(1).count(".") + 1,
                }
            )
            continue
        previous = lines[index - 1].strip() if index else ""
        following = lines[index + 1].strip() if index + 1 < len(lines) else ""
        value = line.strip()
        if (
            previous == ""
            and following == ""
            and 2 <= len(value) <= 80
            and not value.endswith(_SENTENCE_END)
            and not value.startswith(("#", "|", ">", "-", "*", "1.", "1)"))
            and any(character.isalpha() for character in value)
            and _has_title_shape(value)
        ):
            signals.append(
                {
                    "line": line_number,
                    "text": value,
                    "kind": "independent_title",
                    "level": 1,
                }
            )
    return signals


def classify_structure(
    markdown: str,
    *,
    sample_pages: Sequence[int],
    signal_scores: Mapping[str, float] | None = None,
    confidence_threshold: float = 0.9,
) -> dict[str, Any]:
    """Classify sampled markdown into ``tree`` / ``partial`` / ``basic``.

    A small number of signals is enough to count as structure. Full structure
    requires a consistent heading hierarchy; broken hierarchies (skipped levels
    or numbering gaps) keep physical order and use placeholder chapters.
    """

    signals = structure_signals(markdown)
    if not signals:
        return {
            "class": "basic",
            "signals": [],
            "sample_pages": list(sample_pages),
            "reason": "no_structure_signal",
            "signal_confidence": {},
            "signal_confidence_available": False,
            "signal_low_confidence": False,
            "confidence_threshold": float(confidence_threshold),
        }
    scores = signal_scores or {}
    normalized_scores = {
        _signal_text(str(signal["text"])): scores.get(_signal_text(str(signal["text"])))
        for signal in signals
    }
    known_scores = [value for value in normalized_scores.values() if value is not None]
    levels = [int(signal["level"]) for signal in signals]
    previous = 0
    hierarchy_ok = True
    for level in levels:
        if level > previous + 1:
            hierarchy_ok = False
            break
        previous = level
    numbers = [
        tuple(int(part) for part in str(signal["number"]).split("."))
        for signal in signals
        if signal["kind"] == "numbered_section"
    ]
    numbering_ok = True
    if numbers:
        expected = [1]
        for number in numbers:
            if number != expected[: len(number)]:
                numbering_ok = False
                break
            child = list(number) + [1]
            expected = child
    structure_class = "tree" if hierarchy_ok and numbering_ok else "partial"
    return {
        "class": structure_class,
        "signals": signals,
        "sample_pages": list(sample_pages),
        "reason": "structure_signal" if structure_class == "tree" else "incomplete_hierarchy",
        "signal_confidence": normalized_scores,
        "signal_confidence_available": bool(known_scores),
        "signal_low_confidence": any(
            value < confidence_threshold for value in known_scores if value is not None
        ),
        "confidence_threshold": float(confidence_threshold),
    }


def _sample_ranges(plan: OCRSamplePlan) -> list[tuple[int, int]]:
    if not plan.pages:
        return []
    ranges: list[tuple[int, int]] = []
    start = previous = plan.pages[0] - 1
    for page in plan.pages[1:]:
        zero_based = page - 1
        if zero_based == previous + 1:
            previous = zero_based
            continue
        ranges.append((start, previous))
        start = previous = zero_based
    ranges.append((start, previous))
    return ranges


def _load_json(path: Path) -> Mapping[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, Mapping) else None


def _walk_blocks(value: Any) -> Iterator[Mapping[str, Any]]:
    """Yield block-like mappings (carry a ``type`` key) from a page tree."""

    if isinstance(value, Mapping):
        if "type" in value:
            yield value
        else:
            for child in value.values():
                yield from _walk_blocks(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_blocks(child)


def _block_texts(block: Mapping[str, Any]) -> list[str]:
    """Line-span text content of one block, joined per line."""

    texts: list[str] = []
    lines = block.get("lines")
    if isinstance(lines, list):
        for line in lines:
            if isinstance(line, Mapping):
                spans = line.get("spans")
                if isinstance(spans, list):
                    parts = [
                        str(span.get("content") or span.get("text") or "").strip()
                        for span in spans
                        if isinstance(span, Mapping)
                    ]
                    line_text = " ".join(part for part in parts if part)
                    if line_text:
                        texts.append(line_text)
    return texts


def _page_facts(middle: Mapping[str, Any]) -> tuple[dict[int, str], dict[int, int]]:
    """Per-page reconstructed text and figure-block counts from middle.json.

    tolerant walk：``pdf_info`` 页树下的块按 ``type`` 判定；``image/figure``
    块计数（B4 内嵌图片事实），其余块的行 span 文本拼接为页文本（B9 真字符
    偏移的页内坐标系）。MinerU 版本布局差异由递归遍历吸收。
    """

    texts: dict[int, str] = {}
    figures: dict[int, int] = {}
    pages = middle.get("pdf_info")
    if not isinstance(pages, list):
        return texts, figures
    for page_index, page in enumerate(pages, start=1):
        if not isinstance(page, Mapping):
            continue
        parts: list[str] = []
        figure_count = 0
        for block in _walk_blocks(page):
            block_type = str(block.get("type", "")).strip().casefold()
            if block_type in {"image", "figure", "img", "image_body"}:
                figure_count += 1
                continue
            parts.extend(_block_texts(block))
        if parts:
            texts[page_index] = "\n".join(parts)
        if figure_count:
            figures[page_index] = figure_count
    return texts, figures


class _HtmlTableParser(HTMLParser):
    """Outer-table text extraction for MinerU table-body html payloads."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._table_depth = 0
        self._cells: list[str] | None = None
        self._cell: list[str] | None = None

    def _flush_cell(self) -> None:
        if self._cell is not None and self._cells is not None:
            self._cells.append(" ".join("".join(self._cell).split()))
        self._cell = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag == "table":
            self._table_depth += 1
        elif self._table_depth == 1 and tag == "tr":
            if self._cells is not None and self._cells:
                self.rows.append(self._cells)  # tolerate a missing </tr>
            self._cells = []
        elif self._table_depth == 1 and tag in {"td", "th"} and self._cells is not None:
            self._flush_cell()

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._table_depth:
            self._table_depth -= 1
        elif self._table_depth == 1 and tag in {"td", "th"}:
            self._flush_cell()
        elif self._table_depth == 1 and tag == "tr" and self._cells is not None:
            if self._cells:
                self.rows.append(self._cells)
            self._cells = None

    def handle_data(self, data: str) -> None:
        if self._cell is None and self._cells is not None and self._table_depth == 1:
            self._cell = []  # tolerate a missing opening cell tag
        if self._cell is not None:
            self._cell.append(data)


def _html_table_rows(html: str) -> tuple[list[str], list[list[str]]] | None:
    """Parse an html table body into (headers, rows); first row becomes headers."""

    parser = _HtmlTableParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return None
    if not parser.rows:
        return None
    return parser.rows[0], parser.rows[1:]


def _table_htmls(node: Any) -> list[str]:
    """Html payloads of table-typed spans anywhere below ``node``.

    middle.json 携带 html 的位置随版本漂移（L1 表块 lines 直接挂 span、L2
    ``table_body`` 块内 span、或 content_list 风格的 ``table_body`` 字符串）；
    统一按「type=table 且携带 <table> 的 span/字符串」递归收集。
    """

    htmls: list[str] = []
    if isinstance(node, Mapping):
        node_type = str(node.get("type", "")).strip().casefold()
        content = node.get("html") or node.get("content") or node.get("table_body")
        if node_type == "table" and isinstance(content, str) and "<table" in content.casefold():
            return [content]
        for child in node.values():
            htmls.extend(_table_htmls(child))
    elif isinstance(node, list):
        for child in node:
            htmls.extend(_table_htmls(child))
    return htmls


def _table_caption(node: Any) -> str:
    if isinstance(node, Mapping):
        raw = node.get("table_caption")
        if isinstance(raw, list):
            parts = [
                str(item).strip() for item in raw if isinstance(item, str) and str(item).strip()
            ]
            if parts:
                return " ".join(parts)
        if str(node.get("type", "")).strip().casefold() == "table_caption":
            texts = _block_texts(node)
            if texts:
                return " ".join(texts)
        for child in node.values():
            nested = _table_caption(child)
            if nested:
                return nested
        return ""
    if isinstance(node, list):
        for child in node:
            nested = _table_caption(child)
            if nested:
                return nested
    return ""


def _page_tables(middle: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Structured tables from middle.json table blocks, in document order.

    middle.json 在页级 ``tables`` 便捷列表与 ``para_blocks``/``preproc_blocks``
    中重复携带同一表块；按 html 表体去重。表体不可解析或无行时跳过——不产出
    部分成功的事实。
    """

    tables: list[dict[str, Any]] = []
    seen: set[str] = set()
    pages = middle.get("pdf_info")
    if not isinstance(pages, list):
        return tables
    for page_index, page in enumerate(pages, start=1):
        if not isinstance(page, Mapping):
            continue
        for block in _walk_blocks(page):
            if str(block.get("type", "")).strip().casefold() != "table":
                continue
            htmls = _table_htmls(block)
            if not htmls or htmls[0] in seen:
                continue
            seen.add(htmls[0])
            parsed = _html_table_rows(htmls[0])
            if parsed is None:
                continue
            headers, rows = parsed
            tables.append(
                {
                    "page": page_index,
                    "page_start": page_index,
                    "page_end": page_index,
                    "title": _table_caption(block),
                    "headers": headers,
                    "rows": rows,
                }
            )
    return tables


class MinerURun:
    """Parsed facts from one MinerU output directory."""

    def __init__(self, output_dir: Path, *, page_count: int) -> None:
        markdown_files = sorted(output_dir.rglob("*.md"))
        middle_files = sorted(output_dir.rglob("middle.json"))
        if not markdown_files:
            raise PlatformError(
                "mineru_parse_failed", "MinerU produced no markdown output", {}, 422
            )
        self.markdown = markdown_files[0].read_text(encoding="utf-8", errors="strict")
        self.middle: Mapping[str, Any] = {}
        if middle_files:
            loaded = _load_json(middle_files[0])
            if loaded is None:
                raise PlatformError(
                    "mineru_parse_failed", "MinerU middle.json could not be parsed", {}, 422
                )
            self.middle = loaded
        self.page_count = max(page_count, len(self.middle.get("pdf_info") or ()), 1)
        self.ocr_confidence_by_page = _page_scores(self.middle, self.page_count)


class MinerUAdapter:
    """Real MinerU pipeline transport (development fakes inject ``runner``)."""

    backend = "pipeline"

    def __init__(
        self,
        *,
        executable: str = "mineru",
        timeout_seconds: int = 900,
        confidence_threshold: float = 0.9,
        usage_sink: _UsageSink | None = None,
        runner: _Runner | None = None,
    ) -> None:
        self._executable = executable
        self._timeout_seconds = int(timeout_seconds)
        self._confidence_threshold = float(confidence_threshold)
        self._usage_sink = usage_sink
        self._runner = runner or subprocess.run

    def _run(
        self,
        source: Path,
        output_dir: Path,
        *,
        start: int | None = None,
        end: int | None = None,
    ) -> MinerURun:
        command: list[str] = [
            self._executable,
            "-p",
            str(source),
            "-o",
            str(output_dir),
            "-b",
            self.backend,
        ]
        if start is not None and end is not None:
            command.extend(["-s", str(start), "-e", str(end)])
        try:
            completed = self._runner(  # type: ignore[call-arg]
                command,
                capture_output=True,
                timeout=self._timeout_seconds,
                check=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise PlatformError(
                "mineru_parse_failed", "MinerU pipeline timed out", {}, 422
            ) from exc
        except (subprocess.CalledProcessError, OSError) as exc:
            raise PlatformError("mineru_parse_failed", "MinerU pipeline failed", {}, 422) from exc
        del completed
        try:
            return MinerURun(output_dir, page_count=0)
        except PlatformError:
            raise
        except Exception as exc:
            raise PlatformError(
                "mineru_parse_failed", "MinerU output could not be read", {}, 422
            ) from exc

    def parse(
        self,
        content: bytes,
        *,
        media_kind: str = "",
        page_count: int = 0,
        source_url: str | None = None,
    ) -> dict[str, Any]:
        suffix = media_suffix(media_kind or (source_url or ""))
        with tempfile.TemporaryDirectory(prefix="ragqs-mineru-") as work:
            work_dir = Path(work)
            source = work_dir / f"input{suffix}"
            source.write_bytes(content)
            sample_dir = work_dir / "sample"
            sample_dir.mkdir()
            plan = (
                OCRSamplePlan.for_page_count(page_count) if page_count else OCRSamplePlan(1, (1,))
            )
            sample_markdown: list[str] = []
            sample_scores: dict[str, float] = {}
            sample_started = time.monotonic()
            for start, end in _sample_ranges(plan):
                run = self._run(source, sample_dir / f"{start}-{end}", start=start, end=end)
                sample_markdown.append(run.markdown)
                sample_scores.update(signal_confidences(run.markdown, run.middle))
            sample_ms = int((time.monotonic() - sample_started) * 1000)
            sampled = "\n\n".join(sample_markdown)
            structure = classify_structure(
                sampled,
                sample_pages=list(plan.pages),
                signal_scores=sample_scores,
                confidence_threshold=self._confidence_threshold,
            )
            full_started = time.monotonic()
            full = self._run(source, work_dir / "full")
            full_ms = int((time.monotonic() - full_started) * 1000)
        facts = self._facts(full, structure, page_count=page_count)
        usage = {
            "kind": "local_usage",
            "stage": "mineru_pipeline",
            "page_count": int(facts["page_count"]),
            "sample_pages": len(plan.pages),
            "cpu_milliseconds": None,
            "gpu_milliseconds": None,
        }
        facts["usage"] = usage
        facts["timings"] = {"sample_ms": sample_ms, "full_ms": full_ms}
        if self._usage_sink is not None:
            self._usage_sink(usage)
        return facts

    def _facts(
        self, run: MinerURun, structure: Mapping[str, Any], *, page_count: int
    ) -> dict[str, Any]:
        page_scores = run.ocr_confidence_by_page
        scores = sorted(page_scores.values())
        page_texts, page_figures = _page_facts(run.middle)
        page_table_facts = _page_tables(run.middle)
        # B9 真字符偏移：span 为页内字符范围（页文本重建坐标系）；页文本缺失
        # （扫描页/无文本层）不合成假偏移——span=None 由消费端省略。
        chunks = [
            {
                "page": page,
                "span": (
                    f"0:{len(page_texts[page])}"
                    if page in page_texts and page_texts[page].strip()
                    else None
                ),
            }
            for page in sorted(page_scores) or [1]
        ]
        return {
            "text": run.markdown,
            "page_count": max(page_count, run.page_count, 1),
            "has_text_layer": bool(run.markdown.strip()),
            "chunks": chunks,
            "ocr_confidence": scores[0] if scores else None,
            "ocr_confidence_by_page": page_scores,
            "structure": dict(structure),
            "backend": self.backend,
            "model_version": str(
                run.middle.get("model") or run.middle.get("model_version") or "pipeline"
            ),
            # B4 内嵌图片事实：figure 块计数（MinerU 为判定权威）。
            "image_count": sum(page_figures.values()),
            # B9 消费端精化：页重建文本供 snippet 定位（同页重复消歧）。
            "page_texts": {str(page): text for page, text in page_texts.items()},
            # 结构化表格事实（A60）：middle.json 表块 → headers/rows，供内嵌表格
            # 转 markdown 分支消费；无表块或表体不可解析时为空，行为与现状一致。
            "tables": page_table_facts,
        }

    def __call__(self, content: bytes) -> dict[str, Any]:
        return self.parse(content)


class MinerUImageOCR:
    """Standalone image OCR backed by the same local MinerU pipeline."""

    def __init__(
        self,
        adapter: MinerUAdapter | None = None,
        *,
        usage_sink: _UsageSink | None = None,
    ) -> None:
        self._adapter = adapter or MinerUAdapter(usage_sink=usage_sink)

    def __call__(self, content: bytes, context: Mapping[str, Any]) -> str:
        del context
        facts = self._adapter.parse(content, media_kind="image/png")
        return str(facts.get("text") or "").strip()


__all__ = [
    "MinerUAdapter",
    "MinerUImageOCR",
    "classify_structure",
    "media_suffix",
    "signal_confidences",
    "structure_signals",
]
