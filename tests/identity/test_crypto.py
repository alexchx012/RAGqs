from __future__ import annotations

import base64
import hashlib
import hmac
import json

from app.identity.crypto import decrypt_replay_payload, encrypt_replay_payload


def _legacy_b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _legacy_replay_payload(secret: bytes, payload: dict[str, str]) -> str:
    nonce = b"legacy-replay-v1"
    plaintext = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    stream = b""
    counter = 0
    while len(stream) < len(plaintext):
        stream += hmac.new(
            secret,
            nonce + counter.to_bytes(4, "big"),
            hashlib.sha256,
        ).digest()
        counter += 1
    ciphertext = bytes(
        left ^ right for left, right in zip(plaintext, stream[: len(plaintext)], strict=True)
    )
    tag = hmac.new(secret, nonce + ciphertext, hashlib.sha256).digest()
    return ".".join((_legacy_b64(nonce), _legacy_b64(ciphertext), _legacy_b64(tag)))


def test_replay_payload_uses_aead_envelope_and_rejects_tampering() -> None:
    secret = b"test-secret-that-is-long-enough"
    payload = {
        "access_token": "access",
        "refresh_token": "refresh",
        "csrf_token": "csrf",
    }

    encrypted = encrypt_replay_payload(secret, payload)
    nonce, ciphertext = encrypted.split(".")

    assert nonce
    assert ciphertext
    assert decrypt_replay_payload(secret, encrypted) == payload
    replacement = "A" if ciphertext[-1] != "A" else "B"
    assert decrypt_replay_payload(secret, f"{nonce}.{ciphertext[:-1]}{replacement}") is None
    assert decrypt_replay_payload(secret, f"{nonce}.{ciphertext[:2]}!{ciphertext[2:]}") is None
    assert decrypt_replay_payload(b"another-test-secret", encrypted) is None


def test_replay_payload_rejects_the_removed_legacy_envelope() -> None:
    secret = b"test-secret-that-is-long-enough"
    payload = {
        "access_token": "access",
        "refresh_token": "refresh",
        "csrf_token": "csrf",
    }

    legacy = _legacy_replay_payload(secret, payload)

    assert decrypt_replay_payload(secret, legacy) is None
