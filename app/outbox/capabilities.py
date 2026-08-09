"""Assembly-time signed capability tokens for producer and lifecycle authority.

Both the publisher and the lifecycle commands are authorized by opaque,
signed tokens issued at assembly time. A caller can never self-declare
authority by constructing a dataclass with matching fields: the token's
HMAC-SHA256 signature can only be produced by whoever holds the assembly
secret, and every verifier fails closed when the secret is not configured.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

_TOKEN_PREFIX = "v1"
_DOMAIN = "outbox-capability-v1\0"


def _mac(secret: bytes, payload: str) -> str:
    return hmac.new(secret, (_DOMAIN + payload).encode("utf-8"), hashlib.sha256).hexdigest()


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64url(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def sign_token(
    secret: bytes,
    *,
    kind: str,
    principal: str,
    scope: dict[str, Any] | None = None,
) -> str:
    """Sign a capability token. The payload carries only opaque claims."""
    payload = json.dumps(
        {"kind": kind, "principal": principal, "scope": scope or {}},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    signature = _mac(secret, payload)
    return f"{_TOKEN_PREFIX}.{_b64url(payload.encode('utf-8'))}.{signature}"


def verify_token(secret: bytes, token: str) -> dict[str, Any] | None:
    """Return the token claims when the signature verifies, else None.

    None is returned for malformed tokens, wrong-domain tokens and any
    signature mismatch; callers must treat None as fail-closed denial.
    """
    if not isinstance(token, str) or not token:
        return None
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != _TOKEN_PREFIX:
        return None
    try:
        payload = _unb64url(parts[1]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    expected = _mac(secret, payload)
    if not hmac.compare_digest(expected, parts[2]):
        return None
    try:
        claims = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(claims, dict):
        return None
    return claims


class LifecycleCapabilityIssuer:
    """Assembly-time issuer of lifecycle capability tokens.

    Documents holds the redaction authority; it may also delegate an exact
    inline transaction to retention-ops through `issue_retention_redaction`.
    Retention-ops holds the retirement/compaction authority through the
    assembly token issued by `issue_retention`.
    """

    def __init__(self, secret: bytes) -> None:
        self._secret = secret

    def issue_documents_redaction(
        self,
        *,
        deletion_id: str,
        transaction_id: str,
    ) -> str:
        return sign_token(
            self._secret,
            kind="documents_redact",
            principal="documents",
            scope={
                "deletion_id": deletion_id,
                "transaction_id": transaction_id,
                "mode": "inline",
            },
        )

    def issue_retention_redaction(
        self,
        *,
        deletion_id: str,
        transaction_id: str,
    ) -> str:
        return sign_token(
            self._secret,
            kind="retention_redact",
            principal="retention-ops",
            scope={
                "deletion_id": deletion_id,
                "transaction_id": transaction_id,
                "mode": "inline",
            },
        )

    def issue_retention(self) -> str:
        return sign_token(
            self._secret,
            kind="retention",
            principal="retention-ops",
            scope={},
        )
