"""Pre-persistence upload security checks.

Every check here runs before any durable or async ingestion side effect:
the upload path calls ``validate_upload_security`` while fingerprinting a
file, before objects are written or jobs are scheduled.
"""

from __future__ import annotations

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
_MISMATCH_ERROR = "upload_media_mismatch"
_ARCHIVE_ERROR = "upload_archive_not_allowed"
_ENCODING_ERROR = "upload_content_invalid"


def _reject(error_code: str, message: str, **details: object) -> None:
    raise PlatformError(error_code, message, details, 422)


def _starts_with_any(content: bytes, signatures: tuple[bytes, ...]) -> bool:
    return any(content.startswith(signature) for signature in signatures)


def validate_upload_security(*, media_kind: str, content: bytes) -> None:
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
    if media_type.startswith("text/") or media_type in {
        "application/json",
        "application/xml",
        "application/yaml",
        "application/toml",
    }:
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
