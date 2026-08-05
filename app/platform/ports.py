"""Protocols shared by platform adapters and later domain changes."""

from .observability import ObservabilityMetricsPort
from .persistence import (
    AuditRecord,
    AuditWriter,
    DatabaseClock,
    IdempotencyStore,
    Lease,
    LeaseStore,
    TransactionManager,
    TransactionScope,
)
from .provider import ProviderPort
from .storage import ObjectStorePort

__all__ = [
    "AuditRecord",
    "AuditWriter",
    "DatabaseClock",
    "IdempotencyStore",
    "Lease",
    "LeaseStore",
    "ObjectStorePort",
    "ObservabilityMetricsPort",
    "ProviderPort",
    "TransactionManager",
    "TransactionScope",
]
