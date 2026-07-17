import pytest

from app.config import Settings
from app.providers.selection import ProviderSelection, validate_provider_selection


def test_chat_model_is_the_only_chat_model_setting():
    settings = Settings(chat_model="deepseek-v4-pro", chat_provider="   ")

    assert settings.chat_model == "deepseek-v4-pro"
    assert settings.chat_provider is None
    assert "rag_model" not in Settings.model_fields
    assert "dashscope_model" not in Settings.model_fields
    assert "openai_compatible_model" not in Settings.model_fields
    assert "model" not in settings.rag.model_fields
    assert "model" not in settings.dashscope.model_fields
    assert "model" not in settings.openai_compatible.model_fields


@pytest.mark.parametrize(
    ("settings", "provider", "source"),
    [
        (
            Settings(
                _env_file=None,
                chat_provider="dashscope",
                dashscope_api_key="ds",
                deepseek_api_key="",
            ),
            "dashscope",
            "explicit",
        ),
        (
            Settings(_env_file=None, deepseek_api_key="ds", dashscope_api_key=""),
            "deepseek",
            "automatic",
        ),
        (
            Settings(_env_file=None, dashscope_api_key="qwen", deepseek_api_key=""),
            "dashscope",
            "automatic",
        ),
        (
            Settings(_env_file=None, deepseek_api_key="ds", dashscope_api_key="qwen"),
            "deepseek",
            "automatic",
        ),
        (
            Settings(_env_file=None, deepseek_api_key="", dashscope_api_key=""),
            "deepseek",
            "default_candidate",
        ),
    ],
)
def test_selects_chat_provider_predictably(settings, provider, source):
    selection = ProviderSelection.from_settings(settings)
    assert selection.chat_provider == provider
    assert selection.chat_provider_source == source


def test_unknown_explicit_chat_provider_is_rejected():
    errors = validate_provider_selection(
        Settings(
            _env_file=None,
            chat_provider="unknown",
            deepseek_api_key="ds",
            dashscope_api_key="",
        )
    )
    assert errors == [("CHAT_PROVIDER", "unsupported provider: unknown")]


@pytest.mark.parametrize("api_key", ["", "  ", "'your-api-key'", "placeholder", "changeme"])
def test_placeholder_key_is_not_eligible_for_automatic_selection(api_key):
    selection = ProviderSelection.from_settings(
        Settings(_env_file=None, deepseek_api_key=api_key, dashscope_api_key="")
    )
    assert selection.chat_provider == "deepseek"
    assert selection.chat_provider_source == "default_candidate"
