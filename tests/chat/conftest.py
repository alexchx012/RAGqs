"""Shared fixtures for chat-generation API and domain tests."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection

from app.chat.models import CalibrationWindowSnapshot, ChatProviderResponse, RetrievalOutcome
from app.chat.ports import ChatProviderRequest, RecordingChatRetrievalPort
from app.chat.schema import chat_metadata
from app.identity.schema import identity_metadata
from app.identity.service import IdentityAccessService
from app.outbox.schema import outbox_metadata
from app.platform.app_factory import create_platform_app
from app.platform.config import load_platform_settings
from app.platform.database import core_metadata
from app.platform.runtime import build_runtime
from app.usage.schema import usage_metadata

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class FixedClock:
    now: datetime

    def now_utc(self, connection: Connection | None = None) -> datetime:
        del connection
        return self.now


class NullObjectStore:
    def exists(self, key: str) -> bool:
        del key
        return False


class FakeChatProvider:
    """Deterministic provider transport for tests."""

    def __init__(self, *, candidate_bias: bool = False) -> None:
        self.calls: list[ChatProviderRequest] = []
        self.fail_next = False
        self.candidate_bias = candidate_bias

    def generate(self, request: ChatProviderRequest) -> ChatProviderResponse:
        self.calls.append(request)
        if self.fail_next:
            self.fail_next = False
            from app.platform.errors import PlatformError

            raise PlatformError("provider_failed", "provider failed", {}, 502)
        suffix = ""
        if request.candidate == 1:
            suffix = " [variant B]" if self.candidate_bias else ""
        context = f" using {request.context_items[0]['snippet']}" if request.context_items else ""
        return ChatProviderResponse(
            content=f"answer for {request.content[:24]}{context}{suffix}",
            input_tokens=10,
            output_tokens=20,
        )


class FakeCalibration:
    def __init__(self, *, window: CalibrationWindowSnapshot | None = None) -> None:
        self.window = window
        self.opt_out_users: set[str] = set()
        self.collected: list[str] = []
        self.golden_seeds: list[dict[str, Any]] = []
        self.adoptions: list[dict[str, Any]] = []

    def get_open_window(
        self, connection: Connection, *, now: datetime, user_id: str
    ) -> CalibrationWindowSnapshot | None:
        del connection, now, user_id
        return self.window

    def user_ab_opt_out(self, connection: Connection, *, user_id: str) -> bool:
        del connection
        return user_id in self.opt_out_users

    def increment_pairs_collected(self, connection: Connection, window_id: str) -> None:
        del connection
        self.collected.append(window_id)

    def record_golden_seed(
        self,
        connection: Connection,
        *,
        pair_id: str,
        space_id: str,
        question_text: str,
        preferred_candidate: int,
        preferred_content: str,
        preferred_citations: Any,
        rejected_candidate: int,
        policy_version: str,
        now: datetime,
    ) -> None:
        del connection
        self.golden_seeds.append(
            {
                "pair_id": pair_id,
                "space_id": space_id,
                "question_text": question_text,
                "preferred_candidate": preferred_candidate,
                "preferred_content": preferred_content,
                "preferred_citations": list(preferred_citations),
                "rejected_candidate": rejected_candidate,
                "policy_version": policy_version,
                "now": now,
            }
        )

    def maybe_adopt_active_default(
        self, connection: Connection, *, space_id: str, now: datetime
    ) -> None:
        del connection
        self.adoptions.append({"space_id": space_id, "now": now})

    def count_effective_ab_votes(self, connection: Connection, *, space_id: str) -> int:
        del connection, space_id
        return 0


class RecordingUsageSubmission:
    """Test usage port: records provider-call lifecycle without a price catalog."""

    def __init__(self) -> None:
        self.prepared: list[str] = []
        self.completed: list[tuple[str, str]] = []
        self.prepared_requests: list[dict[str, Any]] = []
        self.completion_requests: list[dict[str, Any]] = []

    def prepare_provider_call(self, **kwargs: Any) -> str:
        call_id = f"call_{len(self.prepared) + 1}"
        self.prepared.append(call_id)
        self.prepared_requests.append(dict(kwargs))
        return call_id

    def mark_dispatching(self, provider_call_id: str, *, started_at_provider: Any) -> bool:
        del provider_call_id, started_at_provider
        return True

    def complete_provider_call(self, *, provider_call_id: str, result: str, **kwargs: Any) -> str:
        self.completed.append((provider_call_id, result))
        self.completion_requests.append(
            {"provider_call_id": provider_call_id, "result": result, **kwargs}
        )
        return provider_call_id

    def mark_not_sent(self, provider_call_id: str) -> None:
        del provider_call_id

    def mark_unknown(self, provider_call_id: str) -> None:
        del provider_call_id

    def submit_local_usage(self, **kwargs: Any) -> str:
        del kwargs
        return "local-1"

    def recover_unknown_call(self, **kwargs: Any) -> str:
        del kwargs
        return "recover-1"

    def append_usage_adjustment(self, **kwargs: Any) -> str:
        del kwargs
        return "adj-1"

    def append_cost_adjustment(self, **kwargs: Any) -> str:
        del kwargs
        return "cost-adj-1"


def make_settings() -> Any:
    return load_platform_settings(
        {
            "RAG_PLATFORM_PROFILE": "development",
            "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
            "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
            "RAG_PROVIDER_NAME": "fake",
            "RAG_BUSINESS_TIMEZONE": "Asia/Shanghai",
        }
    )


def make_engine() -> Any:
    handle, path = tempfile.mkstemp(prefix="chat_test_", suffix=".sqlite3")
    os.close(handle)
    engine = create_engine(
        f"sqlite+pysqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    chat_metadata.create_all(engine)
    return engine


def open_window(sample_rate: float = 1.0) -> CalibrationWindowSnapshot:
    return CalibrationWindowSnapshot(
        window_id="window_1",
        status="open",
        policy_version="cal-v1",
        sample_rate=sample_rate,
        window_kind="ab",
        expires_at_utc=NOW + timedelta(days=1),
        close_deadline_at_utc=NOW + timedelta(days=1),
    )


def build_test_env(
    *,
    retrieval: RecordingChatRetrievalPort | None = None,
    provider: FakeChatProvider | None = None,
    calibration: FakeCalibration | None = None,
    generation_service: Any | None = None,
    sampler: Any | None = None,
    ab_source_filter: Any | None = None,
    outcomes: dict[str, RetrievalOutcome] | None = None,
):
    engine = make_engine()
    clock = FixedClock(NOW)
    settings = make_settings()
    from app.chat.ports import ChatGenerationRevocationPort

    revocation_port = ChatGenerationRevocationPort()
    identity = IdentityAccessService(
        engine,
        settings.auth,
        now=clock.now_utc,
        revocation_port=revocation_port,
    )
    retrieval = retrieval or RecordingChatRetrievalPort()
    if outcomes:
        retrieval.outcomes.update(outcomes)
    provider = provider or FakeChatProvider()
    calibration = calibration or FakeCalibration()
    from app.chat.generation import GenerationService

    if generation_service is None:
        generation_service = GenerationService(
            engine,
            clock=clock,
            authorization=build_runtime_authorization(identity),
            calibration=calibration,
            ab_source_filter=ab_source_filter,
            sampler=sampler or (lambda: 0.0),
        )
    usage = RecordingUsageSubmission()
    runtime = build_runtime(
        settings,
        adapters={
            "database_engine": engine,
            "database_clock": clock,
            "identity_access": identity,
            "generation_revocation_port": revocation_port,
            "object_store": NullObjectStore(),
            "chat_retrieval_port": retrieval,
            "chat_provider_port": provider,
            "chat_calibration_port": calibration,
            "chat_generation_service": generation_service,
            "chat_usage_submission": usage,
        },
    )
    client = TestClient(create_platform_app(settings, runtime=runtime))
    return {
        "client": client,
        "runtime": runtime,
        "engine": engine,
        "clock": clock,
        "identity": identity,
        "retrieval": retrieval,
        "provider": provider,
        "calibration": calibration,
        "ab_source_filter": ab_source_filter,
        "usage": usage,
    }


def build_runtime_authorization(identity: IdentityAccessService) -> Any:
    from app.chat.ports import IdentityChatAuthorizationPort

    return IdentityChatAuthorizationPort(identity)


def provision_and_login(identity: IdentityAccessService, username: str) -> tuple[str, str]:
    identity.provision_user(
        username=username,
        password="Password1",
        real_name=username.title(),
        display_name=username,
        role="user",
        department_id=None,
    )
    result = identity.login(username=username, password="Password1")
    return result.access_token, result.session_id


def sse_frames(text: str) -> list[tuple[str, int | None, str]]:
    frames: list[tuple[str, int | None, str]] = []
    event: str | None = None
    event_id: int | None = None
    data_lines: list[str] = []

    def flush() -> None:
        if event is not None and data_lines:
            frames.append((event, event_id, "\n".join(data_lines)))

    for line in text.splitlines():
        if not line or line.startswith(":"):
            flush()
            event = None
            event_id = None
            data_lines = []
            continue
        if line.startswith("event:"):
            flush()
            event = line.split(":", 1)[1].strip()
            data_lines = []
        elif line.startswith("id:"):
            event_id = int(line.split(":", 1)[1].strip())
        elif line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].strip())
    flush()
    return frames
