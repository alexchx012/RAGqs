from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.engine import Connection

from app.platform.errors import PlatformError


@dataclass(frozen=True, slots=True)
class IndexStagingRequest:
    job_id: str
    attempt_id: str
    fencing_token: int
    publication_id: str
    document_id: str
    document_version_id: str
    space_id: str
    operation: str
    base_active_version_id: str | None
    expected_generation_id: str
    index_revision_at_start: int
    object_manifest_ref: str
    processing_config_snapshot: Mapping[str, Any]
    authorization_fence: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.authorization_fence, Mapping) or not self.authorization_fence:
            raise PlatformError("validation_error", "Index staging request is invalid", {}, 422)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "attempt_id": self.attempt_id,
            "fencing_token": self.fencing_token,
            "publication_id": self.publication_id,
            "document_id": self.document_id,
            "document_version_id": self.document_version_id,
            "space_id": self.space_id,
            "operation": self.operation,
            "base_active_version_id": self.base_active_version_id,
            "expected_generation_id": self.expected_generation_id,
            "index_revision_at_start": self.index_revision_at_start,
            "object_manifest_ref": self.object_manifest_ref,
            "processing_config_snapshot": dict(self.processing_config_snapshot),
            "authorization_fence": dict(self.authorization_fence),
        }


@dataclass(frozen=True, slots=True)
class IndexProcessingReceipt:
    job_id: str
    attempt_id: str
    fencing_token: int
    publication_id: str
    document_id: str
    document_version_id: str
    input_content_hash: str
    stage_resources: tuple[Mapping[str, Any], ...]
    processing_config_version: str
    generation_id: str
    authorization_fence: Mapping[str, Any]
    model_version: str
    prompt_version: str
    processing_summary: Mapping[str, Any]
    locator_snippet_integrity: Mapping[str, Any]
    index_component_results: Mapping[str, Any]
    content_manifest_id: str
    content_manifest_hash: str
    failure: Mapping[str, Any] | None
    degradations: tuple[Mapping[str, Any], ...]
    ocr_low_confidence: bool = False
    ocr_low_confidence_fact: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        required_strings = (
            self.job_id,
            self.attempt_id,
            self.publication_id,
            self.document_id,
            self.document_version_id,
            self.input_content_hash,
            self.processing_config_version,
            self.generation_id,
            self.model_version,
            self.prompt_version,
            self.content_manifest_id,
            self.content_manifest_hash,
        )
        summary_counts = ("chunk_count", "page_count", "image_count", "table_count")
        summary_facts = ("ocr", "tree", "cr")
        if (
            self.fencing_token < 1
            or any(not isinstance(value, str) or not value.strip() for value in required_strings)
            or any(
                not isinstance(resource, Mapping)
                or not isinstance(resource.get("backend_kind"), str)
                or not resource["backend_kind"].strip()
                or not isinstance(resource.get("resource_id"), str)
                or not resource["resource_id"].strip()
                for resource in self.stage_resources
            )
            or not isinstance(self.processing_summary, Mapping)
            or any(
                isinstance(self.processing_summary.get(field), bool)
                or not isinstance(self.processing_summary.get(field), int)
                or int(self.processing_summary[field]) < 0
                for field in summary_counts
            )
            or any(
                not isinstance(self.processing_summary.get(field), Mapping)
                for field in summary_facts
            )
            or not isinstance(self.locator_snippet_integrity, Mapping)
            or not self.locator_snippet_integrity
            or not isinstance(self.index_component_results, Mapping)
            or not self.index_component_results
            or (self.failure is not None and not isinstance(self.failure, Mapping))
            or not isinstance(self.authorization_fence, Mapping)
            or not self.authorization_fence
            or any(not isinstance(degradation, Mapping) for degradation in self.degradations)
            or not isinstance(self.ocr_low_confidence, bool)
            or (self.ocr_low_confidence and not isinstance(self.ocr_low_confidence_fact, Mapping))
        ):
            raise PlatformError("validation_error", "Processing receipt is invalid", {}, 422)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> IndexProcessingReceipt:
        try:
            stage_resources = value["stage_resources"]
            degradations = value["degradations"]
            processing_summary = value["processing_summary"]
            failure = value["failure"]
            if not isinstance(stage_resources, (list, tuple)) or not isinstance(
                degradations, (list, tuple)
            ):
                raise TypeError
            return cls(
                job_id=value["job_id"],
                attempt_id=value["attempt_id"],
                fencing_token=value["fencing_token"],
                publication_id=value["publication_id"],
                document_id=value["document_id"],
                document_version_id=value["document_version_id"],
                input_content_hash=value["input_content_hash"],
                stage_resources=tuple(dict(resource) for resource in stage_resources),
                processing_config_version=value["processing_config_version"],
                generation_id=value["generation_id"],
                authorization_fence=dict(value["authorization_fence"]),
                model_version=value["model_version"],
                prompt_version=value["prompt_version"],
                processing_summary=dict(processing_summary),
                locator_snippet_integrity=dict(value["locator_snippet_integrity"]),
                index_component_results=dict(value["index_component_results"]),
                content_manifest_id=value["content_manifest_id"],
                content_manifest_hash=value["content_manifest_hash"],
                failure=dict(failure) if isinstance(failure, Mapping) else failure,
                degradations=tuple(dict(degradation) for degradation in degradations),
                ocr_low_confidence=bool(value.get("ocr_low_confidence", False)),
                ocr_low_confidence_fact=(
                    dict(value["ocr_low_confidence_fact"])
                    if value.get("ocr_low_confidence_fact") is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PlatformError(
                "validation_error", "Processing receipt is invalid", {}, 422
            ) from exc

    def validate_against(self, request: IndexStagingRequest) -> None:
        fields = (
            "job_id",
            "attempt_id",
            "fencing_token",
            "publication_id",
            "document_id",
            "document_version_id",
        )
        if any(getattr(self, field) != getattr(request, field) for field in fields):
            raise PlatformError(
                "processing_receipt_conflict",
                "Processing receipt does not match the staging request",
                {},
                409,
            )
        if dict(self.authorization_fence) != dict(request.authorization_fence):
            raise PlatformError(
                "processing_receipt_conflict",
                "Processing receipt does not match the staging request",
                {},
                409,
            )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "attempt_id": self.attempt_id,
            "fencing_token": self.fencing_token,
            "publication_id": self.publication_id,
            "document_id": self.document_id,
            "document_version_id": self.document_version_id,
            "input_content_hash": self.input_content_hash,
            "stage_resources": [dict(item) for item in self.stage_resources],
            "processing_config_version": self.processing_config_version,
            "generation_id": self.generation_id,
            "authorization_fence": dict(self.authorization_fence),
            "model_version": self.model_version,
            "prompt_version": self.prompt_version,
            "processing_summary": dict(self.processing_summary),
            "locator_snippet_integrity": dict(self.locator_snippet_integrity),
            "index_component_results": dict(self.index_component_results),
            "content_manifest_id": self.content_manifest_id,
            "content_manifest_hash": self.content_manifest_hash,
            "failure": dict(self.failure) if self.failure is not None else None,
            "degradations": [dict(item) for item in self.degradations],
            "ocr_low_confidence": self.ocr_low_confidence,
            "ocr_low_confidence_fact": (
                dict(self.ocr_low_confidence_fact)
                if self.ocr_low_confidence_fact is not None
                else None
            ),
        }


class IndexingHandoffPort(Protocol):
    def publish(self, request: IndexStagingRequest, *, connection: Connection) -> Any: ...

    def discard(self, request: IndexStagingRequest, *, connection: Connection) -> Any: ...

    def cleanup_resource(self, resource: Mapping[str, Any], *, connection: Connection) -> Any: ...


class NoopIndexingHandoff:
    """Explicit development adapter: providers are external to documents."""

    def publish(self, request: IndexStagingRequest, *, connection: Connection) -> Mapping[str, str]:
        del request, connection
        return {"state": "published"}

    def discard(self, request: IndexStagingRequest, *, connection: Connection) -> None:
        del request, connection

    def cleanup_resource(self, resource: Mapping[str, Any], *, connection: Connection) -> None:
        del resource, connection


__all__ = [
    "IndexProcessingReceipt",
    "IndexStagingRequest",
    "IndexingHandoffPort",
    "NoopIndexingHandoff",
]
