from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.engine import Connection

from app.identity.archive import IdentityArchiveProofIssuer, IdentityArchiveProofVerifier
from app.identity.cleanup import ObjectStoreAccountDeletionCleanupPort
from app.identity.ports import (
    AccountRetirementConfirmation,
    AccountRetirementRequest,
    UnavailableAccountRetirementGateway,
)
from app.identity.revocation import DurableGenerationRevocationPort
from app.identity.service import IdentityAccessService
from app.outbox.dispatcher import OutboxDispatcher
from app.outbox.lifecycle import SqlAlchemyOutboxLifecycle
from app.outbox.maintenance import NotificationRetentionMaintenance
from app.outbox.metrics import SqlAlchemyOutboxMetrics
from app.outbox.notifications import NotificationMaterializer
from app.outbox.publisher import SqlAlchemyOutboxPublisher
from app.outbox.service import NotificationService

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


def _random_capability_secret() -> bytes:
    """Independent random outbox capability key (never derived from auth)."""
    return secrets.token_bytes(32)


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
    generation_revocation_port = configured.get("generation_revocation_port") or (
        DurableGenerationRevocationPort()
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
        department_work_check=configured.get("department_work_check_port"),
        deletion_cleanup_port=deletion_cleanup_port,
        object_store=object_store,
        account_retirement_gateway=configured.get("account_retirement_gateway"),
        archive_issuer=archive_issuer,
    )
    configured.setdefault("identity_access", identity_access)
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
    # scoped workers/gateway, but `resolve("outbox_lifecycle")` stays None;
    # an injected BYTES capability secret is never resolvable either. No
    # bearer token and no signing secret are generated or stored anywhere on
    # the runtime; the token model remains available only to explicit
    # external/test boundaries that construct the lifecycle directly.
    outbox_lifecycle = configured.pop("outbox_lifecycle", None) or SqlAlchemyOutboxLifecycle(
        engine,
        now=clock.now_utc,
        clock=clock,
        archive_verifier=archive_verifier,
        capability_secret=None,
    )
    if getattr(outbox_lifecycle, "holds_token_signing_secret", False):
        # The injected raw lifecycle is used ONLY as internal no-token scoped
        # assembly input (worker/gateway). A lifecycle whose capability
        # authorizer holds a signing secret must never be assembled here: the
        # secret would become reachable through the worker/gateway object
        # graphs. Fail closed — the secret is never copied or stored.
        raise RuntimeError(
            "outbox_lifecycle override carries a token signing secret; "
            "the runtime only accepts capability_secret=None lifecycles "
            "for internal scoped assembly"
        )
    for _assembly_only_key in (
        "outbox_capability_secret",
        "retention_capability_token",
        "_retention_capability_token",
        "capability_secret",
        "capability_issuer",
        "producer_capabilities",
    ):
        configured.pop(_assembly_only_key, None)
    account_retirement_gateway = configured.get("account_retirement_gateway") or (
        _OutboxAccountRetirementGateway(outbox_lifecycle)
    )
    configured.setdefault("account_retirement_gateway", account_retirement_gateway)
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
    outbox_publisher = configured.get("outbox_publisher") or SqlAlchemyOutboxPublisher(
        engine,
        clock=clock,
        capability_secret=None,
    )
    configured.setdefault("outbox_publisher", outbox_publisher)
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
    return runtime


class _OutboxAccountRetirementGateway:
    """Identity-deletion scoped retirement façade.

    The gateway ONLY retires exactly the account named by an identity deletion
    workflow request, through the lifecycle's internal no-token entry
    `retire_account_for_identity_deletion` (verified archive proof +
    pending-delete account). It carries no bearer token, no signing secret and
    no generic retention capability: the fixed principal/operation type is
    baked into the façade, so a caller cannot submit a token or choose another
    operation.
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
