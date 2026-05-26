import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.security.runtime_controls import install_runtime_controls_middleware

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_runtime_controls_reject_when_concurrency_queue_times_out():
    started = asyncio.Event()
    release = asyncio.Event()
    app = FastAPI()
    install_runtime_controls_middleware(
        app,
        settings=_settings(
            runtime_controls_enabled=True,
            runtime_max_concurrent_requests=1,
            runtime_queue_timeout_seconds=0.01,
            runtime_request_timeout_seconds=1.0,
        ),
    )

    @app.get("/slow")
    async def slow():
        started.set()
        await release.wait()
        return {"ok": True}

    async with _client(app) as client:
        first_request = asyncio.create_task(client.get("/slow"))
        await started.wait()
        second_response = await client.get("/slow")
        release.set()
        first_response = await first_request

    assert first_response.status_code == 200
    assert second_response.status_code == 429
    assert second_response.json() == {
        "code": 429,
        "message": "request concurrency limit reached",
        "data": {
            "success": False,
            "errorMessage": "request concurrency limit reached",
            "status": "rejected",
        },
    }


@pytest.mark.asyncio
async def test_runtime_controls_return_timeout_response_for_slow_requests():
    app = FastAPI()
    install_runtime_controls_middleware(
        app,
        settings=_settings(
            runtime_controls_enabled=True,
            runtime_max_concurrent_requests=2,
            runtime_queue_timeout_seconds=0.5,
            runtime_request_timeout_seconds=0.01,
        ),
    )

    @app.get("/slow")
    async def slow():
        await asyncio.sleep(0.05)
        return {"ok": True}

    async with _client(app) as client:
        response = await client.get("/slow")

    assert response.status_code == 504
    assert response.json()["message"] == "request timed out"
    assert response.json()["data"]["status"] == "timeout"


@pytest.mark.asyncio
async def test_runtime_controls_can_be_disabled():
    app = FastAPI()
    install_runtime_controls_middleware(
        app,
        settings=_settings(
            runtime_controls_enabled=False,
            runtime_max_concurrent_requests=1,
            runtime_request_timeout_seconds=0.01,
        ),
    )

    @app.get("/slow")
    async def slow():
        await asyncio.sleep(0.02)
        return {"ok": True}

    async with _client(app) as client:
        response = await client.get("/slow")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def _settings(**overrides):
    values = {
        "runtime_controls_enabled": True,
        "runtime_max_concurrent_requests": 10,
        "runtime_queue_timeout_seconds": 1.0,
        "runtime_request_timeout_seconds": 30.0,
        "runtime_control_excluded_paths": "/health,/static",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _client(app: FastAPI):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


def test_fake_load_script_declares_scope_and_fake_provider_contract():
    script = (ROOT / "scripts" / "run-fake-load.ps1").read_text(encoding="utf-8")

    for phrase in [
        "CHAT_PROVIDER=fake",
        "EMBEDDING_PROVIDER=fake",
        "VECTOR_STORE_PROVIDER=fake",
        "does not prove real 80-user capacity or answer quality",
        "verified",
        "skipped",
        "failed",
    ]:
        assert phrase in script


def test_config_validation_rejects_invalid_enabled_runtime_controls():
    from app.config import Settings
    from app.operations.config_validation import validate_settings

    report = validate_settings(
        Settings(
            _env_file=None,
            dashscope_api_key="sk-valid",
            runtime_controls_enabled=True,
            runtime_max_concurrent_requests=0,
            runtime_queue_timeout_seconds=0,
            runtime_request_timeout_seconds=0,
        )
    )

    assert report.is_valid is False
    issues = {(issue.field, issue.message) for issue in report.errors}
    assert (
        "RUNTIME_MAX_CONCURRENT_REQUESTS",
        "must be greater than or equal to 1 when runtime controls are enabled",
    ) in issues
    assert (
        "RUNTIME_QUEUE_TIMEOUT_SECONDS",
        "must be greater than 0 when runtime controls are enabled",
    ) in issues
    assert (
        "RUNTIME_REQUEST_TIMEOUT_SECONDS",
        "must be greater than 0 when runtime controls are enabled",
    ) in issues
