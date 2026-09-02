from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update

from app.documents.read_models import DocumentsRetrievalVisibilityPort
from app.documents.schema import (
    document_read_leases_table,
    document_versions_table,
    documents_table,
)
from app.platform.errors import PlatformError

from .test_commands import _accept, _upload


def _accepted(service, principal):
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="read-lease-1",
    )
    item = created["items"][0]
    _accept(service, principal, item)
    return item


def _acquire(service, item, owner_id="user_1"):
    with service._engine.begin() as connection:
        return service._acquire_read_lease(
            connection,
            document_id=item["document_id"],
            document_version_id=item["document_version_id"],
            principal_id=owner_id,
        )


def _renew(service, reference):
    return service.renew_read_lease(
        reference_id=reference.reference_id,
        owner_id=reference.owner_id,
        lease_token=reference.lease_token,
    )


def test_read_lease_acquisition_returns_fresh_reference(service, principal) -> None:
    item = _accepted(service, principal)

    first = _acquire(service, item)
    assert first.owner_id == "user_1"
    assert first.lease_token
    assert first.expires_at == datetime(2026, 1, 1, 0, 5, tzinfo=UTC)

    second = _acquire(service, item)
    assert second.reference_id != first.reference_id
    assert second.lease_token != first.lease_token

    with pytest.raises(PlatformError) as error:
        _renew(service, first)
    assert error.value.code == "read_lease_unavailable"

    with service._engine.connect() as connection:
        rows = (
            connection.execute(
                select(document_read_leases_table.c.id).where(
                    document_read_leases_table.c.document_version_id == item["document_version_id"]
                )
            )
            .scalars()
            .all()
        )
    assert rows == [second.reference_id]


def test_content_read_releases_its_read_lease_at_request_end(service, principal) -> None:
    item = _accepted(service, principal)

    assert service.content(principal=principal, document_id=item["document_id"]).body == b"hello"

    # Preview/content requests hand the lease back when they finish
    # instead of leaning on the TTL, so no read-lease row is left behind.
    with service._engine.connect() as connection:
        rows = connection.execute(select(document_read_leases_table.c.id)).scalars().all()
    assert rows == []


def test_read_lease_renewal_extends_expiry_and_rejects_expired(service, principal) -> None:
    item = _accepted(service, principal)
    reference = _acquire(service, item)

    service._now = lambda: datetime(2026, 1, 1, 0, 4, tzinfo=UTC)
    renewed = _renew(service, reference)
    assert renewed.reference_id == reference.reference_id
    assert renewed.lease_token == reference.lease_token
    assert renewed.expires_at == datetime(2026, 1, 1, 0, 9, tzinfo=UTC)

    service._now = lambda: datetime(2026, 1, 1, 0, 10, tzinfo=UTC)
    with pytest.raises(PlatformError) as error:
        _renew(service, renewed)
    assert error.value.code == "read_lease_unavailable"


def test_read_lease_renewal_requires_matching_triple(service, principal) -> None:
    item = _accepted(service, principal)
    reference = _acquire(service, item)

    for owner_id, lease_token in (
        ("user_2", reference.lease_token),
        ("user_1", "not-the-lease-token"),
    ):
        with pytest.raises(PlatformError) as error:
            service.renew_read_lease(
                reference_id=reference.reference_id,
                owner_id=owner_id,
                lease_token=lease_token,
            )
        assert error.value.code == "read_lease_unavailable"


def test_read_lease_renewal_fails_for_purging_version(service, principal) -> None:
    item = _accepted(service, principal)
    reference = _acquire(service, item)

    with service._engine.begin() as connection:
        connection.execute(
            update(document_versions_table)
            .where(document_versions_table.c.id == item["document_version_id"])
            .values(status="purging")
        )

    with pytest.raises(PlatformError) as error:
        _renew(service, reference)
    assert error.value.code == "read_lease_unavailable"
    with pytest.raises(PlatformError) as read_error:
        service.content(principal=principal, document_id=item["document_id"])
    assert read_error.value.code == "document_version_unavailable"


def test_read_lease_renewal_fails_after_document_leaves_active(service, principal) -> None:
    item = _accepted(service, principal)
    reference = _acquire(service, item)

    with service._engine.begin() as connection:
        connection.execute(
            update(documents_table)
            .where(documents_table.c.id == item["document_id"])
            .values(lifecycle_status="pending_delete")
        )

    with pytest.raises(PlatformError) as error:
        _renew(service, reference)
    assert error.value.code == "read_lease_unavailable"


def test_active_read_lease_blocks_version_purge_until_expired(service, principal) -> None:
    created = service.create_initial_upload(
        principal=principal,
        space_id="space_1",
        files=[_upload()],
        idempotency_key="read-lease-retention-1",
    )["items"][0]
    _accept(service, principal, created)
    replacement = service.replace_version(
        principal=principal,
        document_id=created["document_id"],
        expected_version=2,
        file=_upload(content=b"replacement"),
        idempotency_key="read-lease-replace-1",
    )
    _accept(service, principal, replacement)
    service._now = lambda: datetime(2026, 2, 1, tzinfo=UTC)

    _acquire(service, created)
    assert service.purge_retained_versions(limit=10) == []

    service._now = lambda: datetime(2026, 2, 1, 0, 6, tzinfo=UTC)
    assert service.purge_retained_versions(limit=10) == [created["document_version_id"]]


class _PortCandidate:
    def __init__(self, document_id: str, space_id: str) -> None:
        self.document_id = document_id
        self.space_id = space_id


def test_visibility_port_leases_carry_tokens_for_request_release(service, principal) -> None:
    item = _accepted(service, principal)
    candidate = _PortCandidate(item["document_id"], "space_1")

    port = DocumentsRetrievalVisibilityPort(service._engine, service._object_store)
    facts = port.get_visibility_facts((candidate,), principal)
    (fact,) = facts.values()
    lease = fact["read_lease"]
    assert lease["lease_token"]

    port.release_read_leases([lease])
    with service._engine.connect() as connection:
        rows = (
            connection.execute(
                select(document_read_leases_table.c.id).where(
                    document_read_leases_table.c.document_version_id == item["document_version_id"]
                )
            )
            .scalars()
            .all()
        )
    assert rows == []


def test_release_read_leases_deletes_only_matching_triples(service, principal) -> None:
    item = _accepted(service, principal)
    first = _acquire(service, item)
    second = _acquire(service, item, owner_id="user_2")

    port = DocumentsRetrievalVisibilityPort(service._engine)
    port.release_read_leases(
        [
            {
                "reference_id": first.reference_id,
                "owner_id": first.owner_id,
                "lease_token": first.lease_token,
            },
            {
                "reference_id": second.reference_id,
                "owner_id": second.owner_id,
                "lease_token": "not-the-lease-token",
            },
        ]
    )

    with service._engine.connect() as connection:
        rows = (
            connection.execute(
                select(document_read_leases_table.c.id).where(
                    document_read_leases_table.c.document_version_id == item["document_version_id"]
                )
            )
            .scalars()
            .all()
        )
    assert rows == [second.reference_id]
