from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_PASSWORD_ITERATIONS = 310_000
_REPLAY_AAD = b"ragqs:refresh-replay:v1"
_REPLAY_NONCE_BYTES = 12
_REPLAY_PAYLOAD_FIELDS = frozenset({"access_token", "refresh_token", "csrf_token"})


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _strict_b64decode(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${_PASSWORD_ITERATIONS}${_b64encode(salt)}${_b64encode(digest)}"


_DUMMY_PASSWORD_HASH = (
    f"pbkdf2_sha256${_PASSWORD_ITERATIONS}${_b64encode(bytes(16))}${_b64encode(bytes(32))}"
)


def verify_password(password: str, encoded: str | None) -> bool:
    try:
        encoded = encoded or _DUMMY_PASSWORD_HASH
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            _b64decode(salt),
            int(iterations),
        )
        return hmac.compare_digest(actual, _b64decode(expected))
    except (TypeError, ValueError):
        return False


def new_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def sign_access_token(
    secret: bytes,
    *,
    user_id: str,
    auth_session_id: str,
    issued_at: datetime,
    expires_in_seconds: int,
) -> str:
    issued = int(_utc(issued_at).timestamp())
    payload = {
        "sub": user_id,
        "sid": auth_session_id,
        "iat": issued,
        "exp": issued + expires_in_seconds,
        "typ": "access",
    }
    encoded_header = _b64encode(b'{"alg":"HS256","typ":"JWT"}')
    encoded_payload = _b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signed = f"{encoded_header}.{encoded_payload}".encode("ascii")
    signature = _b64encode(hmac.new(secret, signed, hashlib.sha256).digest())
    return f"{encoded_header}.{encoded_payload}.{signature}"


def verify_access_token(secret: bytes, token: str, *, now: datetime) -> dict[str, Any] | None:
    try:
        header, payload, signature = token.split(".")
        signed = f"{header}.{payload}".encode("ascii")
        expected = hmac.new(secret, signed, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64decode(signature)):
            return None
        decoded = json.loads(_b64decode(payload))
        if decoded.get("typ") != "access" or not isinstance(decoded.get("sub"), str):
            return None
        if not isinstance(decoded.get("sid"), str) or int(decoded.get("exp", 0)) <= int(
            _utc(now).timestamp()
        ):
            return None
        return decoded
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return None


def new_csrf_token(secret: bytes, auth_session_id: str) -> str:
    nonce = secrets.token_urlsafe(24)
    signature = hmac.new(
        secret,
        f"csrf:{auth_session_id}:{nonce}".encode(),
        hashlib.sha256,
    ).digest()
    return f"{nonce}.{_b64encode(signature)}"


def verify_csrf_token(secret: bytes, auth_session_id: str, token: str | None) -> bool:
    if not token or "." not in token:
        return False
    nonce, supplied = token.rsplit(".", 1)
    expected = hmac.new(
        secret,
        f"csrf:{auth_session_id}:{nonce}".encode(),
        hashlib.sha256,
    ).digest()
    try:
        return hmac.compare_digest(expected, _b64decode(supplied))
    except ValueError:
        return False


def _replay_key(secret: bytes) -> bytes:
    return hashlib.sha256(b"ragqs:refresh-replay:v1\0" + secret).digest()


def _validated_replay_payload(decoded: object) -> dict[str, str] | None:
    if not isinstance(decoded, dict) or set(decoded) != _REPLAY_PAYLOAD_FIELDS:
        return None
    return decoded if all(isinstance(item, str) for item in decoded.values()) else None


def encrypt_replay_payload(secret: bytes, payload: dict[str, str]) -> str:
    nonce = secrets.token_bytes(_REPLAY_NONCE_BYTES)
    plaintext = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ciphertext = AESGCM(_replay_key(secret)).encrypt(nonce, plaintext, _REPLAY_AAD)
    return ".".join((_b64encode(nonce), _b64encode(ciphertext)))


def decrypt_replay_payload(secret: bytes, value: str) -> dict[str, str] | None:
    try:
        values = value.split(".")
        nonce_value, ciphertext_value = values
        nonce = _strict_b64decode(nonce_value)
        ciphertext = _strict_b64decode(ciphertext_value)
        if len(nonce) != _REPLAY_NONCE_BYTES:
            return None
        decoded = json.loads(
            AESGCM(_replay_key(secret)).decrypt(nonce, ciphertext, _REPLAY_AAD).decode("utf-8")
        )
        return _validated_replay_payload(decoded)
    except (
        AttributeError,
        binascii.Error,
        InvalidTag,
        TypeError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return None


def within(value: datetime, *, now: datetime, duration: timedelta) -> bool:
    return _utc(value) + duration > _utc(now)
