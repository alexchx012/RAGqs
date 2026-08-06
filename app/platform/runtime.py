from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.identity.cleanup import ObjectStoreAccountDeletionCleanupPort
from app.identity.revocation import DurableGenerationRevocationPort
from app.identity.service import IdentityAccessService

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
    identity_access = configured.get("identity_access") or IdentityAccessService(
        engine,
        settings.auth,
        now=clock.now_utc,
        revocation_port=generation_revocation_port,
        department_work_check=configured.get("department_work_check_port"),
        deletion_cleanup_port=deletion_cleanup_port,
        object_store=object_store,
    )
    configured.setdefault("identity_access", identity_access)
    return PlatformRuntime(settings=settings, adapters=configured)
