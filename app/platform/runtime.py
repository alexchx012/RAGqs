from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.engine import Connection

from app.chat.conversations import ConversationService
from app.chat.generation import GenerationService
from app.chat.ports import (
    ChatGenerationRevocationPort,
    IdentityChatAuthorizationPort,
    IndexingChatRetrievalPort,
    SqlAlchemyChatPairExpiry,
    UnavailableChatProviderPort,
)
from app.chat.preview import SqlAlchemyMessageCitationPreviewAdapter
from app.chat.streaming import GenerationStreamService
from app.chat.worker import ChatGenerationWorker
from app.documents.preview import ProcessingReceiptPreviewRenderer
from app.documents.public_graph import PublicGraphSourceService
from app.documents.read_models import DocumentsRetrievalVisibilityPort
from app.documents.service import DocumentsDepartmentWorkCheckPort, DocumentsService
from app.documents.submissions import DocumentsSubmissionInvalidationPort
from app.evaluation import (
    CalibrationCloseWorker,
    EvaluationCalibrationWindowPort,
    EvaluationService,
    HttpJudgeProvider,
    IdentitySpaceVisibilityPort,
    IndexingGenerationSourceAdapter,
    IndexingReplayAdapter,
    JudgeConfiguration,
    ShadowEvaluationWorker,
    SqlAlchemyCalibrationOutboxAdapter,
    SqlAlchemyChatFactsPort,
    SqlAlchemyEvaluationRepository,
    UnavailableAnswerReplayPort,
    UnavailableJudgeProvider,
    default_policy_snapshot,
)
from app.graph import (
    DeterministicPublicGraphExtractor,
    GenerationGraphAvailability,
    GraphBuildConfiguration,
    GraphBuildService,
    GraphBuildWorker,
    RepositoryActivatedReceiptVerifier,
    SqlAlchemyGraphBuildOutboxAdapter,
    SqlAlchemyGraphRepository,
    UsageLedgerSubmissionAdapter,
)
from app.identity.archive import IdentityArchiveProofIssuer, IdentityArchiveProofVerifier
from app.identity.cleanup import ObjectStoreAccountDeletionCleanupPort
from app.identity.ports import (
    AccountRetirementConfirmation,
    AccountRetirementRequest,
    UnavailableAccountRetirementGateway,
)
from app.identity.service import IdentityAccessService
from app.indexing import (
    ContentProcessor,
    IndexingService,
    NoopReranker,
    RetrievalReleaseService,
    ScoreReranker,
    SqlAlchemyGenerationManager,
    SqlAlchemyIndexingRepository,
)
from app.indexing.backends import (
    build_configured_sparse_provider,
    build_dense_writer,
    build_embedding_provider,
    is_memory_indexing_adapter,
    probe_configured_backends,
)
from app.indexing.embedding import InMemoryEmbeddingProvider
from app.outbox.dispatcher import OutboxDispatcher
from app.outbox.lifecycle import SqlAlchemyOutboxLifecycle
from app.outbox.maintenance import NotificationRetentionMaintenance
from app.outbox.metrics import SqlAlchemyOutboxMetrics
from app.outbox.notifications import NotificationMaterializer
from app.outbox.ports import (
    DocumentNotificationRedactionCommand,
    DocumentNotificationRedactionReceipt,
)
from app.outbox.publisher import (
    SqlAlchemyIngestionOutboxAdapter,
    SqlAlchemyOutboxPublisher,
    SqlAlchemyPublicGraphSourceOutboxAdapter,
    SqlAlchemyQuotaOutboxEnqueueAdapter,
    SqlAlchemyStartupConfigurationAlertAdapter,
    SqlAlchemySubmissionOutboxAdapter,
)
from app.outbox.service import NotificationService
from app.usage.billing import ProviderBillingService
from app.usage.budget import BudgetMeterService
from app.usage.calendar import CalendarLock, get_calendar_service
from app.usage.ledger import UsageLedger
from app.usage.metering import LocalUsageMeterService
from app.usage.observability import UsageResourceMetrics
from app.usage.price import PriceCatalogService
from app.usage.quota import QuotaService
from app.usage.requests import QuotaRequestService

from .config import PlatformSettings, validate_startup_settings
from .database import (
    SqlAlchemyDatabaseClock,
    SqlAlchemyLeaseStore,
    SqlAlchemyTransactionManager,
    create_engine_for_settings,
)
from .observability import SqlAlchemyObservabilityMetrics
from .storage import build_object_store


@dataclass(slots=True)
class PlatformRuntime:
    settings: PlatformSettings
    adapters: dict[str, Any] = field(default_factory=dict)
    closed: bool = False

    def resolve(self, name: str, default: Any = None) -> Any:
        if self.closed:
            raise RuntimeError("platform runtime is closed")
        return self.adapters.get(name, default)

    def close(self) -> None:
        if self.closed:
            return
        closed_ids: set[int] = set()
        for adapter in self.adapters.values():
            if id(adapter) in closed_ids:
                continue
            closed_ids.add(id(adapter))
            close = getattr(adapter, "close", None)
            if callable(close):
                close()
                continue
            dispose = getattr(adapter, "dispose", None)
            if callable(dispose):
                dispose()
        self.closed = True


