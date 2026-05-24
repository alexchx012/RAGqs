"""Upload validation and prompt-injection screening."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


class UploadSecurityError(ValueError):
    """Raised when an upload violates the configured security policy."""


@dataclass(frozen=True)
class UploadSecurityPolicy:
    allowed_extensions: set[str]
    max_bytes: int
    prompt_injection_scan_enabled: bool = True


@dataclass(frozen=True)
class PromptInjectionFinding:
    pattern: str
    message: str


@dataclass(frozen=True)
class SecureUploadPayload:
    safe_filename: str
    file_path: Path
    extension: str
    content: bytes


PROMPT_INJECTION_PATTERNS = [
    "ignore previous instructions",
    "ignore all previous instructions",
    "reveal the system prompt",
    "print the system prompt",
    "developer message",
    "system message",
]


def parse_allowed_extensions(value: str) -> set[str]:
    """Parse comma-separated extension settings into normalized names."""

    return {
        item.strip().lower().lstrip(".")
        for item in value.split(",")
        if item.strip().lstrip(".")
    }


def secure_upload_payload(
    *,
    filename: str | None,
    content: bytes,
    upload_dir: Path,
    policy: UploadSecurityPolicy,
) -> SecureUploadPayload:
    safe_filename = sanitize_upload_filename(filename)
    extension = _extension_from_filename(safe_filename)
    if extension not in policy.allowed_extensions:
        raise UploadSecurityError(f"unsupported file extension: {extension or '<none>'}")
    if policy.max_bytes < 1:
        raise UploadSecurityError("upload max bytes must be greater than or equal to 1")
    if len(content) > policy.max_bytes:
        raise UploadSecurityError("file size exceeds limit")

    text = _decode_text_content(content)
    if policy.prompt_injection_scan_enabled:
        finding = scan_prompt_injection(text)
        if finding is not None:
            raise UploadSecurityError(f"prompt injection pattern detected: {finding.pattern}")

    return SecureUploadPayload(
        safe_filename=safe_filename,
        file_path=resolve_upload_path(upload_dir, safe_filename),
        extension=extension,
        content=content,
    )


def sanitize_upload_filename(filename: str | None) -> str:
    if not filename:
        raise UploadSecurityError("filename is required")
    basename = Path(str(filename).replace("\\", "/")).name.strip()
    if not basename:
        raise UploadSecurityError("filename is required")
    safe = re.sub(r"\s+", "_", basename)
    safe = re.sub(r'[^A-Za-z0-9._-]', "_", safe)
    safe = safe.strip("._")
    if not safe or safe in {".", ".."}:
        raise UploadSecurityError("filename is invalid")
    return safe


def resolve_upload_path(upload_dir: Path, safe_filename: str) -> Path:
    root = upload_dir.resolve()
    target = (root / safe_filename).resolve()
    if target.parent != root:
        raise UploadSecurityError("upload path escapes upload directory")
    return target


def scan_prompt_injection(text: str) -> PromptInjectionFinding | None:
    normalized = " ".join(text.lower().split())
    for pattern in PROMPT_INJECTION_PATTERNS:
        if pattern in normalized:
            return PromptInjectionFinding(
                pattern=pattern,
                message="high-risk prompt instruction found in uploaded document",
            )
    return None


def _extension_from_filename(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _decode_text_content(content: bytes) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UploadSecurityError("text uploads must be valid UTF-8") from exc
