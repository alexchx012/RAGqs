from __future__ import annotations

from fastapi import APIRouter, Request

from app.platform.context import current_context

router = APIRouter()


@router.get("/health")
async def health(request: Request) -> dict[str, str]:
    del request
    context = current_context()
    return {
        "status": "ok",
        "service": "core-platform",
        "request_id": context.request_id if context is not None else "req_system",
    }
