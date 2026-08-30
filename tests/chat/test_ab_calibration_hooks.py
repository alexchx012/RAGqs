"""A/B calibration closure hooks: same-source skip and vote-time ports (A3/A8/A10)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.chat.models import RetrievalHitOutcome, RetrievalOutcome
from app.chat.schema import (
    chat_ab_candidate_table,
    chat_ab_pair_table,
    chat_generation_event_table,
    chat_message_table,
)

from .conftest import (
    FakeCalibration,
    build_test_env,
    open_window,
    provision_and_login,
)


class FakeAbSourceFilter:
    """Test double for the same-source pre-filter."""

    def __init__(self, *, identical: bool) -> None:
        self.identical = identical
        self.calls: list[dict[str, Any]] = []

    def candidate_sources_identical(
        self,
        query: str,
        *,
        principal: Any,
        narrowing_scope: Any,
        candidate_profiles: tuple[tuple[str, str], tuple[str, str]],
        effort: str,
    ) -> bool:
        self.calls.append(
            {
                "query": query,
                "user_id": getattr(principal, "user_id", principal),
                "candidate_profiles": candidate_profiles,
                "effort": effort,
            }
        )
        return self.identical


def _hit(snippet: str = "the answer is 42") -> RetrievalHitOutcome:
    return RetrievalHitOutcome(
        document_id="doc_1",
        document_version_id="ver_1",
        publication_id="pub_1",
        chunk_id="chunk_1",
        space_id="space_1",
        locator={"page": 1},
        snippet=snippet,
    )


def _ask_and_complete(
    env: dict,
    token: str,
    *,
    content: str,
    scope: dict | None = None,
) -> str:
    """Run one ask through creation and the worker; return the conversation id."""
    from app.chat.models import AskRequest, ConversationScope

    conversation_id = (
        env["client"]
        .post(
            "/v1/conversations",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
        .json()["id"]
    )
    service = env["runtime"].resolve("chat_generation_service")
    principal = env["identity"].authenticate_access_token(token)
    service.ask(
        principal=principal,
        conversation_id=conversation_id,
        request=AskRequest(
            content=content,
            effort_level="quick",
            scope=ConversationScope.from_value(scope),
        ),
        idempotency_key=f"ask-{content}",
    )
    env["runtime"].resolve("chat_generation_worker").run_once()
    return conversation_id


def _assistant_message(env: dict, token: str, conversation_id: str) -> dict:
    detail = (
        env["client"]
        .get(
            f"/v1/conversations/{conversation_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        .json()
    )
    return detail["messages"][1]


def test_same_source_skip_creates_no_pair_and_answers_normally() -> None:
    """A3/A10/A11: identical candidate hit sets skip the pair entirely."""
    env = build_test_env(
        calibration=FakeCalibration(window=open_window()),
        outcomes={"hello": RetrievalOutcome(hits=(_hit(),))},
        ab_source_filter=FakeAbSourceFilter(identical=True),
    )
    token, user_id = provision_and_login(env["identity"], "alice")
    space_id = f"personal:{user_id}"
    conversation_id = _ask_and_complete(
        env, token, content="hello", scope={"space_ids": [space_id]}
    )

    with env["engine"].connect() as connection:
        pairs = connection.execute(select(chat_ab_pair_table)).all()
        event_types = (
            connection.execute(select(chat_generation_event_table.c.event_type)).scalars().all()
        )
        message = connection.execute(
            select(chat_message_table.c.content, chat_message_table.c.notices_json).where(
                chat_message_table.c.role == "assistant"
            )
        ).one()
    # No pair, no ab_start, no notice; the question is answered normally (candidate=0).
    assert pairs == []
    assert "ab_start" not in event_types
    assert "notice" not in event_types
    assert message.notices_json is None
    assert "answer for hello" in message.content
    # The filter ran once at sampling time with both candidate configs' profiles.
    # The pair's config order is randomized per ask (A3), so compare as a set.
    source_filter: FakeAbSourceFilter = env["ab_source_filter"]
    assert len(source_filter.calls) == 1
    call = source_filter.calls[0]
    assert call["query"] == "hello"
    assert call["effort"] == "quick"
    assert set(call["candidate_profiles"]) == {
        ("default", "default"),
        ("default", "candidate_b"),
    }
    # Window quota is untouched: no pair exists to vote on.
    assert env["calibration"].collected == []
    assert _assistant_message(env, token, conversation_id)["ab"] is None


def test_differing_sources_keep_existing_ab_flow() -> None:
    env = build_test_env(
        calibration=FakeCalibration(window=open_window()),
        outcomes={"hello": RetrievalOutcome(hits=(_hit(),))},
        ab_source_filter=FakeAbSourceFilter(identical=False),
    )
    env["provider"].candidate_bias = True
    token, user_id = provision_and_login(env["identity"], "alice")
    conversation_id = _ask_and_complete(
        env, token, content="hello", scope={"space_ids": [f"personal:{user_id}"]}
    )

    with env["engine"].connect() as connection:
        pair = connection.execute(select(chat_ab_pair_table)).mappings().one()
        event_types = (
            connection.execute(select(chat_generation_event_table.c.event_type)).scalars().all()
        )
    assert str(pair["status"]) == "open"
    assert "ab_start" in event_types
    assert _assistant_message(env, token, conversation_id)["ab"]["status"] == "open"


def test_ab_pair_persists_randomized_config_mapping_and_retrieves_each_profile() -> None:
    env = build_test_env(
        calibration=FakeCalibration(window=open_window()),
        outcomes={"hello": RetrievalOutcome(hits=(_hit("profile-specific"),))},
        ab_source_filter=FakeAbSourceFilter(identical=False),
        candidate_config_versions=("cfg_a", "cfg_b", "cfg_unused"),
        # Reverse the deployment order so the persisted mapping is observable.
        ab_randomizer=lambda: 0.9,
    )
    token, _ = provision_and_login(env["identity"], "alice")
    conversation_id = _ask_and_complete(env, token, content="hello")

    with env["engine"].connect() as connection:
        pair_id = str(connection.execute(select(chat_ab_pair_table.c.pair_id)).scalar_one())
        mapping = (
            connection.execute(
                select(
                    chat_ab_candidate_table.c.candidate,
                    chat_ab_candidate_table.c.candidate_config_version,
                ).where(chat_ab_candidate_table.c.pair_id == pair_id)
            )
            .mappings()
            .all()
        )
    assert [(int(row["candidate"]), row["candidate_config_version"]) for row in mapping] == [
        (0, "cfg_b"),
        (1, "cfg_a"),
    ]

    retrieval = env["retrieval"]
    assert [call["profile_version"] for call in retrieval.searches] == ["cfg_b", "cfg_a"]
    assert [call.candidate for call in env["provider"].calls] == [0, 1]
    assert "cfg_a" not in str(_assistant_message(env, token, conversation_id))
    assert "cfg_b" not in str(_assistant_message(env, token, conversation_id))


def test_ab_mapping_survives_worker_retry_without_reassignment() -> None:
    env = build_test_env(
        calibration=FakeCalibration(window=open_window()),
        outcomes={"hello": RetrievalOutcome(hits=(_hit(),))},
        candidate_config_versions=("cfg_a", "cfg_b"),
        ab_randomizer=lambda: 0.1,
    )
    env["provider"].fail_next = True
    token, _ = provision_and_login(env["identity"], "alice")
    conversation_id = (
        env["client"]
        .post(
            "/v1/conversations",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
        .json()["id"]
    )
    from app.chat.models import AskRequest

    principal = env["identity"].authenticate_access_token(token)
    env["runtime"].resolve("chat_generation_service").ask(
        principal=principal,
        conversation_id=conversation_id,
        request=AskRequest(content="hello", effort_level="quick", scope=None),
        idempotency_key="retry-mapping",
    )
    with env["engine"].connect() as connection:
        before = (
            connection.execute(
                select(
                    chat_ab_candidate_table.c.candidate,
                    chat_ab_candidate_table.c.candidate_config_version,
                )
            )
            .mappings()
            .all()
        )
    env["runtime"].resolve("chat_generation_worker").run_once()
    with env["engine"].connect() as connection:
        after = (
            connection.execute(
                select(
                    chat_ab_candidate_table.c.candidate,
                    chat_ab_candidate_table.c.candidate_config_version,
                )
            )
            .mappings()
            .all()
        )
    assert [(row["candidate"], row["candidate_config_version"]) for row in after] == [
        (row["candidate"], row["candidate_config_version"]) for row in before
    ]


def test_no_filter_keeps_sampling_behavior() -> None:
    env = build_test_env(
        calibration=FakeCalibration(window=open_window()),
        outcomes={"hello": RetrievalOutcome(hits=(_hit(),))},
    )
    env["provider"].candidate_bias = True
    token, _ = provision_and_login(env["identity"], "alice")
    _ask_and_complete(env, token, content="hello")

    with env["engine"].connect() as connection:
        assert connection.execute(select(chat_ab_pair_table)).mappings().one() is not None


def test_effective_vote_seeds_golden_pool_and_triggers_adoption_gate() -> None:
    """A8: a counted vote (choice 0/1) records the seed and reruns the gate."""
    env = build_test_env(
        calibration=FakeCalibration(window=open_window()),
        outcomes={"hello": RetrievalOutcome(hits=(_hit(),))},
    )
    env["provider"].candidate_bias = True
    token, _ = provision_and_login(env["identity"], "alice")
    user_id = env["identity"].authenticate_access_token(token).user_id
    space_id = f"personal:{user_id}"
    conversation_id = _ask_and_complete(
        env, token, content="hello", scope={"space_ids": [space_id]}
    )
    assistant = _assistant_message(env, token, conversation_id)
    assert assistant["ab"]["status"] == "open"
    pair_id = assistant["ab"]["pair_id"]

    vote = env["client"].post(
        f"/v1/messages/{assistant['id']}/ab-vote",
        json={"pair_id": pair_id, "choice": "0"},
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "vote-seed-1",
        },
    )
    assert vote.status_code == 200

    seeds = env["calibration"].golden_seeds
    assert len(seeds) == 1
    seed = seeds[0]
    assert seed["pair_id"] == pair_id
    assert seed["space_id"] == space_id
    assert seed["question_text"] == "hello"
    assert seed["preferred_candidate"] == 0
    assert "answer for hello" in seed["preferred_content"]
    assert seed["rejected_candidate"] == 1
    assert seed["policy_version"] == "cal-v1"
    assert env["calibration"].adoptions == [{"space_id": space_id, "now": seed["now"]}]


def test_neither_vote_records_no_seed_and_no_adoption() -> None:
    env = build_test_env(
        calibration=FakeCalibration(window=open_window()),
        outcomes={"hello": RetrievalOutcome(hits=(_hit(),))},
    )
    env["provider"].candidate_bias = True
    token, _ = provision_and_login(env["identity"], "alice")
    user_id = env["identity"].authenticate_access_token(token).user_id
    conversation_id = _ask_and_complete(
        env, token, content="hello", scope={"space_ids": [f"personal:{user_id}"]}
    )
    assistant = _assistant_message(env, token, conversation_id)
    pair_id = assistant["ab"]["pair_id"]

    vote = env["client"].post(
        f"/v1/messages/{assistant['id']}/ab-vote",
        json={"pair_id": pair_id, "choice": "neither"},
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "vote-neither-1",
        },
    )
    assert vote.status_code == 200
    assert env["calibration"].golden_seeds == []
    assert env["calibration"].adoptions == []