def _index_configuration_staging_table_exists(engine: Any, table_name: str) -> bool:
    """Return whether ``table_name`` exists, checked through a live connection.

    ``sqlalchemy.inspect(engine)`` fails with ``NoInspectionAvailable`` for
    engine wrappers that delegate via ``__getattr__`` (e.g. dispose-tracking
    test doubles), while inspecting a real connection always works. A failure
    to open or inspect a connection is treated as "table absent": the staging
    warm-up is an optional startup pre-flight and must never break runtime
    assembly.
    """
    try:
        connection = engine.connect()
    except Exception:
        return False
    try:
        return inspect(connection).has_table(table_name)
    except Exception:
        return False
    finally:
        connection.close()


def missing_evaluation_judge_configuration(settings: PlatformSettings) -> tuple[str, ...]:
    """Return only the missing judge setting names safe to expose in an alert."""
    missing: list[str] = []
    if not settings.evaluation.judge_base_url or not settings.evaluation.judge_base_url.strip():
        missing.append("RAG_EVALUATION_JUDGE_BASE_URL")
    judge_api_key = settings.evaluation.judge_api_key
    if judge_api_key is None or not judge_api_key.get_secret_value().strip():
        missing.append("RAG_EVALUATION_JUDGE_API_KEY")
    return tuple(missing)


