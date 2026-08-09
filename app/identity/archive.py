"""Identity-owned archive proof.

Identity is the archive owner: when an account is purged, identity issues an
HMAC-signed archive reference bound to the user, deletion and cleanup
operation. The outbox retirement lifecycle validates the opaque reference
against this completed archive proof through the verifier, so no
client-supplied value is ever trusted and a proof for one account/deletion can
never be replayed for another.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_ARCHIVE_PREFIX = "identity-archive:"


class IdentityArchiveProofIssuer:
    """Issues HMAC archive proofs bound to user/deletion/workflow."""

    def __init__(self, secret: bytes) -> None:
        if not secret:
            raise ValueError("archive proof secret must not be empty")
        self._secret = secret

    def issue(
        self,
        *,
        user_id: str,
        deletion_id: str,
        cleanup_operation_id: str,
        requested_at: str,
    ) -> tuple[str, str]:
        archive_ref = (
            f"{_ARCHIVE_PREFIX}{user_id}:{deletion_id}:{cleanup_operation_id}:{requested_at}"
        )
        checksum = hmac.new(
            self._secret,
            f"archive-proof-v1\0{user_id}\0{deletion_id}\0{cleanup_operation_id}\0{requested_at}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return archive_ref, checksum


class IdentityArchiveProofVerifier:
    """Validates HMAC archive proofs and their user/deletion/workflow binding."""

    def __init__(self, secret: bytes) -> None:
        if not secret:
            raise ValueError("archive proof secret must not be empty")
        self._secret = secret

    def verify_archive(
        self,
        *,
        archive_ref: str,
        checksum: str,
        user_id: str | None = None,
        deletion_id: str | None = None,
        cleanup_operation_id: str | None = None,
    ) -> bool:
        if not archive_ref.startswith(_ARCHIVE_PREFIX):
            return False
        try:
            _prefix, ref_user, ref_deletion, ref_cleanup, ref_requested = archive_ref.split(":", 4)
        except ValueError:
            return False
        if user_id is not None and ref_user != user_id:
            return False
        if deletion_id is not None and ref_deletion != deletion_id:
            return False
        if cleanup_operation_id is not None and ref_cleanup != cleanup_operation_id:
            return False
        expected = hmac.new(
            self._secret,
            f"archive-proof-v1\0{ref_user}\0{ref_deletion}\0{ref_cleanup}\0{ref_requested}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return secrets.compare_digest(expected, checksum)
