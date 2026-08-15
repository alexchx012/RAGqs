"""Shared fixtures for evaluation & calibration domain and API tests."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection

from app.chat.schema import chat_metadata
from app.evaluation.judge import JudgeRequest
from app.evaluation.models import JudgeScores
from app.evaluation.schema import evaluation_metadata
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


class FakeJudgeProvider:
    def __init__(
        self,
        *,
        scores: JudgeScores | None = None,
        fail_preflight: bool = False,
    ) -> None:
        self.calls: list[JudgeRequest] = []
        self.scores = scores or JudgeScores(
            faithfulness=0.9, answer_relevancy=0.8, is_refusal=False, latency_ms=100
        )
        self.fail_preflight = fail_preflight
        self.preflight_calls = 0

    def preflight_probe(self) -> None:
        self.preflight_calls += 1
        if self.fail_preflight:
            from app.platform.errors import PlatformError

            raise PlatformError(
                "evaluation_judge_unavailable",
                "judge preflight failed",
                {"retryable": True},
                503,
                True,
            )

    def judge(self, request: JudgeRequest) -> JudgeScores:
        self.calls.append(request)
        return self.scores


class RecordingUsageSubmission:
    def __init__(self) -> None:
        self.prepared: list[dict] = []
        self.completed: list[dict] = []
        self.not_sent: list[str] = []

    def prepare_provider_call(self, **kwargs: Any) -> str:
        call_id = f"call_{len(self.prepared) + 1}"
        self.prepared.append({"call_id": call_id, **kwargs})
        return call_id

    def mark_dispatching(self, provider_call_id: str, *, started_at_provider: Any) -> bool:
        del provider_call_id, started_at_provider
        return True

    def complete_provider_call(self, **kwargs: Any) -> str:
        call_id = kwargs["provider_call_id"]
        self.completed.append({**kwargs, "provider_call_id": call_id})
        return call_id

    def mark_not_sent(self, provider_call_id: str) -> None:
        self.not_sent.append(provider_call_id)

    def mark_unknown(self, provider_call_id: str) -> None:
        del provider_call_id


class FakeChatFactsPort:
    def __init__(self, *, sample_count: int = 60) -> None:
        self.samples = [
            {
                "item_id": f"item_{index}",
                "position": index,
                "question_text": f"question {index}",
                "question_hash": f"hash_{index}",
                "evidence_hash": f"evidence_{index}",
                "weak_signals": {},
                "source_ref": f"msg_{index}",
            }
            for index in range(1, sample_count + 1)
        ]
        self.calls: list[tuple[str, int]] = []

    def collect_samples(self, connection: Connection, *, space_id: str, limit: int) -> tuple:
        del connection
        self.calls.append((space_id, limit))
        return tuple(self.samples[:limit])


class FakeCandidateConfigSource:
    def __init__(self, *, versions: tuple[str, ...] = ("default", "candidate_b")) -> None:
        self.versions = versions

    def candidate_config_versions(self, *, space_id: str) -> tuple[str, ...]:
        del space_id
        return self.versions


class FakeIndexGenerationSource:
    def __init__(self, *, generation_id: str = "gen_active", revision: int = 1) -> None:
        self.generation_id = generation_id
        self.revision = revision

    def active_generation(self) -> tuple[str, int]:
        return self.generation_id, self.revision


class FakeRetrievalReplayPort:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def replay(self, **kwargs: Any) -> dict:
        self.calls.append(kwargs)
        return {
            "session_id": kwargs["session_id"],
            "candidate_config_version": kwargs["candidate_config_version"],
            "hits": (),
            "degradations": (),
        }


class FakeAnswerReplayPort:
    def __init__(self, *, answer: str = "replayed answer") -> None:
        self.answer = answer
        self.calls: list[dict] = []

    def replay(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return self.answer


class RecordingCalibrationOutboxPort:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def publish_suggested(self, **kwargs: Any) -> str:
        self.events.append(kwargs)
        return f"evt_{len(self.events)}"


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
    handle, path = tempfile.mkstemp(prefix="eval_test_", suffix=".sqlite3")
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
    evaluation_metadata.create_all(engine)
    return engine


def build_test_env(
    *,
    judge: FakeJudgeProvider | None = None,
    chat_facts: FakeChatFactsPort | None = None,
    candidate_configs: FakeCandidateConfigSource | None = None,
    index_generation: FakeIndexGenerationSource | None = None,
    retrieval: FakeRetrievalReplayPort | None = None,
    answer_replay: FakeAnswerReplayPort | None = None,
    outbox: RecordingCalibrationOutboxPort | None = None,
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
    judge = judge or FakeJudgeProvider()
    chat_facts = chat_facts or FakeChatFactsPort()
    candidate_configs = candidate_configs or FakeCandidateConfigSource()
    index_generation = index_generation or FakeIndexGenerationSource()
    retrieval = retrieval or FakeRetrievalReplayPort()
    answer_replay = answer_replay or FakeAnswerReplayPort()
    outbox = outbox or RecordingCalibrationOutboxPort()
    runtime = build_runtime(
        settings,
        adapters={
            "database_engine": engine,
            "database_clock": clock,
            "identity_access": identity,
            "generation_revocation_port": revocation_port,
            "object_store": NullObjectStore(),
            "judge_provider": judge,
            "sample_snapshot_source": chat_facts,
            "candidate_config_source": candidate_configs,
            "index_generation_source": index_generation,
            "retrieval_replay_port": retrieval,
            "answer_replay_port": answer_replay,
            "calibration_outbox_port": outbox,
            "evaluation_usage_submission": RecordingUsageSubmission(),
        },
    )
    client = TestClient(create_platform_app(settings, runtime=runtime))
    return {
        "client": client,
        "runtime": runtime,
        "engine": engine,
        "clock": clock,
        "identity": identity,
        "judge": judge,
        "chat_facts": chat_facts,
        "candidate_configs": candidate_configs,
        "index_generation": index_generation,
        "retrieval": retrieval,
        "answer_replay": answer_replay,
        "outbox": outbox,
    }


def provision_and_login(
    identity: IdentityAccessService,
    username: str,
    role: str = "user",
) -> tuple[str, str, str]:
    identity.provision_user(
        username=username,
        password="Password1",
        real_name=username.title(),
        display_name=username,
        role=role,  # type: ignore[arg-type]
        department_id=None,
    )
    result = identity.login(username=username, password="Password1")
    return result.access_token, result.session_id, str(result.user["id"])
