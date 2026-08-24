from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from app.platform.errors import PlatformError

RouteKind = Literal["no_rewrite", "rewrite", "split_subquestions", "hyde"]
ReturnGranularity = Literal["sub_chunk", "parent_document", "document_summary"]


_QUOTE_RE = re.compile(r"[\"“”'‘’][^\"“”'‘’]{1,120}[\"“”'‘’]")
_ORDINAL_RE = re.compile(r"第\s*\d+\s*(?:章|节|条|部分|卷|回)", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\b\d{2,}\b")
_YEAR_RE = re.compile(r"(?:19|20)\d{2}\s*[年/-]")
_DATE_RANGE_RE = re.compile(r"(?:19|20)\d{2}\s*[年/-]\s*(?:[1-9]|1[0-2])\s*[月/-]?", re.IGNORECASE)
_PRONOUN_RE = re.compile(r"(?:它|他|她|他们|它们|这个|那个|这些|那些|上述|该|此)")
_AGGREGATE_RE = re.compile(
    r"(?:总结|汇总|概括|概述|综述|摘要|summary|overview|summarize)", re.IGNORECASE
)
_HYDE_RE = re.compile(r"(?:hyde|假设(?:文档|答案|文本)?|用假设)", re.IGNORECASE)
_POLITE_PREFIX_RE = re.compile(
    r"^(?:请(?:帮|帮忙|帮我)?|麻烦你|帮我|could you|please)[,，。：:\s]+", re.IGNORECASE
)
_SPLIT_RE = re.compile(r"[？?;；]|(?:以及|并且|和呢)", re.IGNORECASE)
_PROPER_NOUN_RE = re.compile(
    r"[\u4e00-\u9fff]{2,8}(?:公司|协议|标准|规范|办法|条例|系统|平台|模型)"
)


@dataclass(frozen=True, slots=True)
class MetadataPrefilter:
    """Typed, ACL-narrowing-only metadata constraints extracted by rule routing."""

    published_from: str | None = None
    published_to: str | None = None
    ordinal_from: int | None = None
    ordinal_to: int | None = None
    document_types: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for value, name in (
            (self.published_from, "published_from"),
            (self.published_to, "published_to"),
        ):
            if value is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise PlatformError("validation_error", f"{name} must be an RFC3339 date", {}, 422)
        if (
            self.published_from is not None
            and self.published_to is not None
            and self.published_from > self.published_to
        ):
            raise PlatformError("validation_error", "metadata date range is invalid", {}, 422)
        for value, name in ((self.ordinal_from, "ordinal_from"), (self.ordinal_to, "ordinal_to")):
            if value is not None and value < 0:
                raise PlatformError("validation_error", f"{name} must be non-negative", {}, 422)
        if not all(isinstance(item, str) and item.strip() for item in self.document_types):
            raise PlatformError("validation_error", "document_types entries are invalid", {}, 422)

    def is_empty(self) -> bool:
        return not (
            self.published_from
            or self.published_to
            or self.ordinal_from is not None
            or self.ordinal_to is not None
            or self.document_types
        )

    def to_mapping(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "published_from": self.published_from,
            "published_to": self.published_to,
            "ordinal_from": self.ordinal_from,
            "ordinal_to": self.ordinal_to,
            "document_types": list(self.document_types),
        }
        return value

    @classmethod
    def from_value(cls, value: Any) -> MetadataPrefilter | None:
        if value is None:
            return None
        if isinstance(value, MetadataPrefilter):
            return value
        if not isinstance(value, Mapping):
            raise PlatformError("validation_error", "metadata prefilter is invalid", {}, 422)
        document_types = value.get("document_types", ())
        if not isinstance(document_types, (list, tuple)) or any(
            not isinstance(item, str) for item in document_types
        ):
            raise PlatformError("validation_error", "document_types entries are invalid", {}, 422)
        return cls(
            published_from=value.get("published_from"),
            published_to=value.get("published_to"),
            ordinal_from=value.get("ordinal_from"),
            ordinal_to=value.get("ordinal_to"),
            document_types=tuple(document_types),
        )

    def matches(self, metadata: Mapping[str, Any]) -> bool:
        """True when the chunk metadata satisfies every active constraint."""

        published = metadata.get("published_at") or metadata.get("published_date")
        if self.published_from or self.published_to:
            if published is None:
                return False
            published_value = str(published)[:10]
            if self.published_from and published_value < self.published_from:
                return False
            if self.published_to and published_value > self.published_to:
                return False
        ordinal = metadata.get("ordinal")
        if self.ordinal_from is not None:
            try:
                if ordinal is None or int(ordinal) < self.ordinal_from:
                    return False
            except (TypeError, ValueError):
                return False
        if self.ordinal_to is not None:
            try:
                if ordinal is None or int(ordinal) > self.ordinal_to:
                    return False
            except (TypeError, ValueError):
                return False
        if self.document_types:
            document_type = str(metadata.get("document_type") or "")
            if document_type not in self.document_types:
                return False
        return True


@dataclass(frozen=True, slots=True)
class Subquestion:
    id: str
    query: str


@dataclass(frozen=True, slots=True)
class RouteOutput:
    """Fixed, versionable rule-routing contract; no exclusive retrieval control."""

    kind: RouteKind
    original_query: str
    rewritten_query: str | None = None
    subquestions: tuple[Subquestion, ...] | None = None
    hyde_text: str | None = None
    return_granularity: ReturnGranularity = "parent_document"
    metadata_prefilter: MetadataPrefilter | None = None
    query_history_ref: str | None = None
    schema_version: str = "1"

    def __post_init__(self) -> None:
        if self.kind not in {"no_rewrite", "rewrite", "split_subquestions", "hyde"}:
            raise PlatformError("validation_error", "route kind is invalid", {}, 422)
        if self.return_granularity not in {"sub_chunk", "parent_document", "document_summary"}:
            raise PlatformError("validation_error", "return granularity is invalid", {}, 422)
        if not isinstance(self.original_query, str) or not self.original_query.strip():
            raise PlatformError("validation_error", "original query is required", {}, 422)
        if self.kind == "rewrite" and (
            not self.rewritten_query or not self.rewritten_query.strip()
        ):
            raise PlatformError("validation_error", "rewritten query is required", {}, 422)
        if self.kind != "rewrite" and self.rewritten_query is not None:
            raise PlatformError(
                "validation_error", "only rewrite may carry a rewritten query", {}, 422
            )
        if self.kind == "split_subquestions":
            if not self.subquestions or any(not item.query.strip() for item in self.subquestions):
                raise PlatformError("validation_error", "subquestions are invalid", {}, 422)
            ids = [item.id for item in self.subquestions]
            if len(ids) != len(set(ids)):
                raise PlatformError("validation_error", "subquestion ids must be unique", {}, 422)
        elif self.subquestions is not None:
            raise PlatformError("validation_error", "only split may carry subquestions", {}, 422)
        if self.kind == "hyde" and (not self.hyde_text or not self.hyde_text.strip()):
            raise PlatformError("validation_error", "hyde text is required", {}, 422)
        if self.kind != "hyde" and self.hyde_text is not None:
            raise PlatformError("validation_error", "only hyde may carry hypothesis text", {}, 422)

    def search_queries(self) -> tuple[str, ...]:
        """Queries that keep the default hybrid retrieval running additively."""

        if self.kind == "rewrite":
            return (self.rewritten_query or self.original_query,)
        if self.kind == "split_subquestions":
            assert self.subquestions is not None
            return tuple(item.query for item in self.subquestions)
        if self.kind == "hyde":
            return (self.hyde_text or self.original_query,)
        return (self.original_query,)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "original_query": self.original_query,
            "rewritten_query": self.rewritten_query,
            "subquestions": (
                None
                if self.subquestions is None
                else [{"id": item.id, "query": item.query} for item in self.subquestions]
            ),
            "hyde_text": self.hyde_text,
            "return_granularity": self.return_granularity,
            "metadata_prefilter": (
                None if self.metadata_prefilter is None else self.metadata_prefilter.to_mapping()
            ),
            "query_history_ref": self.query_history_ref,
        }


