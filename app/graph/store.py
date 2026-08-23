from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, Engine, and_, delete, select

from app.platform.errors import PlatformError

from .schema import graph_entities_table, graph_relations_table


def _now() -> datetime:
    return datetime.now(UTC)


def _required(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlatformError(
            "graph_provider_schema_invalid", f"Graph provider {field} is required", {}, 409
        )
    return value.strip()


def _confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlatformError(
            "graph_provider_schema_invalid", "Graph confidence must be numeric", {}, 409
        )
    result = float(value)
    if result < 0 or result > 1:
        raise PlatformError(
            "graph_provider_schema_invalid", "Graph confidence is out of range", {}, 409
        )
    return result


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:48]}"


class SqlAlchemyPublicGraphStore:
    """Generation-scoped public graph facts and the retrieval routing adapter."""

    def __init__(self, engine: Engine, *, now: Callable[[], datetime] = _now) -> None:
        self._engine = engine
        self._now = now

    def activate(
        self,
        *,
        connection: Connection,
        graph_build_id: str,
        graph_generation_id: str,
        index_generation_id: str,
        source_revision: int,
        source_head_fence: int,
        publications: Sequence[Mapping[str, str]],
        resources: Sequence[Mapping[str, Any]],
    ) -> None:
        publication_by_id = {
            _required(item.get("publication_id"), "publication_id"): dict(item)
            for item in publications
        }
        entity_rows: list[dict[str, Any]] = []
        relation_rows: list[dict[str, Any]] = []
        created_at = self._now()
        for resource in resources:
            if resource.get("resource_kind") != "publication_graph":
                raise PlatformError(
                    "graph_provider_schema_invalid", "Graph resource kind is invalid", {}, 409
                )
            publication_id = _required(resource.get("resource_id"), "resource_id")
            publication = publication_by_id.get(publication_id)
            if publication is None:
                raise PlatformError(
                    "graph_provider_schema_invalid",
                    "Graph resource is not in the frozen publication snapshot",
                    {},
                    409,
                )
            payload = resource.get("payload")
            graph = payload.get("graph") if isinstance(payload, Mapping) else None
            if not isinstance(graph, Mapping):
                raise PlatformError(
                    "graph_provider_schema_invalid", "Graph provider payload is invalid", {}, 409
                )
            source = graph.get("source")
            if not isinstance(source, Mapping) or any(
                _required(source.get(field), field) != publication[field]
                for field in ("document_id", "content_manifest_id")
            ):
                raise PlatformError(
                    "graph_provider_schema_invalid",
                    "Graph provider provenance does not match the frozen publication",
                    {},
                    409,
                )
            nodes = graph.get("nodes")
            edges = graph.get("edges")
            if not isinstance(nodes, list) or not isinstance(edges, list):
                raise PlatformError(
                    "graph_provider_schema_invalid", "Graph nodes and edges must be arrays", {}, 409
                )
            node_metadata: dict[str, tuple[Mapping[str, Any], str, str]] = {}
            for raw_node in nodes:
                if not isinstance(raw_node, Mapping):
                    raise PlatformError(
                        "graph_provider_schema_invalid", "Graph node is invalid", {}, 409
                    )
                canonical_key = _required(raw_node.get("canonical_key"), "canonical_key")
                locator = raw_node.get("chunk_locator")
                aliases = raw_node.get("aliases")
                if not isinstance(locator, Mapping) or not locator:
                    raise PlatformError(
                        "graph_provider_schema_invalid", "Graph chunk locator is required", {}, 409
                    )
                if not isinstance(aliases, list) or any(
                    not isinstance(alias, str) or not alias.strip() for alias in aliases
                ):
                    raise PlatformError(
                        "graph_provider_schema_invalid", "Graph aliases are invalid", {}, 409
                    )
                model_revision = _required(
                    raw_node.get("extraction_model_revision"), "extraction_model_revision"
                )
                prompt_revision = _required(raw_node.get("prompt_revision"), "prompt_revision")
                node_metadata[canonical_key] = (locator, model_revision, prompt_revision)
                entity_rows.append(
                    {
                        "id": _stable_id(
                            "graph_entity",
                            graph_generation_id,
                            canonical_key,
                            publication_id,
                            publication["document_version_id"],
                        ),
                        "graph_generation_id": graph_generation_id,
                        "index_generation_id": index_generation_id,
                        "source_revision": source_revision,
                        "source_head_fence": source_head_fence,
                        "space_id": "public",
                        "canonical_key": canonical_key,
                        "entity_type": _required(raw_node.get("entity_type"), "entity_type"),
                        "display_name": _required(raw_node.get("display_name"), "display_name"),
                        "aliases_json": [alias.strip() for alias in aliases],
                        "document_id": publication["document_id"],
                        "document_version_id": publication["document_version_id"],
                        "publication_id": publication_id,
                        "content_manifest_id": publication["content_manifest_id"],
                        "chunk_locator_json": dict(locator),
                        "extraction_model_revision": model_revision,
                        "prompt_revision": prompt_revision,
                        "confidence": _confidence(raw_node.get("confidence")),
                        "graph_build_id": graph_build_id,
                        "created_at_utc": created_at,
                    }
                )
            fallback_metadata = next(iter(node_metadata.values()), None)
            for raw_edge in edges:
                if not isinstance(raw_edge, Mapping) or fallback_metadata is None:
                    raise PlatformError(
                        "graph_provider_schema_invalid", "Graph relation is invalid", {}, 409
                    )
                source_key = _required(raw_edge.get("source_key"), "source_key")
                target_key = _required(raw_edge.get("target_key"), "target_key")
                locator, model_revision, prompt_revision = node_metadata.get(
                    source_key, fallback_metadata
                )
                properties = raw_edge.get("properties", {})
                if not isinstance(properties, Mapping) or not isinstance(
                    raw_edge.get("directed"), bool
                ):
                    raise PlatformError(
                        "graph_provider_schema_invalid",
                        "Graph relation fields are invalid",
                        {},
                        409,
                    )
                relation_type = _required(raw_edge.get("relation_type"), "relation_type")
                relation_rows.append(
                    {
                        "id": _stable_id(
                            "graph_relation",
                            graph_generation_id,
                            source_key,
                            target_key,
                            relation_type,
                            publication_id,
                            publication["document_version_id"],
                        ),
                        "graph_generation_id": graph_generation_id,
                        "index_generation_id": index_generation_id,
                        "source_revision": source_revision,
                        "source_head_fence": source_head_fence,
                        "space_id": "public",
                        "source_canonical_key": source_key,
                        "target_canonical_key": target_key,
                        "relation_type": relation_type,
                        "directed": raw_edge["directed"],
                        "properties_json": dict(properties),
                        "document_id": publication["document_id"],
                        "document_version_id": publication["document_version_id"],
                        "publication_id": publication_id,
                        "content_manifest_id": publication["content_manifest_id"],
                        "chunk_locator_json": dict(locator),
                        "extraction_model_revision": model_revision,
                        "prompt_revision": prompt_revision,
                        "confidence": _confidence(raw_edge.get("confidence")),
                        "graph_build_id": graph_build_id,
                        "created_at_utc": created_at,
                    }
                )
        connection.execute(
            delete(graph_relations_table).where(
                graph_relations_table.c.graph_generation_id == graph_generation_id
            )
        )
        connection.execute(
            delete(graph_entities_table).where(
                graph_entities_table.c.graph_generation_id == graph_generation_id
            )
        )
        if entity_rows:
            connection.execute(graph_entities_table.insert(), entity_rows)
        if relation_rows:
            connection.execute(graph_relations_table.insert(), relation_rows)

    def route(
        self,
        query: str,
        candidates: Sequence[Any],
        *,
        rag_call_limit: int,
        reader_lease: Any,
    ) -> Mapping[str, Any]:
        if (
            getattr(reader_lease, "component_kind", None) != "public_graph"
            or getattr(reader_lease, "expires_at", self._now()) <= self._now()
        ):
            raise PlatformError(
                "graph_unavailable", "Public graph reader lease is invalid", {}, 409
            )
        document_ids = tuple(
            dict.fromkeys(
                str(candidate.chunk.document_id)
                for candidate in candidates
                if getattr(getattr(candidate, "chunk", None), "document_id", None)
            )
        )
        if not document_ids:
            return {"entities": (), "relations": ()}
        limit = min(100, max(1, rag_call_limit) * 20)
        with self._engine.connect() as connection:
            entities = (
                connection.execute(
                    select(graph_entities_table).where(
                        and_(
                            graph_entities_table.c.index_generation_id
                            == str(reader_lease.generation_id),
                            graph_entities_table.c.source_head_fence
                            == int(reader_lease.source_head_fence),
                            graph_entities_table.c.space_id == "public",
                            graph_entities_table.c.document_id.in_(document_ids),
                        )
                    )
                )
                .mappings()
                .all()
            )
            normalized = query.casefold().strip()
            matched = [
                row
                for row in entities
                if not normalized
                or normalized in str(row["canonical_key"]).casefold()
                or normalized in str(row["display_name"]).casefold()
                or any(normalized in str(alias).casefold() for alias in row["aliases_json"])
            ][:limit]
            keys = {str(row["canonical_key"]) for row in matched}
            relations = []
            if keys:
                relations = (
                    connection.execute(
                        select(graph_relations_table).where(
                            and_(
                                graph_relations_table.c.index_generation_id
                                == str(reader_lease.generation_id),
                                graph_relations_table.c.source_head_fence
                                == int(reader_lease.source_head_fence),
                                graph_relations_table.c.space_id == "public",
                                graph_relations_table.c.document_id.in_(document_ids),
                                graph_relations_table.c.source_canonical_key.in_(keys),
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
        return {
            "entities": tuple(
                {
                    "canonical_key": row["canonical_key"],
                    "entity_type": row["entity_type"],
                    "display_name": row["display_name"],
                    "aliases": tuple(row["aliases_json"]),
                    "document_id": row["document_id"],
                    "publication_id": row["publication_id"],
                    "confidence": row["confidence"],
                }
                for row in matched
            ),
            "relations": tuple(
                {
                    "source_key": row["source_canonical_key"],
                    "target_key": row["target_canonical_key"],
                    "relation_type": row["relation_type"],
                    "directed": row["directed"],
                    "confidence": row["confidence"],
                }
                for row in relations
            ),
        }

    def delete_document_version(
        self, index_generation_id: str, document_id: str, document_version_id: str
    ) -> int:
        with self._engine.begin() as connection:
            relations = connection.execute(
                delete(graph_relations_table).where(
                    and_(
                        graph_relations_table.c.index_generation_id == index_generation_id,
                        graph_relations_table.c.document_id == document_id,
                        graph_relations_table.c.document_version_id == document_version_id,
                    )
                )
            ).rowcount
            entities = connection.execute(
                delete(graph_entities_table).where(
                    and_(
                        graph_entities_table.c.index_generation_id == index_generation_id,
                        graph_entities_table.c.document_id == document_id,
                        graph_entities_table.c.document_version_id == document_version_id,
                    )
                )
            ).rowcount
        return int(relations or 0) + int(entities or 0)

    def delete_document(self, index_generation_id: str, document_id: str) -> int:
        with self._engine.begin() as connection:
            relations = connection.execute(
                delete(graph_relations_table).where(
                    and_(
                        graph_relations_table.c.index_generation_id == index_generation_id,
                        graph_relations_table.c.document_id == document_id,
                    )
                )
            ).rowcount
            entities = connection.execute(
                delete(graph_entities_table).where(
                    and_(
                        graph_entities_table.c.index_generation_id == index_generation_id,
                        graph_entities_table.c.document_id == document_id,
                    )
                )
            ).rowcount
        return int(relations or 0) + int(entities or 0)

    def purge_generation(self, index_generation_id: str) -> int:
        with self._engine.begin() as connection:
            relations = connection.execute(
                delete(graph_relations_table).where(
                    graph_relations_table.c.index_generation_id == index_generation_id
                )
            ).rowcount
            entities = connection.execute(
                delete(graph_entities_table).where(
                    graph_entities_table.c.index_generation_id == index_generation_id
                )
            ).rowcount
        return int(relations or 0) + int(entities or 0)


__all__ = ["SqlAlchemyPublicGraphStore"]
