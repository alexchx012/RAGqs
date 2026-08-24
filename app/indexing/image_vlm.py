"""Image description provider boundary (bailian / InternVL / none).

Providers return a typed ``ImageDescriptionResult``; errors map to stable
codes instead of empty strings. Bailian submits provider usage and product
quota separately; InternVL is a deployed local stage and submits local usage
only. The ``none`` provider is development/CI-only and keeps OCR/existing text
with a degraded marker.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import struct
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from app.platform.errors import PlatformError

_Transport = Callable[[str, str, Mapping[str, str], Mapping[str, Any]], Mapping[str, Any]]
_UsageSink = Callable[[Mapping[str, Any]], None]

DECORATIVE_MIN_PIXELS = 100
DECORATIVE_MIN_PAGE_RATIO = 0.02


def is_decorative(
    width: int,
    height: int,
    *,
    page_area: int | None = None,
    min_pixels: int = DECORATIVE_MIN_PIXELS,
    min_page_ratio: float = DECORATIVE_MIN_PAGE_RATIO,
) -> bool:
    if width < min_pixels or height < min_pixels:
        return True
    return page_area is not None and page_area > 0 and (width * height) / page_area < min_page_ratio


def image_dimensions(content: bytes) -> tuple[int, int] | None:
    """Read intrinsic PNG/GIF/JPEG dimensions without a parser dependency."""

    if content.startswith(b"\x89PNG\r\n\x1a\n") and len(content) >= 24:
        width, height = struct.unpack(">II", content[16:24])
        return width, height
    if content.startswith((b"GIF87a", b"GIF89a")) and len(content) >= 10:
        width, height = struct.unpack("<HH", content[6:10])
        return width, height
    if content.startswith(b"\xff\xd8"):
        offset = 2
        while offset + 9 < len(content):
            if content[offset] != 0xFF:
                offset += 1
                continue
            marker = content[offset + 1]
            if marker in {0xD8, 0x01, 0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7}:
                offset += 2
                continue
            if offset + 4 > len(content):
                break
            length = struct.unpack(">H", content[offset + 2 : offset + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in {0xC4, 0xC8, 0xCC}:
                if offset + 9 <= len(content):
                    height, width = struct.unpack(">HH", content[offset + 5 : offset + 9])
                    return width, height
            offset += 2 + length
    return None


@dataclass(frozen=True, slots=True)
class ImageDescriptionResult:
    text: str
    indexable: bool
    degraded: bool
    reason: str
    provider: str = "none"
    usage: Mapping[str, Any] | None = None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "indexable": self.indexable,
            "degraded": self.degraded,
            "reason": self.reason,
            "provider": self.provider,
            **({"usage": dict(self.usage)} if self.usage is not None else {}),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ImageDescriptionResult:
        usage = value.get("usage")
        return cls(
            text=str(value.get("text", "")),
            indexable=bool(value.get("indexable", True)),
            degraded=bool(value.get("degraded", False)),
            reason=str(value.get("reason", "ok")),
            provider=str(value.get("provider", "unknown")),
            usage=dict(usage) if isinstance(usage, Mapping) else None,
        )


def describe_prompt(context: Mapping[str, Any]) -> str:
    """Serialize caption/section/preceding/following context and OCR tags."""

    lines = ["Describe this image for document retrieval and answer generation."]
    for name in ("caption", "section_path", "preceding_text", "following_text"):
        value = str(context.get(name, "")).strip()
        if value:
            lines.append(f"{name}: {value}")
    ocr_text = str(context.get("ocr_text", "")).strip()
    if ocr_text:
        lines.append(f"ocr_tag: {ocr_text}")
    return "\n".join(lines)


def _decode_image(content: bytes, media_kind: str) -> str:
    media = media_kind if "/" in media_kind else "image/png"
    try:
        encoded = base64.b64encode(content).decode("ascii")
    except (TypeError, ValueError, binascii.Error) as exc:
        raise PlatformError(
            "image_provider_schema_error", "Image payload could not be encoded", {}, 422
        ) from exc
    return f"data:{media};base64,{encoded}"


class BailianImageDescriber:
    """OpenAI-compatible multimodal transport for bailian qwen-vl models."""

    provider = "bailian"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = "qwen-vl-plus",
        timeout_seconds: int = 60,
        usage_sink: _UsageSink | None = None,
        transport: _Transport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = float(timeout_seconds)
        self._usage_sink = usage_sink
        self._transport = transport or self._http_transport

    def _http_transport(
        self, url: str, payload: str, headers: Mapping[str, str], options: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        import httpx

        try:
            response = httpx.post(
                url,
                content=payload,
                headers=dict(headers),
                timeout=float(options.get("timeout_seconds", 60.0)),
            )
        except httpx.TimeoutException as exc:
            raise PlatformError(
                "image_provider_timeout", "Image VLM provider timed out", {}, 422
            ) from exc
        except httpx.HTTPError as exc:
            raise PlatformError(
                "image_provider_unavailable", "Image VLM provider is unavailable", {}, 422
            ) from exc
        if response.status_code >= 500:
            raise PlatformError(
                "image_provider_unavailable", "Image VLM provider is unavailable", {}, 422
            )
        if response.status_code >= 400:
            raise PlatformError(
                "image_provider_schema_error", "Image VLM provider rejected the request", {}, 422
            )
        try:
            value = json.loads(response.text)
        except ValueError as exc:
            raise PlatformError(
                "image_provider_schema_error", "Image VLM response was malformed", {}, 422
            ) from exc
        if not isinstance(value, Mapping):
            raise PlatformError(
                "image_provider_schema_error", "Image VLM response was malformed", {}, 422
            )
        return value

    def __call__(self, content: bytes, context: Mapping[str, Any]) -> ImageDescriptionResult:
        media_kind = str(context.get("media_kind", "image/png"))
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": describe_prompt(context)},
                        {
                            "type": "image_url",
                            "image_url": {"url": _decode_image(content, media_kind)},
                        },
                    ],
                }
            ],
        }
        started = time.monotonic()
        response = self._transport(
            f"{self._base_url}/chat/completions",
            json.dumps(payload, ensure_ascii=False),
            {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
            {"timeout_seconds": self._timeout_seconds},
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        try:
            text = str(response["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise PlatformError(
                "image_provider_schema_error", "Image VLM response was malformed", {}, 422
            ) from exc
        usage_raw = response.get("usage")
        usage = dict(usage_raw) if isinstance(usage_raw, Mapping) else None
        if usage is None:
            usage = {}
        usage = {
            "provider": self.provider,
            "model": self._model,
            "image_count": 1,
            "latency_ms": latency_ms,
            **usage,
        }
        if self._usage_sink is not None:
            # Provider usage and the product quota debit are separate facts;
            # quota debits ride the existing publication page ledger.
            self._usage_sink({"kind": "provider_usage", **usage})
        return ImageDescriptionResult(
            text=text,
            indexable=bool(text),
            degraded=not bool(text),
            reason="ok" if text else "empty_description",
            provider=self.provider,
            usage=usage,
        )


class InternVLImageDescriber:
    """Deployed InternVL endpoint profile: local usage only, no product quota."""

    provider = "internvl"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        model: str,
        revision: str = "",
        timeout_seconds: int = 120,
        usage_sink: _UsageSink | None = None,
        transport: _Transport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._revision = revision
        self._timeout_seconds = float(timeout_seconds)
        self._usage_sink = usage_sink
        self._transport = (
            transport
            or BailianImageDescriber(
                base_url=base_url, api_key=api_key or "", model=model
            )._http_transport
        )

    def __call__(self, content: bytes, context: Mapping[str, Any]) -> ImageDescriptionResult:
        def local_usage_sink(fact: Mapping[str, Any]) -> None:
            if self._usage_sink is not None:
                self._usage_sink(
                    {
                        **fact,
                        "kind": "local_usage",
                        "stage": "image_vlm_internvl",
                        "revision": self._revision,
                    }
                )

        describer = BailianImageDescriber(
            base_url=self._base_url,
            api_key=self._api_key or "",
            model=self._model,
            timeout_seconds=int(self._timeout_seconds),
            usage_sink=local_usage_sink if self._usage_sink is not None else None,
            transport=self._transport,
        )
        result = describer(content, context)
        return ImageDescriptionResult(
            text=result.text,
            indexable=result.indexable,
            degraded=result.degraded,
            reason=result.reason,
            provider=self.provider,
            usage={**(result.usage or {}), "provider": self.provider, "revision": self._revision},
        )


class NoneImageDescriber:
    """Non-production provider: keep OCR/existing text, mark degraded."""

    provider = "none"

    def __init__(self, *, environment: str = "development") -> None:
        if environment == "production":
            raise PlatformError(
                "image_provider_unavailable",
                "IMAGE_VLM_PROVIDER=none is not allowed in production",
                {},
                422,
            )
        self._environment = environment

    def __call__(self, content: bytes, context: Mapping[str, Any]) -> ImageDescriptionResult:
        del content
        parts = [
            str(context.get(name, "")).strip()
            for name in ("caption", "ocr_text", "preceding_text", "following_text")
        ]
        text = "\n".join(part for part in parts if part)
        return ImageDescriptionResult(
            text=text,
            indexable=bool(text),
            degraded=True,
            reason="ok" if text else "degraded_no_text",
            provider=self.provider,
            usage={"provider": self.provider, "environment": self._environment},
        )


def normalize_description(
    value: ImageDescriptionResult | Mapping[str, Any] | str,
) -> ImageDescriptionResult:
    if isinstance(value, ImageDescriptionResult):
        return value
    if isinstance(value, Mapping):
        return ImageDescriptionResult.from_mapping(value)
    text = str(value).strip()
    return ImageDescriptionResult(
        text=text,
        indexable=bool(text),
        degraded=not bool(text),
        reason="ok" if text else "empty_description",
        provider="legacy",
    )


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def safe_reason(value: str) -> str:
    return _CONTROL.sub("", str(value))[:200]


__all__ = [
    "BailianImageDescriber",
    "ImageDescriptionResult",
    "InternVLImageDescriber",
    "NoneImageDescriber",
    "image_dimensions",
    "is_decorative",
    "normalize_description",
]
