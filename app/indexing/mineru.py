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
from collections.abc import Callable, Mapping, Sequence
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
_NUMBERED = re.compile(r"^(\d+(?:\.\d+)*)([.)])\s+(\S.*)$")


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


def structure_signals(markdown: str) -> list[dict[str, Any]]:
    """Return machine-readable heading signals used by structure classification."""

    signals: list[dict[str, Any]] = []
    for line_number, line in enumerate(markdown.splitlines(), start=1):
        heading = _HEADING.match(line.strip())
        if heading:
            signals.append(
                {"line": line_number, "kind": "markdown_heading", "level": len(heading.group(1))}
            )
            continue
        numbered = _NUMBERED.match(line.strip())
        if numbered:
            signals.append(
                {
                    "line": line_number,
                    "kind": "numbered_section",
                    "number": numbered.group(1),
                    "level": numbered.group(1).count(".") + 1,
                }
            )
    return signals


def classify_structure(markdown: str, *, sample_pages: Sequence[int]) -> dict[str, Any]:
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
        }
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
    }


def _sample_ranges(plan: OCRSamplePlan) -> list[tuple[int, int]]:
    if not plan.pages:
        return []
    ranges: list[tuple[int, int]] = []
    start = previous = plan.pages[0]
    for page in plan.pages[1:]:
        if page == previous + 1:
            previous = page
            continue
        ranges.append((start, previous))
        start = previous = page
    ranges.append((start, previous))
    return ranges


def _load_json(path: Path) -> Mapping[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, Mapping) else None


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
        usage_sink: _UsageSink | None = None,
        runner: _Runner | None = None,
    ) -> None:
        self._executable = executable
        self._timeout_seconds = int(timeout_seconds)
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
            sample_started = time.monotonic()
            for start, end in _sample_ranges(plan):
                run = self._run(source, sample_dir / f"{start}-{end}", start=start, end=end)
                sample_markdown.append(run.markdown)
            sample_ms = int((time.monotonic() - sample_started) * 1000)
            sampled = "\n\n".join(sample_markdown)
            structure = classify_structure(sampled, sample_pages=list(plan.pages))
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
        chunks = [
            {"page": page, "span": f"{index}:{index + 1}"}
            for index, page in enumerate(sorted(page_scores) or [1], start=1)
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
    "structure_signals",
]
