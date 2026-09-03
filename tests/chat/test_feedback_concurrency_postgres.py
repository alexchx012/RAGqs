"""PostgreSQL acceptance: concurrent same-key feedback resolves to the 409 contract.

The ``chat_message_feedback`` primary key (message_id, voter_user_id) is the
concurrency boundary. Two racing submissions with different idempotency keys
must not leak an IntegrityError: the winner commits, the loser maps the real
unique violation to ``feedback_already_submitted`` (409).
"""

from __future__ import annotations

import os
import threading
import time
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import URL, make_url

from app.chat.generation import GenerationService
from app.chat.models import FeedbackRequest
from app.chat.schema import (
    chat_conversation_table,
    chat_message_feedback_table,
    chat_message_table,
)
from app.identity.schema import identity_metadata
from app.identity.service import IdentityAccessService
from app.outbox.schema import outbox_metadata
from app.platform.database import core_metadata
from app.platform.errors import PlatformError
from app.usage.schema import usage_metadata

from .conftest import (
    NOW,
    FakeCalibration,
    FixedClock,
    build_runtime_authorization,
    make_settings,
    provision_and_login,
)

pytestmark = pytest.mark.integration

_PG_URL_ENV = "RAGQS_TEST_POSTGRES_URL"


def _postgres_test_url() -> URL:
    url = os.environ.get(_PG_URL_ENV)
    if not url:
        pytest.skip(
            "PostgreSQL concurrency acceptance requires RAGQS_TEST_POSTGRES_URL "
            "(NOT RUN/BLOCKED)"
        )
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql":
        pytest.skip("RAGQS_TEST_POSTGRES_URL must use a postgresql backend (NOT RUN/BLOCKED)")
    if parsed.database is None or "test" not in parsed.database.lower():
        pytest.skip("RAGQS_TEST_POSTGRES_URL database name must contain 'test' (NOT RUN/BLOCKED)")
    return parsed


@pytest.fixture()
def pg_feedback_engine():
    base_url = _postgres_test_url()
    schema = f"feedback_race_{uuid4().hex[:12]}"
    base_engine = create_engine(base_url)
    scoped_engine = None
    try:
        with base_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        query = dict(base_url.query)
        existing_options = str(query.get("options", "")).strip()
        query["options"] = f"{existing_options} -csearch_path={schema}".strip()
        scoped_engine = create_engine(base_url.set(query=query))
        core_metadata.create_all(scoped_engine)
        identity_metadata.create_all(scoped_engine)
        outbox_metadata.create_all(scoped_engine)
        usage_metadata.create_all(scoped_engine)
        from app.chat.schema import chat_metadata

        chat_metadata.create_all(scoped_engine)
        yield scoped_engine
    finally:
        if scoped_engine is not None:
            scoped_engine.dispose()
        with base_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        base_engine.dispose()


def _seed_completed_assistant_message(engine, *, owner_user_id: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            chat_conversation_table.insert().values(
                id="conv_1",
                owner_user_id=owner_user_id,
                title="feedback race",
                pinned=False,
                effort_level="quick",
                scope_json={},
                last_active_at_utc=NOW,
                created_at_utc=NOW,
                updated_at_utc=NOW,
            )
        )
        connection.execute(
            chat_message_table.insert().values(
                id="user_msg_1",
                conversation_id="conv_1",
                owner_user_id=owner_user_id,
                role="user",
                content="question",
                created_at_utc=NOW,
            )
        )
        connection.execute(
            chat_message_table.insert().values(
                id="msg_1",
                conversation_id="conv_1",
                owner_user_id=owner_user_id,
                role="assistant",
                content="answer",
                status="completed",
                generation_id="gen_1",
                created_at_utc=NOW,
                updated_at_utc=NOW,
            )
        )


def test_concurrent_same_key_feedback_maps_unique_violation_to_409(pg_feedback_engine) -> None:
    engine = pg_feedback_engine
    clock = FixedClock(NOW)
    identity = IdentityAccessService(
        engine,
        make_settings().auth,
        now=clock.now_utc,
    )
    token, _user_id = provision_and_login(identity, "alice")
    principal = identity.authenticate_access_token(token)
    service = GenerationService(
        engine,
        clock=clock,
        authorization=build_runtime_authorization(identity),
        calibration=FakeCalibration(),
        sampler=lambda: 0.0,
    )

    _seed_completed_assistant_message(engine, owner_user_id=str(principal.user_id))

    winner = threading.Event()
    release = threading.Event()

    def hold_uncommitted_feedback_row() -> None:
        with engine.begin() as connection:
            connection.execute(
                chat_message_feedback_table.insert().values(
                    message_id="msg_1",
                    voter_user_id=str(principal.user_id),
                    vote="up",
                    down_reason=None,
                    created_at_utc=NOW,
                )
            )
            winner.set()
            assert release.wait(timeout=10)

    holder = threading.Thread(target=hold_uncommitted_feedback_row, name="feedback-holder")
    holder.start()
    assert winner.wait(timeout=10)

    outcome: dict[str, object] = {}

    def submit_concurrently() -> None:
        try:
            service.submit_feedback(
                principal=principal,
                message_id="msg_1",
                request=FeedbackRequest(vote="down", down_reason="no_grounding"),
                idempotency_key="race-key",
            )
        except PlatformError as error:
            outcome["code"] = error.code
            outcome["status_code"] = error.status_code
        except Exception as error:  # noqa: BLE001 - the race must never leak 500s
            outcome["unexpected"] = repr(error)

    racer = threading.Thread(target=submit_concurrently, name="feedback-racer")
    racer.start()
    time.sleep(0.3)
    assert racer.is_alive(), "the loser must block on the uncommitted same-key row"
    release.set()
    racer.join(timeout=10)
    holder.join(timeout=10)

    assert outcome == {"code": "feedback_already_submitted", "status_code": 409}
    with engine.connect() as connection:
        votes = (
            connection.execute(
                select(chat_message_feedback_table.c.vote).where(
                    chat_message_feedback_table.c.message_id == "msg_1"
                )
            )
            .scalars()
            .all()
        )
    assert votes == ["up"]
