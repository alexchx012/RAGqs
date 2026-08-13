"""Shadow-run API contract (A3/A4/A5/A6)."""

from __future__ import annotations

from sqlalchemy import select

from app.evaluation.schema import shadow_evaluation_result_table

from .conftest import build_test_env, provision_and_login


def _ops(env) -> tuple[str, str, str]:
    return provision_and_login(env["identity"], "ops1", role="ops")


def _admin(env) -> tuple[str, str, str]:
    return provision_and_login(env["identity"], "admin1", role="admin")


def _user(env) -> tuple[str, str, str]:
    return provision_and_login(env["identity"], "user1", role="user")


def _post_run(env, token: str, *, space_id: str, key: str):
    return env["client"].post(
        "/v1/admin/evaluations/shadow-runs",
        json={"space_id": space_id},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
    )


def test_forbidden_without_evaluation_run_capability() -> None:
    env = build_test_env()
    token, _, user_id = _user(env)
    response = _post_run(env, token, space_id=f"personal:{user_id}", key="k1")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "evaluation_run_forbidden"


def test_create_enqueues_and_does_not_run_handler() -> None:
    env = build_test_env()
    token, _, user_id = _ops(env)
    response = _post_run(env, token, space_id=f"personal:{user_id}", key="k1")
    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"run_id", "status"}
    assert body["status"] == "queued"
    # A12: handler only enqueues, never executes judge or writes results.
    assert env["judge"].calls == []
    with env["engine"].connect() as connection:
        assert connection.execute(select(shadow_evaluation_result_table)).all() == []


def test_idempotent_replay_and_conflict() -> None:
    env = build_test_env()
    token, _, user_id = _ops(env)
    first = _post_run(env, token, space_id=f"personal:{user_id}", key="k1")
    assert first.status_code == 202
    replay = _post_run(env, token, space_id=f"personal:{user_id}", key="k1")
    assert replay.status_code == 202
    assert replay.json() == first.json()
    # Same key, different request → 409.
    conflict = _post_run(env, token, space_id="personal:other", key="k1")
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_key_conflict"


def test_in_progress_space_returns_conflict() -> None:
    env = build_test_env()
    token, _, user_id = _ops(env)
    assert _post_run(env, token, space_id=f"personal:{user_id}", key="k1").status_code == 202
    second = _post_run(env, token, space_id=f"personal:{user_id}", key="k2")
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "shadow_evaluation_in_progress"


def test_space_unavailable_returns_404() -> None:
    env = build_test_env()
    token, _, _ = _ops(env)
    response = _post_run(env, token, space_id="personal:missing", key="k1")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "evaluation_space_unavailable"


def test_judge_unavailable_returns_503() -> None:
    from .conftest import FakeJudgeProvider

    env = build_test_env(judge=FakeJudgeProvider(fail_preflight=True))
    token, _, user_id = _ops(env)
    response = _post_run(env, token, space_id=f"personal:{user_id}", key="k1")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "evaluation_judge_unavailable"


def test_read_model_fields_and_errors() -> None:
    env = build_test_env()
    token, _, user_id = _ops(env)
    created = _post_run(env, token, space_id=f"personal:{user_id}", key="k1").json()
    run_id = created["run_id"]
    response = env["client"].get(
        f"/v1/admin/evaluations/shadow-runs/{run_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == run_id
    assert body["state"] == "queued"
    assert "progress" in body and "report_ref" in body
    assert "question_text" not in str(body)

    missing = env["client"].get(
        "/v1/admin/evaluations/shadow-runs/run_missing",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "shadow_evaluation_not_found"

    user_token, _, _ = _user(env)
    forbidden = env["client"].get(
        f"/v1/admin/evaluations/shadow-runs/{run_id}",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "evaluation_run_forbidden"


def test_admin_can_read_run() -> None:
    env = build_test_env()
    token, _, user_id = _ops(env)
    created = _post_run(env, token, space_id=f"personal:{user_id}", key="k1").json()
    admin_token, _, _ = _admin(env)
    response = env["client"].get(
        f"/v1/admin/evaluations/shadow-runs/{created['run_id']}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.status_code == 200