def build_runtime(
    settings: PlatformSettings,
    adapters: Mapping[str, Any] | None = None,
) -> PlatformRuntime:
    validate_startup_settings(settings)
    configured = dict(adapters or {})
    engine = configured.get("database_engine") or create_engine_for_settings(settings)
    clock = configured.get("database_clock") or SqlAlchemyDatabaseClock(engine)
    transaction_manager = configured.get("transaction_manager") or SqlAlchemyTransactionManager(
        engine,
        clock,
    )
    lease_store = configured.get("lease_store") or SqlAlchemyLeaseStore(engine, clock)
    observability_metrics = configured.get(
        "observability_metrics"
    ) or SqlAlchemyObservabilityMetrics(
        engine,
        now=clock.now_utc,
        retention_days=settings.observability.api_metric_retention_days,
        success_sample_rate=settings.observability.success_sample_rate,
        max_route_templates=settings.observability.max_route_templates,
    )
    secret_key = (
        settings.object_storage.secret_key.get_secret_value()
        if settings.object_storage.secret_key is not None
        else None
    )
    object_store = configured.get("object_store") or build_object_store(
        endpoint=settings.object_storage.endpoint,
        bucket=settings.object_storage.bucket,
        access_key=settings.object_storage.access_key,
        secret_key=secret_key,
    )
    configured.setdefault("database_engine", engine)
    configured.setdefault("database_clock", clock)
    configured.setdefault("transaction_manager", transaction_manager)
    configured.setdefault("lease_store", lease_store)
    configured.setdefault("observability_metrics", observability_metrics)
    configured.setdefault("object_store", object_store)
    department_work_check_port = configured.get(
        "department_work_check_port"
    ) or DocumentsDepartmentWorkCheckPort(engine)
    configured.setdefault("department_work_check_port", department_work_check_port)
    generation_revocation_port = configured.get("generation_revocation_port") or (
        ChatGenerationRevocationPort()
    )
    configured.setdefault("generation_revocation_port", generation_revocation_port)
    deletion_cleanup_port = configured.get("account_deletion_cleanup_port") or (
        ObjectStoreAccountDeletionCleanupPort(object_store)
    )
    configured.setdefault("account_deletion_cleanup_port", deletion_cleanup_port)
    archive_secret = configured.get("archive_proof_secret") or (
        settings.auth.secret_key.get_secret_value().encode("utf-8")
        if settings.auth.secret_key
        else b"ragqs-development-archive-proof-secret"
    )
    archive_issuer = configured.get("archive_issuer") or IdentityArchiveProofIssuer(archive_secret)
    archive_verifier = configured.get("archive_verifier") or IdentityArchiveProofVerifier(
        archive_secret
    )
    configured.setdefault("archive_issuer", archive_issuer)
    configured.setdefault("archive_verifier", archive_verifier)
    identity_access = configured.get("identity_access") or IdentityAccessService(
        engine,
        settings.auth,
        now=clock.now_utc,
        revocation_port=generation_revocation_port,
        department_work_check=department_work_check_port,
        deletion_cleanup_port=deletion_cleanup_port,
        object_store=object_store,
        account_retirement_gateway=configured.get("account_retirement_gateway"),
        archive_issuer=archive_issuer,
    )
    configured.setdefault("identity_access", identity_access)
    documents_service = configured.get("documents_service")
    preview_renderer = (
        configured.get("document_preview_renderer") or ProcessingReceiptPreviewRenderer()
    )
    configured.setdefault("document_preview_renderer", preview_renderer)
    message_citation_preview_port = configured.get("message_citation_preview_port") or (
        SqlAlchemyMessageCitationPreviewAdapter(engine)
    )
    configured.setdefault("message_citation_preview_port", message_citation_preview_port)
    notification_materializer = configured.get("notification_materializer") or (
        NotificationMaterializer(
            engine,
            notification_retention_days=settings.outbox.notification_retention_days,
        )
    )
    configured.setdefault("notification_materializer", notification_materializer)
    outbox_dispatcher = configured.get("outbox_dispatcher") or OutboxDispatcher(
        engine,
        consumers={"in_app_notification": notification_materializer},
        now=clock.now_utc,
        clock=clock,
        retention_days=settings.outbox.outbox_delivered_retention_days,
        notification_retention_days=settings.outbox.notification_retention_days,
        metrics=SqlAlchemyOutboxMetrics(),
    )
    configured.setdefault("outbox_dispatcher", outbox_dispatcher)
    notification_service = configured.get("notification_service") or NotificationService(
        engine,
        now=clock.now_utc,
        clock=clock,
    )
    configured.setdefault("notification_service", notification_service)
    # Outbox lifecycle assembly: the raw lifecycle object is NEVER registered
    # as a runtime adapter. Only operation-scoped typed façades are exposed —
    # the identity-deletion retirement gateway and the scoped workers — each
    # with a fixed principal/operation type. Every assembly-only sensitive/raw
    # override is CONSUMED with pop() so it never enters PlatformRuntime.
    # adapters: the injected raw lifecycle is still used to assemble the
    # scoped workers/gateway, but `resolve("outbox_lifecycle")` stays None.
    outbox_lifecycle = configured.pop("outbox_lifecycle", None) or SqlAlchemyOutboxLifecycle(
        engine,
        now=clock.now_utc,
        clock=clock,
        archive_verifier=archive_verifier,
    )
    account_retirement_gateway = configured.get("account_retirement_gateway") or (
        _OutboxAccountRetirementGateway(outbox_lifecycle)
    )
    configured.setdefault("account_retirement_gateway", account_retirement_gateway)
    document_lifecycle_port = configured.get("document_lifecycle_port") or (
        _OutboxDocumentLifecycleGateway(outbox_lifecycle)
    )
    configured.setdefault("document_lifecycle_port", document_lifecycle_port)
    if identity_access._account_retirement_gateway is None or isinstance(
        identity_access._account_retirement_gateway, UnavailableAccountRetirementGateway
    ):
        identity_access._account_retirement_gateway = account_retirement_gateway
    retention_maintenance = configured.get("notification_retention_maintenance") or (
        NotificationRetentionMaintenance(
            engine,
            now=clock.now_utc,
        )
    )
    configured.setdefault("notification_retention_maintenance", retention_maintenance)
    graph_build_repository = configured.get("graph_build_repository") or (
        SqlAlchemyGraphRepository(engine, now=clock.now_utc)
    )
    configured.setdefault("graph_build_repository", graph_build_repository)
    graph_activated_receipt_verifier = configured.get(
        "graph_activated_receipt_verifier"
    ) or RepositoryActivatedReceiptVerifier(graph_build_repository)
    # The raw publisher is assembly input only. Domain-specific outbox facades
    # below are the runtime adapters, so callers cannot select another domain
    # by resolving a generic publisher and supplying an event type.
    outbox_publisher = configured.pop("outbox_publisher", None) or SqlAlchemyOutboxPublisher(
        engine,
        clock=clock,
        graph_activated_receipt_port=graph_activated_receipt_verifier,
        retention_days=settings.outbox.outbox_delivered_retention_days,
    )
    startup_configuration_alert_port = configured.get("startup_configuration_alert_port") or (
        SqlAlchemyStartupConfigurationAlertAdapter(outbox_publisher)
    )
    configured.setdefault("startup_configuration_alert_port", startup_configuration_alert_port)
    public_source_outbox_port = configured.get("public_graph_source_outbox_port") or (
        SqlAlchemyPublicGraphSourceOutboxAdapter(outbox_publisher)
    )
    graph_trusted_consumers = configured.get("public_graph_source_trusted_consumers") or {
        "indexing": {"indexing"},
        "public_graph": {"public_graph"},
    }
    public_graph_source_service = configured.get("public_graph_source_service") or (
        PublicGraphSourceService(
            engine,
            now=clock.now_utc,
            trusted_consumers=graph_trusted_consumers,
            outbox_port=public_source_outbox_port,
        )
    )
    configured.setdefault("public_graph_source_service", public_graph_source_service)
    generation_repository = configured.get("indexing_generation_repository") or (
        SqlAlchemyIndexingRepository(
            engine,
            now=clock.now_utc,
            rollback_days=settings.index.generation_rollback_days,
            generation_configuration={
                "provider": settings.index.sparse_provider,
                "engine": (
                    "opensearch"
                    if settings.index.sparse_provider.startswith("opensearch")
                    else "meilisearch"
                ),
                "analyzer": (
                    "ik" if settings.index.sparse_provider == "opensearch+ik" else "jieba"
                ),
                "pretokenizer_version": "v1",
                "schema_version": "index-chunks-v1",
                "reranker_provider": settings.index.reranker_provider,
                "image_vlm_provider": settings.index.image_vlm_provider,
                "embedding_model": settings.index.embedding_model or "configured",
                "embedding_revision": (
                    settings.index.embedding_revision
                    or settings.index.embedding_model
                    or "configured"
                ),
                "embedding_dimension": settings.index.embedding_dimension,
                "embedding_metric": settings.index.embedding_metric,
            },
        )
    )
    configured.setdefault("indexing_generation_repository", generation_repository)
    generation_manager = configured.get("indexing_generation_manager") or (
        SqlAlchemyGenerationManager(generation_repository)
    )
    configured.setdefault("indexing_generation_manager", generation_manager)
    retrieval_releases = configured.get("retrieval_release_service") or RetrievalReleaseService(
        engine, now=clock.now_utc
    )
    configured.setdefault("retrieval_release_service", retrieval_releases)
    generation_repository.set_retrieval_release_gate(retrieval_releases.is_released_for_generation)
    reranker = configured.get("indexing_reranker")
    if reranker is None:
        if settings.profile == "production":
            raise RuntimeError("production requires an explicit indexing reranker")
        reranker = (
            NoopReranker(environment=settings.profile)
            if settings.index.reranker_provider.casefold() == "none"
            else ScoreReranker()
        )
    configured.setdefault("indexing_reranker", reranker)
    visibility_facts = configured.get("indexing_visibility_facts") or (
        DocumentsRetrievalVisibilityPort(engine, object_store)
    )
    configured.setdefault("indexing_visibility_facts", visibility_facts)
    processor = configured.get("indexing_processor") or ContentProcessor(
        mineru=configured.get("indexing_mineru"),
        image_describer=configured.get("indexing_image_describer"),
        image_ocr=configured.get("indexing_image_ocr"),
        text_chunk_max_chars=settings.index.text_chunk_max_chars,
        xlsx_merged_cells_max=settings.index.xlsx_merged_cells_max,
        ocr_confidence_threshold=settings.index.ocr_confidence_threshold,
    )
    configured.setdefault("indexing_processor", processor)
    embedding = configured.get("indexing_embedding")
    allow_create = settings.profile != "production"
    if embedding is None and (
        settings.index.embedding_provider != "memory" or settings.index.vector_provider != "memory"
    ):
        embedding = build_embedding_provider(settings)
        configured.setdefault("indexing_embedding", embedding)
    dense_writer = configured.get("indexing_dense_writer")
    sparse_provider = configured.get("indexing_sparse_provider")
    if dense_writer is None and settings.index.vector_provider != "memory":
        dense_writer = build_dense_writer(settings, embedding, allow_create=allow_create)
        configured.setdefault("indexing_dense_writer", dense_writer)
    if sparse_provider is None and settings.index.sparse_url:
        sparse_provider = build_configured_sparse_provider(settings, allow_create=allow_create)
        configured.setdefault("indexing_sparse_provider", sparse_provider)
    token_counter = configured.get("indexing_token_counter")
    if settings.profile == "production":
        if not callable(configured.get("indexing_image_ocr")) or not callable(
            configured.get("indexing_image_describer")
        ):
            raise RuntimeError("production requires explicit indexing image OCR and VLM ports")
        if dense_writer is None or sparse_provider is None:
            raise RuntimeError("production requires explicit indexing dense and sparse backends")
        if not callable(token_counter):
            raise RuntimeError("production requires an explicit indexing token counter")
        if (
            is_memory_indexing_adapter(dense_writer)
            or is_memory_indexing_adapter(sparse_provider)
            or isinstance(embedding, InMemoryEmbeddingProvider)
            or isinstance(reranker, (NoopReranker, ScoreReranker))
        ):
            raise RuntimeError("production does not accept memory or test indexing adapters")
        required_writer_methods = (
            "stage_chunks",
            "publish_staged",
            "discard_staged",
            "delete_document_version",
            "delete_document",
        )
        if any(not callable(getattr(dense_writer, name, None)) for name in required_writer_methods):
            raise RuntimeError("production dense backend does not implement the indexing port")
        if any(
            not callable(getattr(sparse_provider, name, None)) for name in required_writer_methods
        ):
            raise RuntimeError("production sparse backend does not implement the indexing port")
        if not callable(getattr(dense_writer, "search", None)):
            raise RuntimeError("production dense backend must provide vector search")
        if not callable(getattr(sparse_provider, "search", None)):
            raise RuntimeError("production sparse backend must provide BM25 search")
        if not callable(getattr(reranker, "rerank", None)):
            raise RuntimeError("production reranker does not implement the rerank port")
    probe_configured_backends(dense_writer, sparse_provider)
    indexing_service = configured.get("indexing_service") or IndexingService(
        processor=processor,
        dense_writer=dense_writer,
        sparse_provider=sparse_provider,
        sparse_provider_name=settings.index.sparse_provider,
        generation_manager=generation_manager,
        reranker=reranker,
        environment=settings.profile,
        profile_resolver=retrieval_releases.resolve,
        identity_access=identity_access,
        visibility_facts=visibility_facts,
        source_service=public_graph_source_service,
        tree_router=configured.get("indexing_tree_router"),
        graph_router=configured.get("indexing_graph_router"),
        token_counter=token_counter,
        object_store=object_store,
        embedding=embedding,
        exact_match_metrics=observability_metrics,
    )
    configured.setdefault("indexing_service", indexing_service)
    if _index_configuration_staging_table_exists(engine, "index_generations"):
        indexing_service.ensure_configuration_staging()
    calendar = configured.get("business_calendar") or get_calendar_service(
        engine,
        clock,
        settings.business_timezone or "UTC",
    )
    prices = configured.get("price_catalog") or PriceCatalogService(engine, clock)
    ledger = configured.get("usage_ledger") or UsageLedger(engine, clock, calendar, prices)
    usage_metrics = configured.get("usage_resource_metrics") or UsageResourceMetrics()
    configured.setdefault("usage_resource_metrics", usage_metrics)
    local_usage_meter = configured.get("local_usage_meter") or LocalUsageMeterService(
        ledger, clock, usage_metrics
    )
    provider_billing = configured.get("provider_billing") or ProviderBillingService(
        ledger, clock, usage_metrics
    )
    quota_service = configured.get("quota_service") or QuotaService(engine, clock, calendar)
    outbox_port = configured.get("outbox_enqueue_port") or (
        SqlAlchemyQuotaOutboxEnqueueAdapter(outbox_publisher)
    )
    submission_outbox_port = configured.get("submission_outbox_port") or (
        SqlAlchemySubmissionOutboxAdapter(outbox_publisher)
    )
    ingestion_outbox_port = configured.get("ingestion_outbox_port") or (
        SqlAlchemyIngestionOutboxAdapter(outbox_publisher)
    )
    quota_request_service = configured.get("quota_request_service") or QuotaRequestService(
        engine,
        clock,
        calendar,
        quota_service,
        outbox_port,
    )
    configured.setdefault("business_calendar", calendar)
    configured.setdefault("price_catalog", prices)
    configured.setdefault("usage_ledger", ledger)
    configured.setdefault("local_usage_meter", local_usage_meter)
    configured.setdefault("provider_billing", provider_billing)
    configured.setdefault("quota_service", quota_service)
    configured.setdefault("outbox_enqueue_port", outbox_port)
    configured.setdefault("submission_outbox_port", submission_outbox_port)
    configured.setdefault("ingestion_outbox_port", ingestion_outbox_port)
    configured.setdefault("quota_request_service", quota_request_service)
    indexing_usage_submission = configured.get("indexing_usage_submission") or (
        UsageLedgerSubmissionAdapter(ledger)
    )
    configured.setdefault("indexing_usage_submission", indexing_usage_submission)
    configure_embedding_usage = getattr(embedding, "set_usage_submission", None)
    if callable(configure_embedding_usage):
        configure_embedding_usage(indexing_usage_submission)
    if documents_service is None:
        documents_service = DocumentsService(
            engine,
            now=clock.now_utc,
            object_store=object_store,
            identity_access=identity_access,
            lifecycle_port=document_lifecycle_port,
            indexing_handoff_port=configured.get("indexing_handoff_port") or indexing_service,
            quota_service=quota_service,
            submission_notification_port=submission_outbox_port,
            ingestion_notification_port=ingestion_outbox_port,
            public_graph_source_service=public_graph_source_service,
            max_upload_bytes=settings.documents.upload_max_bytes,
            cleanup_max_attempts=settings.documents.cleanup_max_attempts,
            version_retention_days=settings.documents.version_retention_days,
            preview_renderer=preview_renderer,
            message_citation_preview_port=message_citation_preview_port,
        )
    elif isinstance(documents_service, DocumentsService):
        if documents_service._lifecycle_port is None:
            documents_service._lifecycle_port = document_lifecycle_port
        if documents_service._quota_service is None:
            documents_service._quota_service = quota_service
        if documents_service._submission_notification_port is None:
            documents_service._submission_notification_port = submission_outbox_port
        if documents_service._ingestion_notification_port is None:
            documents_service._ingestion_notification_port = ingestion_outbox_port
        if documents_service._public_graph_source_service is None:
            documents_service._public_graph_source_service = public_graph_source_service
        if documents_service._preview_renderer is None:
            documents_service._preview_renderer = preview_renderer
        if documents_service._message_citation_preview_port is None:
            documents_service._message_citation_preview_port = message_citation_preview_port
    configured.setdefault("documents_service", documents_service)
    graph_build_outbox_port = configured.get("graph_build_outbox_port") or (
        SqlAlchemyGraphBuildOutboxAdapter(outbox_publisher)
    )
    graph_availability_port = configured.get("graph_availability_port") or (
        GenerationGraphAvailability(generation_repository)
    )
    graph_build_configuration = configured.get("graph_build_configuration")
    if graph_build_configuration is None:
        graph_build_configuration = GraphBuildConfiguration()
    elif isinstance(graph_build_configuration, dict):
        graph_build_configuration = GraphBuildConfiguration(**graph_build_configuration)
    graph_build_extractor = configured.get("graph_build_extractor")
    if graph_build_extractor is None:
        if settings.profile == "production":
            raise RuntimeError("production requires an explicit graph build extractor")
        graph_build_extractor = DeterministicPublicGraphExtractor()
    graph_usage_submission = configured.get("graph_usage_submission") or (
        UsageLedgerSubmissionAdapter(ledger)
    )
    graph_build_service = configured.get("graph_build_service") or GraphBuildService(
        engine,
        repository=graph_build_repository,
        source=public_graph_source_service,
        coordinator=indexing_service.graph,
        availability=graph_availability_port,
        extractor=graph_build_extractor,
        outbox=graph_build_outbox_port,
        verifier=graph_activated_receipt_verifier,
        configuration=graph_build_configuration,
        now=clock.now_utc,
    )
    configured.setdefault("graph_build_service", graph_build_service)
    configured.setdefault(
        "graph_build_worker",
        configured.get("graph_build_worker")
        or GraphBuildWorker(
            graph_build_service,
            graph_build_extractor,
            graph_usage_submission,
            now=clock.now_utc,
        ),
    )
    chat_authorization = configured.get("chat_authorization_port") or (
        IdentityChatAuthorizationPort(identity_access)
    )
    configured.setdefault("chat_authorization_port", chat_authorization)
    chat_retrieval = configured.get("chat_retrieval_port") or (
        IndexingChatRetrievalPort(indexing_service)
    )
    configured.setdefault("chat_retrieval_port", chat_retrieval)
    chat_provider = configured.get("chat_provider_port") or UnavailableChatProviderPort()
    configured.setdefault("chat_provider_port", chat_provider)
    evaluation_calibration_port = configured.get("evaluation_calibration_port") or (
        EvaluationCalibrationWindowPort(engine)
    )
    configured.setdefault("evaluation_calibration_port", evaluation_calibration_port)
    chat_calibration = configured.get("chat_calibration_port") or evaluation_calibration_port
    configured.setdefault("chat_calibration_port", chat_calibration)
    chat_usage: Any = configured.get("chat_usage_submission") or (
        UsageLedgerSubmissionAdapter(ledger)
    )
    configured.setdefault("chat_usage_submission", chat_usage)
    generation_budget_meter = configured.get("generation_budget_meter")
    if settings.profile == "production":
        if not isinstance(generation_budget_meter, BudgetMeterService):
            raise RuntimeError("production requires an explicit generation budget meter")
        generation_budget_meter.policy.validate(production=True)
    configured.setdefault("generation_budget_meter", generation_budget_meter)
    chat_conversation_service = configured.get("chat_conversation_service") or (
        ConversationService(engine, now=clock)
    )
    configured.setdefault("chat_conversation_service", chat_conversation_service)
    chat_generation_service = configured.get("chat_generation_service") or (
        GenerationService(
            engine,
            clock=clock,
            authorization=chat_authorization,
            calibration=chat_calibration,
            budget_meter=generation_budget_meter,
        )
    )
    configured.setdefault("chat_generation_service", chat_generation_service)
    chat_stream_service = configured.get("chat_stream_service") or (
        GenerationStreamService(
            engine,
            clock=clock,
            authorization=chat_authorization,
        )
    )
    configured.setdefault("chat_stream_service", chat_stream_service)
    chat_worker = configured.get("chat_generation_worker") or ChatGenerationWorker(
        engine,
        clock=clock,
        retrieval=chat_retrieval,
        provider=chat_provider,
        usage=chat_usage,
        calibration=chat_calibration,
        budget_meter=generation_budget_meter,
    )
    configured.setdefault("chat_generation_worker", chat_worker)
    evaluation_repository = configured.get("evaluation_repository") or (
        SqlAlchemyEvaluationRepository(engine)
    )
    configured.setdefault("evaluation_repository", evaluation_repository)
    if _index_configuration_staging_table_exists(engine, "evaluation_policy"):
        with engine.begin() as connection:
            if evaluation_repository.latest_policy(connection) is None:
                evaluation_repository.ensure_policy(
                    connection,
                    policy=default_policy_snapshot(now=clock.now_utc(connection)),
                )
    evaluation_usage_submission = configured.get("evaluation_usage_submission") or (
        UsageLedgerSubmissionAdapter(ledger)
    )
    configured.setdefault("evaluation_usage_submission", evaluation_usage_submission)
    judge_provider = configured.get("judge_provider")
    auto_assembled_http_judge: HttpJudgeProvider | None = None
    judge_requires_preflight = settings.profile == "production"
    if judge_provider is None:
        if settings.profile == "production":
            missing_judge_configuration = missing_evaluation_judge_configuration(settings)
            if missing_judge_configuration:
                judge_provider = UnavailableJudgeProvider(environment=settings.profile)
                judge_requires_preflight = False
            else:
                judge_api_key = settings.evaluation.judge_api_key
                assert judge_api_key is not None
                auto_assembled_http_judge = HttpJudgeProvider(
                    base_url=settings.evaluation.judge_base_url or "",
                    api_key=judge_api_key.get_secret_value(),
                    usage_submission=evaluation_usage_submission,
                    configuration=JudgeConfiguration(
                        provider=settings.evaluation.judge_provider,
                        model=settings.evaluation.judge_model,
                        mode=settings.evaluation.judge_mode,
                        credential_ref=settings.evaluation.judge_credential_ref,
                    ),
                )
                judge_provider = auto_assembled_http_judge
        else:
            judge_provider = UnavailableJudgeProvider(environment=settings.profile)
    configured.setdefault("judge_provider", judge_provider)
    if judge_requires_preflight:
        from app.evaluation import JudgePreflight

        try:
            JudgePreflight(judge_provider).verify_startup()
        except Exception:
            if auto_assembled_http_judge is not None:
                auto_assembled_http_judge.close()
            raise
    calibration_outbox_port = configured.get("calibration_outbox_port") or (
        SqlAlchemyCalibrationOutboxAdapter(outbox_publisher)
    )
    configured.setdefault("calibration_outbox_port", calibration_outbox_port)
    sample_snapshot_source = configured.get("sample_snapshot_source") or SqlAlchemyChatFactsPort(
        engine
    )
    configured.setdefault("sample_snapshot_source", sample_snapshot_source)

    class _EvaluationCandidateConfigSource:
        def __init__(self, settings: Any) -> None:
            self._settings = settings

        def candidate_config_versions(self, *, space_id: str) -> tuple[str, ...]:
            del space_id
            return tuple(self._settings.evaluation.candidate_configs)

    candidate_config_source = configured.get("candidate_config_source") or (
        _EvaluationCandidateConfigSource(settings)
    )
    configured.setdefault("candidate_config_source", candidate_config_source)
    index_generation_source = configured.get("index_generation_source") or (
        IndexingGenerationSourceAdapter(generation_repository, generation_manager)
    )
    configured.setdefault("index_generation_source", index_generation_source)
    retrieval_replay_port = configured.get("retrieval_replay_port") or IndexingReplayAdapter(
        indexing_service
    )
    configured.setdefault("retrieval_replay_port", retrieval_replay_port)
    answer_replay_port = configured.get("answer_replay_port") or UnavailableAnswerReplayPort()
    configured.setdefault("answer_replay_port", answer_replay_port)
    evaluation_space_visibility = configured.get("evaluation_space_visibility") or (
        IdentitySpaceVisibilityPort(identity_access)
    )
    configured.setdefault("evaluation_space_visibility", evaluation_space_visibility)
    evaluation_service = configured.get("evaluation_service") or EvaluationService(
        engine,
        evaluation_repository,
        judge=judge_provider,
        chat_facts=sample_snapshot_source,
        candidate_configs=candidate_config_source,
        index_generation=index_generation_source,
        retrieval=retrieval_replay_port,
        space_visibility=evaluation_space_visibility,
        now=clock.now_utc,
    )
    evaluation_service.attach_outbox(calibration_outbox_port)
    configured.setdefault("evaluation_service", evaluation_service)
    evaluation_worker = configured.get("evaluation_worker") or ShadowEvaluationWorker(
        engine,
        evaluation_repository,
        judge_provider,
        retrieval_replay_port,
        answer_replay=answer_replay_port,
        now=clock.now_utc,
        suggestion_callback=evaluation_service.compute_suggestion,
    )
    configured.setdefault("evaluation_worker", evaluation_worker)
    configured.setdefault(
        "calibration_close_worker",
        configured.get("calibration_close_worker")
        or CalibrationCloseWorker(
            engine,
            evaluation_repository,
            pair_expiry=SqlAlchemyChatPairExpiry(engine),
            now=clock.now_utc,
        ),
    )
    submission_invalidation_port = configured.get("submission_invalidation_port")
    if submission_invalidation_port is None:
        with engine.connect() as connection:
            documents_schema_available = inspect(connection).has_table("knowledge_submissions")
        if documents_schema_available:
            submission_invalidation_port = DocumentsSubmissionInvalidationPort(documents_service)
    if submission_invalidation_port is not None:
        configured.setdefault("submission_invalidation_port", submission_invalidation_port)
        if identity_access._pending_submission_invalidation_port is None:
            identity_access._pending_submission_invalidation_port = submission_invalidation_port
    runtime = PlatformRuntime(settings=settings, adapters=configured)
    # Assemble the scoped workers around THIS runtime. Both workers use the
    # lifecycle's INTERNAL no-token entries; the worker object graphs contain
    # no token and no signing secret. The worker classes and WorkerRuntime are
    # imported lazily to avoid a circular import with the worker modules.
    from app.outbox.compaction_worker import CompactionWorker
    from app.outbox.retirement_worker import RetirementWorker, build_retirement_processor

    from .worker import WorkerRuntime

    worker_runtime = WorkerRuntime(
        runtime=runtime,
        leases=configured["lease_store"],
        now=clock.now_utc,
        owns_runtime=False,
    )
    configured.setdefault(
        "retirement_worker",
        RetirementWorker(
            worker_runtime,
            processor=build_retirement_processor(outbox_lifecycle),
        ),
    )
    configured.setdefault(
        "compaction_worker",
        CompactionWorker(worker_runtime, lifecycle=outbox_lifecycle),
    )
    # Retention & operations orchestration assembly. Destructive effects stay
    # inside the owner domains; retention only drives owner entries and owns
    # its reconciliation/findings/receipts and the server-driven read models.
    from app.retention.adapters import (
        RuntimeAccountCompactionPort,
        RuntimeDocumentsCleanupPort,
        RuntimeGraphGcPort,
        RuntimeIndexingGcPort,
    )
    from app.retention.compaction import AccountCompactionRequester
    from app.retention.gc_handoff import GenerationGcCoordinator
    from app.retention.readers import DashboardReadModels, OpsJobsReadModel
    from app.retention.reconcile import ReconciliationService
    from app.retention.repository import SqlAlchemyRetentionRepository
    from app.retention.service import RetentionOpsService
    from app.retention.worker import RetentionMaintenanceWorker

    retention_repository = configured.get("retention_repository") or (
        SqlAlchemyRetentionRepository(engine, now=clock.now_utc)
    )
    configured.setdefault("retention_repository", retention_repository)
    documents_cleanup_port = configured.get("retention_documents_cleanup_port") or (
        RuntimeDocumentsCleanupPort(documents_service)
    )
    configured.setdefault("retention_documents_cleanup_port", documents_cleanup_port)
    identity_history_cleanup_port = configured.get("retention_identity_history_cleanup_port") or (
        identity_access
    )
    configured.setdefault("retention_identity_history_cleanup_port", identity_history_cleanup_port)
    indexing_gc_port = configured.get("retention_indexing_gc_port") or (
        RuntimeIndexingGcPort(indexing_service.graph)
    )
    configured.setdefault("retention_indexing_gc_port", indexing_gc_port)
    graph_gc_port = configured.get("retention_graph_gc_port") or (
        RuntimeGraphGcPort(graph_build_service)
    )
    configured.setdefault("retention_graph_gc_port", graph_gc_port)
    account_compaction_gateway = configured.get("account_compaction_gateway") or (
        _OutboxAccountCompactionGateway(outbox_lifecycle)
    )
    configured.setdefault("account_compaction_gateway", account_compaction_gateway)
    compaction_port = configured.get("retention_compaction_port") or (
        RuntimeAccountCompactionPort(engine, account_compaction_gateway)
    )
    configured.setdefault("retention_compaction_port", compaction_port)
    gc_coordinator = configured.get("retention_gc_coordinator") or GenerationGcCoordinator(
        repository=retention_repository,
        indexing_gc_port=indexing_gc_port,
        graph_gc_port=graph_gc_port,
    )
    configured.setdefault("retention_gc_coordinator", gc_coordinator)
    retention_reconciliation = configured.get("retention_reconciliation") or (
        ReconciliationService(
            repository=retention_repository,
            documents_port=documents_cleanup_port,
            gc_coordinator=gc_coordinator,
            engine=engine,
            now=clock.now_utc,
        )
    )
    configured.setdefault("retention_reconciliation", retention_reconciliation)
    dashboard_read = configured.get("retention_dashboard") or DashboardReadModels(
        engine=engine,
        now=clock.now_utc,
        observability_metrics=observability_metrics,
    )
    configured.setdefault("retention_dashboard", dashboard_read)
    ops_jobs_read = configured.get("retention_ops_jobs") or OpsJobsReadModel(
        engine=engine,
        now=clock.now_utc,
        documents_service=documents_service,
    )
    configured.setdefault("retention_ops_jobs", ops_jobs_read)
    compaction_requester = configured.get("retention_compaction_requester") or (
        AccountCompactionRequester(repository=retention_repository, port=compaction_port)
    )
    configured.setdefault("retention_compaction_requester", compaction_requester)
    retention_ops = configured.get("retention_ops") or RetentionOpsService(
        repository=retention_repository,
        dashboard=dashboard_read,
        ops_jobs=ops_jobs_read,
        reconciliation=retention_reconciliation,
        gc_coordinator=gc_coordinator,
        compaction=compaction_requester,
        engine=engine,
        documents_cleanup_port=documents_cleanup_port,
        identity_history_cleanup_port=identity_history_cleanup_port,
    )
    configured.setdefault("retention_ops", retention_ops)
    configured.setdefault(
        "retention_worker",
        configured.get("retention_worker") or RetentionMaintenanceWorker(worker_runtime),
    )
    return runtime


