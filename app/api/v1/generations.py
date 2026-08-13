"""Generation recovery, stop/retry, feedback and A/B vote routes."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

from app.chat.generation import GenerationService
from app.chat.models import AbVoteRequest, FeedbackRequest
from app.chat.streaming import GenerationStreamService
from app.identity.service import AuthPrincipal
from app.platform.errors import PlatformError

from .dependencies import current_principal

router = APIRouter(tags=["generations"])


class FeedbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vote: Literal["up", "down"]
    reason: Literal["no_grounding", "wrong_citation"] | None = None


class AbVoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pair_id: str
    choice: Literal["0", "1", "neither"]


def _service(request: Request) -> GenerationService:
    service = request.app.state.platform_runtime.resolve("chat_generation_service")
    if not isinstance(service, GenerationService):
        raise RuntimeError("chat generation service is not configured")
    return service


def _stream_service(request: Request) -> GenerationStreamService:
    service = request.app.state.platform_runtime.resolve("chat_stream_service")
    if not isinstance(service, GenerationStreamService):
        raise RuntimeError("chat stream service is not configured")
    return service


def _key(request: Request) -> str:
    key = request.headers.get("Idempotency-Key")
    if not key or not key.strip():
        raise PlatformError("validation_error", "Idempotency-Key is required", {}, 422)
    return key.strip()


def _last_event_id(request: Request) -> int:
    value = request.headers.get("Last-Event-ID")
    if value is None or value == "":
        return 0
    try:
        return max(0, int(value))
    except ValueError as error:
        raise PlatformError(
            "validation_error", "Last-Event-ID must be an integer", {}, 422
        ) from error


def _require_streaming(accept: str | None) -> None:
    if accept is None:
        return
    accepted = [part.strip() for part in accept.split(",")]
    if not any(part == "*/*" or part.startswith("text/event-stream") for part in accepted):
        raise PlatformError(
            "streaming_response_required",
            "This endpoint only returns text/event-stream",
            {},
            406,
        )


@router.get("/generations/{generation_id}/events")
def generation_events(
    generation_id: str,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    accept: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    _require_streaming(accept)
    return StreamingResponse(
        _stream_service(request).stream(
            principal=principal,
            generation_id=generation_id,
            last_event_id=_last_event_id(request),
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/generations/{generation_id}/stop")
def stop_generation(
    generation_id: str,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
) -> JSONResponse:
    result = _service(request).stop(principal=principal, generation_id=generation_id)
    status_code = 200 if result.get("status") == "stopped" else 202
    return JSONResponse(result, status_code=status_code)


@router.post("/generations/{failed_generation_id}/retry")
def retry_generation(
    failed_generation_id: str,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
    accept: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    _require_streaming(accept)
    result = _service(request).retry(
        principal=principal,
        failed_generation_id=failed_generation_id,
        idempotency_key=_key(request),
    )
    return StreamingResponse(
        _stream_service(request).stream(
            principal=principal,
            generation_id=result.generation_id,
            last_event_id=0,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/messages/{message_id}/feedback", status_code=204)
def submit_feedback(
    message_id: str,
    body: FeedbackBody,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
) -> None:
    if body.vote == "down" and body.reason is None:
        raise PlatformError(
            "validation_error", "reason is required for a down vote", {"field": "reason"}, 422
        )
    if body.vote == "up" and body.reason is not None:
        raise PlatformError(
            "validation_error", "reason is not allowed for an up vote", {"field": "reason"}, 422
        )
    _service(request).submit_feedback(
        principal=principal,
        message_id=message_id,
        request=FeedbackRequest(vote=body.vote, down_reason=body.reason),
        idempotency_key=_key(request),
    )


@router.post("/messages/{message_id}/ab-vote")
def submit_ab_vote(
    message_id: str,
    body: AbVoteBody,
    request: Request,
    principal: Annotated[AuthPrincipal, Depends(current_principal)],
) -> JSONResponse:
    result = _service(request).submit_ab_vote(
        principal=principal,
        message_id=message_id,
        request=AbVoteRequest(pair_id=body.pair_id, choice=body.choice),
        idempotency_key=_key(request),
    )
    return JSONResponse(result, status_code=200)
