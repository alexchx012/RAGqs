from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
    input_manifest_hash: str
    processing_profile_version: str
    usage_ownership: Mapping[str, Any] | None = None
    usage_deadline_at_utc: datetime | None = None
    usage_replay_generation: int = 0

    def __post_init__(self) -> None:
        required = (
            self.job_id,
            self.attempt_id,
            self.publication_id,
            self.document_id,
            self.document_version_id,
            self.space_id,
            self.operation,
            self.expected_generation_id,
            self.object_manifest_ref,
        )
        if (
            self.fencing_token < 1
            or self.index_revision_at_start < 0
            or any(not isinstance(value, str) or not value.strip() for value in required)
            or not isinstance(self.processing_config_snapshot, Mapping)
            or not isinstance(self.authorization_fence, Mapping)
            or not self.authorization_fence
            or not isinstance(self.input_manifest_hash, str)
            or not self.input_manifest_hash.strip()
            or not isinstance(self.processing_profile_version, str)
            or not self.processing_profile_version.strip()
            or ((self.usage_ownership is None) != (self.usage_deadline_at_utc is None))
            or (
                self.usage_ownership is not None
                and not isinstance(self.usage_ownership, Mapping)
            )
            or (
                self.usage_deadline_at_utc is not None
                and not isinstance(self.usage_deadline_at_utc, datetime)
            )
            or isinstance(self.usage_replay_generation, bool)
            or not isinstance(self.usage_replay_generation, int)
            or self.usage_replay_generation < 0
        ):
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
            "input_manifest_hash": self.input_manifest_hash,
            "processing_profile_version": self.processing_profile_version,
            "usage_ownership": (
                dict(self.usage_ownership) if self.usage_ownership is not None else None
            ),
            "usage_deadline_at_utc": (
                self.usage_deadline_at_utc.astimezone(UTC).isoformat()
                if self.usage_deadline_at_utc is not None
                else None
            ),
            "usage_replay_generation": self.usage_replay_generation,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> IndexStagingRequest:
        if not isinstance(value, Mapping):
            raise PlatformError("validation_error", "Index staging request is invalid", {}, 422)
        try:
            usage_ownership = value.get("usage_ownership")
            usage_deadline = value.get("usage_deadline_at_utc")
            usage_replay_generation = value.get("usage_replay_generation", 0)
            if isinstance(usage_replay_generation, bool):
                raise TypeError
            if usage_deadline is not None:
                if isinstance(usage_deadline, str):
                    usage_deadline = datetime.fromisoformat(usage_deadline)
                if not isinstance(usage_deadline, datetime):
                    raise TypeError
                usage_deadline = (
                    usage_deadline.replace(tzinfo=UTC)
                    if usage_deadline.tzinfo is None
                    else usage_deadline.astimezone(UTC)
                )
            return cls(
                job_id=str(value["job_id"]),
                attempt_id=str(value["attempt_id"]),
                fencing_token=int(value["fencing_token"]),
                publication_id=str(value["publication_id"]),
                document_id=str(value["document_id"]),
                document_version_id=str(value["document_version_id"]),
                space_id=str(value["space_id"]),
                operation=str(value["operation"]),
                base_active_version_id=value.get("base_active_version_id"),
                expected_generation_id=str(value["expected_generation_id"]),
                index_revision_at_start=int(value["index_revision_at_start"]),
                object_manifest_ref=str(value["object_manifest_ref"]),
                processing_config_snapshot=dict(value["processing_config_snapshot"]),
                authorization_fence=dict(value["authorization_fence"]),
                input_manifest_hash=str(value["input_manifest_hash"]),
                processing_profile_version=str(value["processing_profile_version"]),
                usage_ownership=(
                    dict(usage_ownership) if isinstance(usage_ownership, Mapping) else usage_ownership
                ),
                usage_deadline_at_utc=usage_deadline,
                usage_replay_generation=int(usage_replay_generation),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PlatformError(
                "validation_error", "Index staging request is invalid", {}, 422
            ) from exc


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
    input_manifest_hash: str
    processing_profile_version: str
    ocr_low_confidence: bool = False
    ocr_low_confidence_fact: Mapping[str, Any] | None = None
    stage_resource_ids: tuple[str, ...] = ()
    model_versions: Mapping[str, str] = field(default_factory=dict)
    prompt_versions: Mapping[str, str] = field(default_factory=dict)
    space_id: str | None = None
    operation: str | None = None
    base_active_version_id: str | None = None
    index_revision_at_start: int | None = None
    object_manifest_ref: str | None = None
    processing_config_snapshot: Mapping[str, Any] = field(default_factory=dict)

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
                or any(
                    not isinstance(resource.get(key), expected)
                    or (isinstance(resource.get(key), str) and not resource[key].strip())
                    for key, expected in (
                        ("backend_kind", str),
                        ("resource_id", str),
                        ("attempt_id", str),
                        ("publication_id", str),
                        ("document_id", str),
                        ("document_version_id", str),
                        ("generation_id", str),
                    )
                )
                or not isinstance(resource.get("fencing_token"), int)
                or resource["fencing_token"] < 1
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
            or not isinstance(self.input_manifest_hash, str)
            or not self.input_manifest_hash.strip()
            or not isinstance(self.processing_profile_version, str)
            or not self.processing_profile_version.strip()
            or any(
                not isinstance(resource_id, str) or not resource_id.strip()
                for resource_id in self.stage_resource_ids
            )
            or not isinstance(self.model_versions, Mapping)
            or any(
                not isinstance(name, str)
                or not name.strip()
                or not isinstance(version, str)
                or not version.strip()
                for name, version in self.model_versions.items()
            )
            or not isinstance(self.prompt_versions, Mapping)
            or any(
                not isinstance(name, str)
                or not name.strip()
                or not isinstance(version, str)
                or not version.strip()
                for name, version in self.prompt_versions.items()
            )
            or not isinstance(self.processing_config_snapshot, Mapping)
            or not isinstance(self.index_revision_at_start, int)
            or self.index_revision_at_start < 0
            or any(
                not isinstance(value, str) or not value.strip()
                for value in (self.space_id, self.operation, self.object_manifest_ref)
            )
            or (
                self.base_active_version_id is not None
                and (
                    not isinstance(self.base_active_version_id, str)
                    or not self.base_active_version_id.strip()
                )
            )
            or self.stage_resource_ids
            != tuple(str(resource["resource_id"]) for resource in self.stage_resources)
            or (int(self.processing_summary.get("chunk_count", 0)) > 0 and not self.stage_resources)
        ):
            raise PlatformError("validation_error", "Processing receipt is invalid", {}, 422)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> IndexProcessingReceipt:
        if not isinstance(value, Mapping):
            raise PlatformError("validation_error", "Processing receipt is invalid", {}, 422)
        try:
            required_echoes = (
                "space_id",
                "operation",
                "base_active_version_id",
                "index_revision_at_start",
                "object_manifest_ref",
                "processing_config_snapshot",
            )
            if any(key not in value for key in required_echoes):
                raise KeyError
            stage_resources = value.get("stage_resources")
            if stage_resources is None:
                stage_resources = []
            degradations = value.get("degradations", ())
            processing_summary = value["processing_summary"]
            failure = value.get("failure")
            model_versions = value.get("model_versions", {})
            prompt_versions = value.get("prompt_versions", {})
            if (
                not isinstance(stage_resources, (list, tuple))
                or not isinstance(degradations, (list, tuple))
                or not isinstance(model_versions, Mapping)
                or not isinstance(prompt_versions, Mapping)
            ):
                raise TypeError
            return cls(
                job_id=value["job_id"],
                attempt_id=value["attempt_id"],
                fencing_token=value["fencing_token"],
                publication_id=value["publication_id"],
                document_id=value["document_id"],
                document_version_id=value["document_version_id"],
                input_content_hash=value.get(
                    "input_content_hash", value.get("input_manifest_hash")
                ),
                stage_resources=tuple(dict(resource) for resource in stage_resources),
                processing_config_version=value.get(
                    "processing_config_version", value.get("processing_profile_version")
                ),
                generation_id=value["generation_id"],
                authorization_fence=dict(value["authorization_fence"]),
                model_version=value["model_version"],
                prompt_version=value["prompt_version"],
                processing_summary=dict(processing_summary),
                locator_snippet_integrity=dict(value["locator_snippet_integrity"]),
                index_component_results=dict(value["index_component_results"]),
                content_manifest_id=value["content_manifest_id"],
                content_manifest_hash=value.get(
                    "content_manifest_hash", value.get("chunk_manifest_hash")
                ),
                failure=dict(failure) if isinstance(failure, Mapping) else failure,
                degradations=tuple(dict(degradation) for degradation in degradations),
                ocr_low_confidence=bool(value.get("ocr_low_confidence", False)),
                ocr_low_confidence_fact=(
                    dict(value["ocr_low_confidence_fact"])
                    if value.get("ocr_low_confidence_fact") is not None
                    else None
                ),
                input_manifest_hash=value["input_manifest_hash"],
                processing_profile_version=value["processing_profile_version"],
                stage_resource_ids=tuple(
                    str(resource_id) for resource_id in value.get("stage_resource_ids", ())
                ),
                model_versions={
                    str(name): str(version) for name, version in model_versions.items()
                },
                prompt_versions={
                    str(name): str(version) for name, version in prompt_versions.items()
                },
                space_id=value["space_id"],
                operation=value["operation"],
                base_active_version_id=value["base_active_version_id"],
                index_revision_at_start=value["index_revision_at_start"],
                object_manifest_ref=value["object_manifest_ref"],
                processing_config_snapshot=dict(value["processing_config_snapshot"]),
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
        if self.generation_id != request.expected_generation_id:
            raise PlatformError(
                "generation_conflict",
                "The processing receipt generation is no longer current",
                {},
                409,
            )
        echoed = (
            ("space_id", request.space_id),
            ("operation", request.operation),
            ("base_active_version_id", request.base_active_version_id),
            ("index_revision_at_start", request.index_revision_at_start),
            ("object_manifest_ref", request.object_manifest_ref),
        )
        if any(getattr(self, field) != expected for field, expected in echoed) or (
            dict(self.processing_config_snapshot) != dict(request.processing_config_snapshot)
        ):
            raise PlatformError(
                "processing_receipt_conflict",
                "Processing receipt does not match the staging request",
                {},
                409,
            )
        for resource in self.stage_resources:
            identity = (
                ("attempt_id", request.attempt_id),
                ("publication_id", request.publication_id),
                ("fencing_token", request.fencing_token),
                ("document_id", request.document_id),
                ("document_version_id", request.document_version_id),
                ("generation_id", request.expected_generation_id),
            )
            if any(resource[key] != expected for key, expected in identity):
                raise PlatformError(
                    "processing_receipt_conflict",
                    "Processing receipt does not match the staging request",
                    {},
                    409,
                )
        expected_resource_ids = tuple(
            str(resource["resource_id"]) for resource in self.stage_resources
        )
        if (
            self.stage_resource_ids != expected_resource_ids
            or int(self.processing_summary.get("chunk_count", 0)) != len(self.stage_resources)
            or self.input_manifest_hash != request.input_manifest_hash
            or self.processing_profile_version != request.processing_profile_version
        ):
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
            "input_manifest_hash": self.input_manifest_hash,
            "processing_profile_version": self.processing_profile_version,
            "stage_resource_ids": list(self.stage_resource_ids),
            "model_versions": dict(self.model_versions),
            "prompt_versions": dict(self.prompt_versions),
            "space_id": self.space_id,
            "operation": self.operation,
            "base_active_version_id": self.base_active_version_id,
            "index_revision_at_start": self.index_revision_at_start,
            "object_manifest_ref": self.object_manifest_ref,
            "processing_config_snapshot": dict(self.processing_config_snapshot),
        }


class IndexingHandoffPort(Protocol):
    def publish(
        self,
        request: IndexStagingRequest,
        *,
        connection: Connection,
        receipt: IndexProcessingReceipt | Mapping[str, Any] | None = None,
    ) -> Any: ...

    def discard(self, request: IndexStagingRequest, *, connection: Connection) -> Any: ...

    def cleanup_resource(
        self, resource: Mapping[str, Any], *, connection: Connection | None
    ) -> Any: ...


class NoopIndexingHandoff:
    """Explicit development adapter: providers are external to documents."""

    def publish(
        self,
        request: IndexStagingRequest,
        *,
        connection: Connection,
        receipt: IndexProcessingReceipt | Mapping[str, Any] | None = None,
    ) -> Mapping[str, str]:
        del request, connection, receipt
        return {"state": "published"}

    def discard(self, request: IndexStagingRequest, *, connection: Connection) -> None:
        del request, connection

    def cleanup_resource(
        self, resource: Mapping[str, Any], *, connection: Connection | None
    ) -> None:
        del resource, connection


__all__ = [
    "IndexProcessingReceipt",
    "IndexStagingRequest",
    "IndexingHandoffPort",
    "NoopIndexingHandoff",
]