class _OutboxAccountRetirementGateway:
    """Identity-deletion scoped retirement façade.

    The gateway ONLY retires exactly the account named by an identity deletion
    workflow request, through the lifecycle's internal
    `retire_account_for_identity_deletion` (verified archive proof +
    pending-delete account). Its fixed principal and operation type are baked
    into the façade, so a caller cannot choose another operation.
    """

    def __init__(self, lifecycle: SqlAlchemyOutboxLifecycle) -> None:
        self._lifecycle = lifecycle

    def retire(
        self,
        request: AccountRetirementRequest,
        *,
        connection: Connection,
    ) -> AccountRetirementConfirmation:
        receipt = self._lifecycle.retire_account_for_identity_deletion(
            operation_id=request.operation_id,
            user_id=request.user_id,
            deletion_id=request.deletion_id,
            verified_archive_ref=request.verified_archive_ref,
            archive_checksum=request.archive_checksum,
            transaction_id=request.transaction_id,
            connection=connection,
        )
        return AccountRetirementConfirmation(
            state=receipt.state,
            receipt_count=receipt.receipt_count,
        )


class _OutboxDocumentLifecycleGateway:
    """Documents-deletion scoped redaction facade."""

    def __init__(self, lifecycle: SqlAlchemyOutboxLifecycle) -> None:
        self._lifecycle = lifecycle

    def redact_document_notifications(
        self,
        command: DocumentNotificationRedactionCommand,
        *,
        connection: Connection,
    ) -> DocumentNotificationRedactionReceipt:
        return self._lifecycle.redact_document_notifications_for_documents(
            operation_id=command.operation_id,
            deletion_id=command.deletion_id,
            document_id=command.document_id,
            document_version_ids=command.document_version_ids,
            transaction_id=command.transaction_id,
            connection=connection,
        )