class RuleQueryRouter:
    """Deterministic, zero-LLM query routing; rules prefer recall over precision."""

    def route(
        self,
        query: str,
        *,
        history_ref: str | None = None,
        recent_queries: Sequence[str] = (),
    ) -> RouteOutput:
        if not isinstance(query, str) or not query.strip():
            raise PlatformError("validation_error", "query is required", {}, 422)
        normalized = " ".join(query.split())
        prefilter = self._metadata_prefilter(query)
        granularity = self._granularity(query)
        history = history_ref or (self._pronoun_history_ref(query, recent_queries))
        split_parts = self._split_parts(normalized)
        if len(split_parts) >= 2:
            subquestions = tuple(
                Subquestion(id=f"sq-{index + 1}", query=part)
                for index, part in enumerate(split_parts)
            )
            return RouteOutput(
                kind="split_subquestions",
                original_query=query,
                subquestions=subquestions,
                return_granularity=granularity,
                metadata_prefilter=prefilter,
                query_history_ref=history,
            )
        # HyDE is a deterministic retrieval-only hypothesis. It is deliberately
        # derived from the user's text and never enters the answer evidence path.
        if _HYDE_RE.search(normalized):
            return RouteOutput(
                kind="hyde",
                original_query=query,
                hyde_text=f"检索假设：{normalized}",
                return_granularity=granularity,
                metadata_prefilter=prefilter,
                query_history_ref=history,
            )
        rewritten = _POLITE_PREFIX_RE.sub("", normalized).strip()
        if rewritten and rewritten != query:
            return RouteOutput(
                kind="rewrite",
                original_query=query,
                rewritten_query=rewritten,
                return_granularity=granularity,
                metadata_prefilter=prefilter,
                query_history_ref=history,
            )
        return RouteOutput(
            kind="no_rewrite",
            original_query=query,
            return_granularity=granularity,
            metadata_prefilter=prefilter,
            query_history_ref=history,
        )

    @staticmethod
    def _granularity(query: str) -> ReturnGranularity:
        if _AGGREGATE_RE.search(query):
            return "document_summary"
        if _QUOTE_RE.search(query) or _PROPER_NOUN_RE.search(query) or _NUMBER_RE.search(query):
            return "sub_chunk"
        return "parent_document"

    @staticmethod
    def _metadata_prefilter(query: str) -> MetadataPrefilter | None:
        year = _YEAR_RE.search(query)
        ordinal = _ORDINAL_RE.search(query)
        if not year and not ordinal:
            return None
        prefilter = MetadataPrefilter()
        if year:
            year_value = int(re.search(r"(?:19|20)\d{2}", year.group()).group())
            return MetadataPrefilter(
                published_from=f"{year_value}-01-01",
                published_to=f"{year_value}-12-31",
            )
        if ordinal:
            ordinal_value = int(re.search(r"\d+", ordinal.group()).group())
            return MetadataPrefilter(ordinal_from=ordinal_value, ordinal_to=ordinal_value)
        return prefilter

    @staticmethod
    def _pronoun_history_ref(query: str, recent_queries: Sequence[str]) -> str | None:
        if _PRONOUN_RE.search(query) and (recent_queries or query.strip()):
            return "conversation_history"
        return None

    @staticmethod
    def _split_parts(query: str) -> list[str]:
        parts = [part.strip() for part in _SPLIT_RE.split(query)]
        return [part for part in parts if len(part) >= 4]
