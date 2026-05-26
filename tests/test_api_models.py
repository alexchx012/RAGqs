import importlib
import warnings


def test_request_models_use_pydantic_v2_config_without_deprecation():
    import app.models.request as request_models

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        reloaded = importlib.reload(request_models)

    deprecations = [
        warning
        for warning in caught
        if "class-based `config` is deprecated" in str(warning.message)
    ]
    assert deprecations == []

    chat_request = reloaded.ChatRequest(Id="s1", Question="hello")
    clear_request = reloaded.ClearRequest(sessionId="s1")
    assert chat_request.id == "s1"
    assert chat_request.question == "hello"
    assert clear_request.session_id == "s1"


def test_response_envelope_helpers_preserve_public_code_message_data_shape():
    from app.models.response import ApiEnvelope, error_envelope, success_envelope

    success = success_envelope({"answer": "RAG"}, message="ok")
    error = error_envelope("failed", code=503, data={"retryable": True})

    assert isinstance(success, ApiEnvelope)
    assert success.model_dump() == {
        "code": 200,
        "message": "ok",
        "data": {"answer": "RAG"},
    }
    assert error.model_dump() == {
        "code": 503,
        "message": "failed",
        "data": {"retryable": True},
    }


def test_json_response_helper_sets_status_code_and_envelope_content():
    from app.models.response import envelope_json_response

    response = envelope_json_response({"healthy": True}, code=202, message="accepted")

    assert response.status_code == 202
    assert response.body == b'{"code":202,"message":"accepted","data":{"healthy":true}}'


def test_json_response_helper_can_preserve_legacy_envelope_without_message():
    from app.models.response import envelope_json_response

    response = envelope_json_response({"healthy": False}, code=503, include_message=False)

    assert response.status_code == 503
    assert response.body == b'{"code":503,"data":{"healthy":false}}'
