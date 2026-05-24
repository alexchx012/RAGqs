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
