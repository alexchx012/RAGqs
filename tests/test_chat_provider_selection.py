from app.config import Settings


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
