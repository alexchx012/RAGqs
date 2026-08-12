from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
from typing import Any

from app.documents.indexing import IndexProcessingReceipt, IndexStagingRequest
from app.platform.errors import PlatformError
from app.platform.storage import ObjectStorePort

from .generation import GenerationManager
from .graph import GraphComponentCoordinator
from .models import NarrowingScope, RetrievalProfile, RetrievalResult
from .processing import ContentProcessor, ProcessingOutput
from .providers import (
    IndexWriter,
    InMemoryIndexWriter,
    InMemorySparseIndexProvider,
    SparseIndexProvider,
    build_sparse_provider,
)
from .retrieval import CitationService, NoopReranker, RetrievalService, ScoreReranker


class IndexingService:
    """Application-facing facade for processing, staging, publishing and retrieval."""

    def __init__(
        self,
        *,
        processor: ContentProcessor | None = None,
        dense_writer: IndexWriter | None = None,
        sparse_provider: SparseIndexProvider | None = None,
        generation_manager: GenerationManager | None = None,
        sparse_provider_name: str | None = None,
        identity_access: Any | None = None,
        visibility_facts: Any | None = None,
        source_service: Any | None = None,
        reranker: Any | None = None,
        environment: str = "test",
        profile_resolver: Any | None = None,
        tree_router: Any | None = None,
        graph_router: Any | None = None,
        token_counter: Any | None = None,
        object_store: ObjectStorePort | None = None,
    ) -> None:
        if environment == "production":
            if (
                dense_writer is None
                or sparse_provider is None
                or reranker is None
                or not callable(token_counter)
            ):
                raise RuntimeError(
                    "production requires explicit dense, sparse, reranker, and tokenizer adapters"
                )
            if (
                isinstance(dense_writer, (InMemoryIndexWriter, InMemorySparseIndexProvider))
                or isinstance(sparse_provider, (InMemoryIndexWriter, InMemorySparseIndexProvider))
                or isinstance(reranker, (NoopReranker, ScoreReranker))
            ):
                raise RuntimeError("production does not accept memory or test indexing adapters")
        self.processor = processor or ContentProcessor()
        self.dense_writer = dense_writer or InMemoryIndexWriter(provider_name="dense-memory")
        self.sparse_provider = sparse_provider or build_sparse_provider(sparse_provider_name)
        self.generation = generation_manager or GenerationManager()
        self._object_store = object_store
        self._staged_chunks: dict[tuple[str, str], tuple[Any, ...]] = {}
        self._staged_receipts: dict[tuple[str, str], IndexProcessingReceipt] = {}
        self.graph = (
            GraphComponentCoordinator(self.generation, source_service)
            if source_service is not None
            else None
        )
        self.retrieval = RetrievalService(
            self.generation,
            (self.dense_writer, self.sparse_provider),
            identity_access=identity_access,
            visibility_facts=visibility_facts,
            reranker=reranker,
            environment=environment,
            profile_resolver=profile_resolver,
            tree_router=tree_router,
            graph_router=graph_router,
            graph_reader=self.graph,
            token_counter=token_counter,
        )
        self.citations = (
            CitationService(
                visibility_facts,
                self.generation,
                identity_access=identity_access,
            )
            if visibility_facts is not None
            else None
        )
        repository = getattr(self.generation, "_repository", None)
        if repository is not None:
            repository.set_generation_cleanup(self._cleanup_generation_publication)
            repository.set_generation_purge(self._purge_generation_resources)
            if object_store is not None:
                repository.set_generation_builder(self._build_generation_publication)

    def _cleanup_generation_publication(
        self, generation_id: str, document_id: str, document_version_id: str | None
    ) -> None:
        for provider in (self.dense_writer, self.sparse_provider):
            if document_version_id is None:
                provider.delete_document(document_id, generation_id=generation_id)
            else:
                provider.delete_document_version(
                    document_id, document_version_id, generation_id=generation_id
                )

    def _purge_generation_resources(
        self, generation_id: str, publications: tuple[tuple[str, str], ...]
    ) -> None:
        for document_id, document_version_id in publications:
            self._cleanup_generation_publication(
                generation_id,
                document_id,
                document_version_id,
            )

    def _build_generation_publication(
        self, generation: Any, source: Mapping[str, Any], connection: Any
    ) -> None:
        manifest = dict(source["manifest"])
        content_manifest_id = str(manifest.get("content_manifest_id") or "")
        content_manifest_hash = str(manifest.get("content_manifest_hash") or "")
        object_key = str(source.get("object_key") or "")
        media_kind = str(source.get("media_kind") or "")
        if not content_manifest_id or not content_manifest_hash or not object_key or not media_kind:
            raise PlatformError(
                "generation_source_missing", "Documents publication source is incomplete", {}, 409
            )
        assert self._object_store is not None
        content, metadata = self._object_store.get(object_key)
        if (
            sha256(content).hexdigest() != content_manifest_hash
            or metadata.content_type != media_kind
        ):
            raise PlatformError(
                "generation_source_conflict", "Documents publication content has changed", {}, 409
            )
        request = IndexStagingRequest(
            job_id=f"generation-build:{generation.generation_id}:{source['publication_id']}",
            attempt_id=f"generation-build:{generation.generation_id}:{source['publication_id']}",
            fencing_token=1,
            publication_id=str(source["publication_id"]),
            document_id=str(source["document_id"]),
            document_version_id=str(source["document_version_id"]),
            space_id=str(source["space_id"]),
            operation="reindex",
            base_active_version_id=None,
            expected_generation_id=generation.generation_id,
            index_revision_at_start=generation.applied_revision,
            object_manifest_ref=object_key,
            processing_config_snapshot=dict(manifest.get("processing_config_snapshot") or {}),
            authorization_fence={"generation_id": generation.generation_id},
            input_manifest_hash=content_manifest_hash,
            processing_profile_version=str(manifest.get("processing_profile_version") or "default"),
        )
        output = self.processor.process(
            request,
            content,
            media_kind=media_kind,
            content_manifest_id=content_manifest_id,
            content_manifest_hash=content_manifest_hash,
        )
        self.dense_writer.stage_chunks(
            request.attempt_id,
            request.publication_id,
            request.document_id,
            request.document_version_id,
            output.chunks,
            fencing_token=request.fencing_token,
            expected_generation_id=request.expected_generation_id,
            stage_resource_manifest=output.receipt.stage_resources,
            content_hash=output.receipt.input_manifest_hash,
        )
        self.sparse_provider.stage_chunks(
            request.attempt_id,
            request.publication_id,
            request.document_id,
            request.document_version_id,
            output.chunks,
            fencing_token=request.fencing_token,
            expected_generation_id=request.expected_generation_id,
            stage_resource_manifest=output.receipt.stage_resources,
            content_hash=output.receipt.input_manifest_hash,
        )
        self.dense_writer.publish_staged(
            request.attempt_id,
            request.publication_id,
            fencing_token=request.fencing_token,
            expected_generation_id=request.expected_generation_id,
            stage_resource_manifest=output.receipt.stage_resources,
            content_hash=output.receipt.input_manifest_hash,
        )
        self.sparse_provider.publish_staged(
            request.attempt_id,
            request.publication_id,
            fencing_token=request.fencing_token,
            expected_generation_id=request.expected_generation_id,
            stage_resource_manifest=output.receipt.stage_resources,
            content_hash=output.receipt.input_manifest_hash,
        )
        repository = getattr(self.generation, "_repository", None)
        assert repository is not None
        repository.record_published_chunks(request, output.chunks, connection=connection)

    def _ensure_current_generation(
        self, request: IndexStagingRequest, *, connection: Any | None = None
    ) -> None:
        repository = getattr(self.generation, "_repository", None)
        active = (
            repository.active_generation_id(connection=connection)
            if repository is not None and connection is not None
            else self.generation.active_generation_id
        )
        if request.expected_generation_id != active:
            raise PlatformError(
                "generation_conflict",
                "The processing attempt must be staged for the current generation",
                {},
                409,
            )

    def ensure_configuration_staging(self) -> Any:
        repository = getattr(self.generation, "_repository", None)
        if repository is None:
            return None
        return repository.ensure_configuration_staging()

    def process_and_stage(
        self,
        request: IndexStagingRequest,
        content: bytes | str,
        *,
        media_kind: str,
        content_manifest_id: str,
        content_manifest_hash: str,
        **options: Any,
    ) -> ProcessingOutput:
        self._ensure_current_generation(request)
        output = self.processor.process(
            request,
            content,
            media_kind=media_kind,
            content_manifest_id=content_manifest_id,
            content_manifest_hash=content_manifest_hash,
            **options,
        )
        try:
            self.dense_writer.stage_chunks(
                request.attempt_id,
                request.publication_id,
                request.document_id,
                request.document_version_id,
                output.chunks,
                fencing_token=request.fencing_token,
                expected_generation_id=request.expected_generation_id,
                stage_resource_manifest=output.receipt.stage_resources,
                content_hash=output.receipt.input_manifest_hash,
            )
            self.sparse_provider.stage_chunks(
                request.attempt_id,
                request.publication_id,
                request.document_id,
                request.document_version_id,
                output.chunks,
                fencing_token=request.fencing_token,
                expected_generation_id=request.expected_generation_id,
                stage_resource_manifest=output.receipt.stage_resources,
                content_hash=output.receipt.input_manifest_hash,
            )
            self._staged_chunks[(request.attempt_id, request.publication_id)] = output.chunks
            self._staged_receipts[(request.attempt_id, request.publication_id)] = output.receipt
        except PlatformError:
            self._discard_providers(request, output.receipt)
            raise
        return output

    def publish(
        self,
        request: IndexStagingRequest,
        *,
        connection: Any | None = None,
        receipt: IndexProcessingReceipt | Mapping[str, Any],
        validator: Any | None = None,
    ) -> Mapping[str, Any]:
        typed: IndexProcessingReceipt | None = None
        try:
            self._ensure_current_generation(request, connection=connection)
            typed = (
                receipt
                if isinstance(receipt, IndexProcessingReceipt)
                else IndexProcessingReceipt.from_mapping(receipt)
            )
            typed.validate_against(request)
            dense = self.dense_writer.publish_staged(
                request.attempt_id,
                request.publication_id,
                validator=validator,
                fencing_token=request.fencing_token,
                expected_generation_id=request.expected_generation_id,
                stage_resource_manifest=typed.stage_resources,
                content_hash=typed.input_manifest_hash,
            )
            sparse = self.sparse_provider.publish_staged(
                request.attempt_id,
                request.publication_id,
                validator=validator,
                fencing_token=request.fencing_token,
                expected_generation_id=request.expected_generation_id,
                stage_resource_manifest=typed.stage_resources,
                content_hash=typed.input_manifest_hash,
            )
            if dense.state != "published" or sparse.state != "published":
                raise PlatformError(
                    "indexing_publish_failed", "Index components did not publish", {}, 409
                )
            repository = getattr(self.generation, "_repository", None)
            staged_chunks = self._staged_chunks.get(
                (request.attempt_id, request.publication_id), ()
            )
            if repository is not None and connection is not None:
                repository.record_published_chunks(request, staged_chunks, connection=connection)
        except PlatformError:
            self._discard_providers(request, typed)
            raise
        return {
            "state": "published",
            "generation_id": request.expected_generation_id,
            "dense": dense.to_mapping(),
            "sparse": sparse.to_mapping(),
        }

    def discard(
        self,
        request: IndexStagingRequest,
        *,
        connection: Any | None = None,
    ) -> Mapping[str, Any]:
        key = (request.attempt_id, request.publication_id)
        receipt = self._staged_receipts.get(key)
        dense, sparse = self._discard_providers(request, receipt)
        self._staged_chunks.pop(key, None)
        self._staged_receipts.pop(key, None)
        return {"state": "discarded", "dense": dense.to_mapping(), "sparse": sparse.to_mapping()}

    def _discard_providers(
        self, request: IndexStagingRequest, receipt: IndexProcessingReceipt | None
    ) -> tuple[Any, Any]:
        if receipt is None:
            receipt = self._staged_receipts.get((request.attempt_id, request.publication_id))
        kwargs: dict[str, Any] = {
            "fencing_token": request.fencing_token,
            "expected_generation_id": request.expected_generation_id,
        }
        if receipt is not None:
            kwargs.update(
                stage_resource_manifest=receipt.stage_resources,
                content_hash=receipt.input_manifest_hash,
            )
        return (
            self.dense_writer.discard_staged(request.attempt_id, request.publication_id, **kwargs),
            self.sparse_provider.discard_staged(
                request.attempt_id, request.publication_id, **kwargs
            ),
        )

    def cleanup_resource(
        self, resource: Mapping[str, Any], *, connection: Any | None = None
    ) -> None:
        del resource, connection

    @staticmethod
    def _validate_cleanup_target(
        cleanup_target: Mapping[str, Any],
        *,
        document_id: str,
        document_version_id: str | None = None,
    ) -> None:
        if not isinstance(cleanup_target, Mapping):
            raise PlatformError(
                "cleanup_target_invalid", "Documents cleanup target is invalid", {}, 422
            )
        backend_kind = cleanup_target.get("backend_kind")
        resource_id = cleanup_target.get("resource_id")
        if (
            backend_kind not in {"index", "index_chunk", "cache"}
            or not isinstance(resource_id, str)
            or not resource_id.strip()
        ):
            raise PlatformError(
                "cleanup_target_invalid", "Documents cleanup target is invalid", {}, 422
            )
        if cleanup_target.get("document_id") not in {None, document_id}:
            raise PlatformError(
                "cleanup_target_invalid", "Documents cleanup target is invalid", {}, 422
            )
        if document_version_id is not None and cleanup_target.get("document_version_id") not in {
            None,
            document_version_id,
        }:
            raise PlatformError(
                "cleanup_target_invalid", "Documents cleanup target is invalid", {}, 422
            )

    def delete_document_version(
        self,
        document_id: str,
        document_version_id: str,
        *,
        cleanup_target: Mapping[str, Any] | None = None,
    ) -> int:
        if cleanup_target is None:
            raise PlatformError(
                "cleanup_target_required",
                "Documents cleanup target is required",
                {},
                409,
            )
        self._validate_cleanup_target(
            cleanup_target, document_id=document_id, document_version_id=document_version_id
        )
        return self.dense_writer.delete_document_version(
            document_id, document_version_id
        ) + self.sparse_provider.delete_document_version(document_id, document_version_id)

    def delete_document(
        self,
        document_id: str,
        *,
        cleanup_target: Mapping[str, Any] | None = None,
    ) -> int:
        if cleanup_target is None:
            raise PlatformError(
                "cleanup_target_required",
                "Documents cleanup target is required",
                {},
                409,
            )
        self._validate_cleanup_target(cleanup_target, document_id=document_id)
        return self.dense_writer.delete_document(
            document_id
        ) + self.sparse_provider.delete_document(document_id)

    def search(
        self,
        query: str,
        *,
        principal: Any = None,
        narrowing_scope: NarrowingScope | Mapping[str, Any] | None = None,
        profile: RetrievalProfile | None = None,
    ) -> RetrievalResult:
        return self.retrieval.search(
            query,
            principal=principal,
            narrowing_scope=narrowing_scope,
            profile=profile,
        )

    def open_retrieval_request(self) -> Any:
        return self.retrieval.open_request()

    def resolve_citation(
        self,
        hit: Any,
        *,
        request: Any | None = None,
        principal: Any = None,
    ) -> Mapping[str, Any]:
        if request is None:
            raise PlatformError(
                "retrieval_request_required",
                "citation resolution requires the originating retrieval request",
                {},
                409,
            )
        return request.resolve_citation(hit, principal=principal)


__all__ = ["IndexingService"]
