"""Runtime request controls for local internal-trial hardening."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request

from app.config import config
from app.models.response import envelope_json_response


@dataclass(frozen=True)
class RuntimeControlSettings:
    enabled: bool
    max_concurrent_requests: int
    queue_timeout_seconds: float
    request_timeout_seconds: float
    excluded_paths: tuple[str, ...]

    @classmethod
    def from_settings(cls, settings: Any = config) -> RuntimeControlSettings:
        runtime_group = getattr(settings, "runtime", None)
        return cls(
            enabled=_setting_bool(settings, runtime_group, "runtime_controls_enabled", "enabled", False),
            max_concurrent_requests=int(
                _setting_value(
                    settings,
                    runtime_group,
                    "runtime_max_concurrent_requests",
                    "max_concurrent_requests",
                    40,
                )
            ),
            queue_timeout_seconds=float(
                _setting_value(
                    settings,
                    runtime_group,
                    "runtime_queue_timeout_seconds",
                    "queue_timeout_seconds",
                    2.0,
                )
            ),
            request_timeout_seconds=float(
                _setting_value(
                    settings,
                    runtime_group,
                    "runtime_request_timeout_seconds",
                    "request_timeout_seconds",
                    60.0,
                )
            ),
            excluded_paths=_parse_paths(
                _setting_value(
                    settings,
                    runtime_group,
                    "runtime_control_excluded_paths",
                    "control_excluded_paths",
                    "/health,/static",
                )
            ),
        )

    def is_excluded(self, path: str) -> bool:
        return any(path == prefix or path.startswith(f"{prefix}/") for prefix in self.excluded_paths)


def install_runtime_controls_middleware(app: FastAPI, *, settings: Any = config) -> None:
    """Install process-local concurrency and timeout controls."""

    controls = RuntimeControlSettings.from_settings(settings)
    if not controls.enabled or controls.max_concurrent_requests < 1:
        return

    semaphore = asyncio.Semaphore(controls.max_concurrent_requests)

    @app.middleware("http")
    async def runtime_controls_middleware(request: Request, call_next):
        if controls.is_excluded(request.url.path):
            return await call_next(request)

        try:
            await asyncio.wait_for(
                semaphore.acquire(),
                timeout=max(controls.queue_timeout_seconds, 0.001),
            )
        except TimeoutError:
            return envelope_json_response(
                {
                    "success": False,
                    "errorMessage": "request concurrency limit reached",
                    "status": "rejected",
                },
                code=429,
                message="request concurrency limit reached",
            )

        try:
            if controls.request_timeout_seconds <= 0:
                return await call_next(request)
            return await asyncio.wait_for(
                call_next(request),
                timeout=controls.request_timeout_seconds,
            )
        except TimeoutError:
            return envelope_json_response(
                {
                    "success": False,
                    "errorMessage": "request timed out",
                    "status": "timeout",
                },
                code=504,
                message="request timed out",
            )
        finally:
            semaphore.release()


def _setting_value(
    settings: Any,
    group: Any,
    flat_name: str,
    group_name: str,
    default: Any,
) -> Any:
    if group is not None and hasattr(group, group_name):
        return getattr(group, group_name)
    return getattr(settings, flat_name, default)


def _setting_bool(
    settings: Any,
    group: Any,
    flat_name: str,
    group_name: str,
    default: bool,
) -> bool:
    value = _setting_value(settings, group, flat_name, group_name, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_paths(value: str) -> tuple[str, ...]:
    paths = []
    for item in str(value or "").split(","):
        path = item.strip()
        if path:
            paths.append(path.rstrip("/") or "/")
    return tuple(paths)
