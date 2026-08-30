"""Pre-persistence upload security checks.

Every check here runs before any durable or async ingestion side effect:
the upload path calls ``validate_upload_security`` while fingerprinting a
file, before objects are written or jobs are scheduled. Malware scanning,
injection risk marking and the allowed media-type list are all maintained
in this module only (后端设计 §9.5).
"""

from __future__ import annotations

import re
from typing import Protocol

from app.platform.errors import PlatformError

_ALLOWED_MEDIA_TYPES: frozenset[str] = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/json",
        "application/pdf",
        "image/png",
        "image/jpeg",
        "application/xml",
        "text/xml",
        "application/yaml",
        "text/yaml",
        "application/toml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
    }
)

_MAGIC_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "application/pdf": (b"%PDF-",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
}

# Zip-container office formats: the archive signature is their required magic.
_ZIP_CONTAINER_TYPES: frozenset[str] = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }
)

# Signatures of container formats we do not accept for non-office uploads.
_ARCHIVE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"PK\x03\x04", "zip"),
    (b"PK\x05\x06", "zip"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"Rar!\x1a\x07", "rar"),
    (b"\x1f\x8b", "gzip"),
)

_MEDIA_TYPE_ERROR = "upload_media_type_not_allowed"
_MISMATCH_ERROR = "upload_content_type_mismatch"
_ARCHIVE_ERROR = "upload_archive_not_allowed"
_ENCODING_ERROR = "upload_content_invalid"
_MALWARE_ERROR = "malware_detected"
_MALWARE_UNAVAILABLE_ERROR = "malware_scan_unavailable"

# The EICAR standard antivirus test file, byte-for-byte: the canonical
# detection proof for the local signature scanner.
_EICAR_TEST_FILE = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"

_LOCAL_MALWARE_SIGNATURES: tuple[bytes, ...] = (_EICAR_TEST_FILE,)

# Minimal instruction-override phrasings for the text prompt-injection risk
# marker. Detection only records a closed fact; matched text is never echoed.
_INJECTION_RISK_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+instructions?",
        r"disregard\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)",
        r"reveal\s+(?:your|the)\s+(?:system\s+)?(?:prompt|instructions)",
        r"you\s+are\s+now\s+(?:a|an)\s+\w",
    )
)

INJECTION_RISK_KIND = "prompt_injection_risk"


class MalwareScannerPort(Protocol):
    """Deployment-injectable malware scan; returns True on detection."""

    def scan(self, *, media_kind: str, content: bytes) -> bool: ...


class SignatureMalwareScanner:
    """Local in-process signature scanner; always effective when no external
    scanning service is configured (A3)."""

    def scan(self, *, media_kind: str, content: bytes) -> bool:
        del media_kind
        return any(signature in content for signature in _LOCAL_MALWARE_SIGNATURES)


def scan_prompt_injection_risk(*, media_kind: str, content: bytes) -> dict[str, str] | None:
    """Closed-shape risk fact for confirmed text carriers, or None.

    Runs on the size-bounded upload payload after the UTF-8 validation in the
    upload chain. A hit is metadata only: it never rejects the upload.
    """

    if not _is_text_carrier(media_kind):
        return None
    text = content.decode("utf-8", errors="replace")
    if any(pattern.search(text) for pattern in _INJECTION_RISK_PATTERNS):
        return {"kind": INJECTION_RISK_KIND}
    return None


def _reject(error_code: str, message: str, **details: object) -> None:
    raise PlatformError(error_code, message, details, 422)


def _starts_with_any(content: bytes, signatures: tuple[bytes, ...]) -> bool:
    return any(content.startswith(signature) for signature in signatures)


def _is_text_carrier(media_type: str) -> bool:
    """Confirmed text carriers: decoded and scanned as text (§9.5)."""

    return media_type.startswith("text/") or media_type in {
        "application/json",
        "application/xml",
        "application/yaml",
        "application/toml",
    }


def validate_upload_security(
    *,
    media_kind: str,
    content: bytes,
    scanner: MalwareScannerPort | None = None,
) -> None:
    """Reject uploads that violate the declared media type or safety rules."""

    media_type = media_kind.split(";", 1)[0].strip().casefold()
    if media_type not in _ALLOWED_MEDIA_TYPES:
        _reject(
            _MEDIA_TYPE_ERROR,
            "The uploaded media type is not allowed",
            media_type=media_type,
        )
    for signature, archive_kind in _ARCHIVE_SIGNATURES:
        if content.startswith(signature) and media_type not in _ZIP_CONTAINER_TYPES:
            _reject(
                _ARCHIVE_ERROR,
                "Archived or compressed uploads are not allowed",
                archive_kind=archive_kind,
            )
    expected = _MAGIC_SIGNATURES.get(media_type)
    if expected is not None and not _starts_with_any(content, expected):
        _reject(
            _MISMATCH_ERROR,
            "The uploaded content does not match the declared media type",
            media_type=media_type,
        )
    if media_type in _ZIP_CONTAINER_TYPES and not content.startswith(b"PK\x03\x04"):
        _reject(
            _MISMATCH_ERROR,
            "The uploaded content does not match the declared media type",
            media_type=media_type,
        )
    if _is_text_carrier(media_type):
        if b"\x00" in content:
            _reject(
                _ENCODING_ERROR,
                "Text uploads must not contain NUL bytes",
                media_type=media_type,
            )
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            _reject(
                _ENCODING_ERROR,
                "Text uploads must be valid UTF-8",
                media_type=media_type,
            )
    # Malware scanning runs after every other rule, on the same bounded
    # payload; the local signature scanner stays effective unless a
    # deployment injects an external one. The error object carries only the
    # media type — never scan details or storage locations.
    active_scanner = scanner if scanner is not None else SignatureMalwareScanner()
    try:
        detected = active_scanner.scan(media_kind=media_type, content=content)
    except Exception:
        raise PlatformError(
            _MALWARE_UNAVAILABLE_ERROR,
            "Malware scanning is currently unavailable",
            {},
            503,
        ) from None
    if detected:
        _reject(
            _MALWARE_ERROR,
            "The uploaded file was rejected by malware scanning",
            media_type=media_type,
        )