class _OutboxAccountCompactionGateway:
    """Identity-deletion scoped eligible-compaction facade.

    The gateway ONLY requests event compaction for exactly the completed
    retirement named by an identity deletion workflow, through the lifecycle's
    internal request_compaction_for_identity_deletion entry. The retirement
    row is re-read server-side and its canonical fingerprint is never caller
    input.
    """

    def __init__(self, lifecycle: SqlAlchemyOutboxLifecycle) -> None:
        self._lifecycle = lifecycle

    def request_compaction(
        self,
        *,
        operation_id: str,
        user_id: str,
        deletion_id: str,
        retirement_receipt_id: str,
        connection: Connection,
    ) -> Any:
        return self._lifecycle.request_compaction_for_identity_deletion(
            operation_id=operation_id,
            user_id=user_id,
            deletion_id=deletion_id,
            retirement_receipt_id=retirement_receipt_id,
            connection=connection,
        )


def ensure_business_calendar_locked(runtime: PlatformRuntime) -> CalendarLock:
    """启动时锁定/校验业务日历；时区与已锁版本不一致 → 503 拒绝启动（H3）。

    app lifespan 与 usage maintenance 共享同一 helper（M4），避免启动与 CLI 的
    calendar lock 逻辑漂移。返回锁定的 CalendarLock；未完整组装 → RuntimeError
    （fail-closed，由调用方以平台错误边界暴露）。
    """
    engine = runtime.resolve("database_engine")
    calendar = runtime.resolve("business_calendar")
    if engine is None or calendar is None:
        raise RuntimeError("usage runtime is not fully assembled")
    with engine.begin() as connection:
        return calendar.lock_or_verify(connection)
