from types import SimpleNamespace

import app.providers.checkpoints as checkpoints
from app.providers import CheckpointProvider
from app.providers.factory import create_default_provider_container


def test_postgres_checkpoint_provider_defers_connection_until_create_checkpointer():
    factory = RecordingPostgresSaverFactory()
    provider_class = _postgres_checkpoint_provider_class()
    provider = provider_class(
        "postgresql://rag:secret@db/ragqs",
        saver_factory=factory,
    )

    assert isinstance(provider, CheckpointProvider)
    assert provider.dsn == "postgresql://rag:secret@db/ragqs"
    assert factory.calls == []

    checkpointer = provider.create_checkpointer()

    assert factory.calls == ["postgresql://rag:secret@db/ragqs"]
    assert checkpointer.setup_count == 1
    assert provider.create_checkpointer() is checkpointer

    provider.close()

    assert factory.contexts[0].exit_count == 1


def test_provider_factory_can_select_postgres_checkpoint_provider_without_connecting():
    settings = SimpleNamespace(
        chat_provider="fake",
        chat_model="test-chat-model",
        deepseek_api_key="",
        dashscope_api_key="",
        dashscope_embedding_model="text-embedding-v4",
        rag_top_k=3,
        milvus_host="127.0.0.1",
        milvus_port=19530,
        embedding_provider="fake",
        vector_store_provider="fake",
        ingestion_provider="fake",
        session_store_provider="memory",
        checkpoint_provider="postgres",
        checkpoint_postgres_dsn="postgresql://rag:secret@db/ragqs",
    )
    assert not hasattr(settings, "rag_model")

    container = create_default_provider_container(settings=settings, milvus_manager=object())

    assert isinstance(container.checkpoint_provider, _postgres_checkpoint_provider_class())
    assert container.checkpoint_provider.dsn == "postgresql://rag:secret@db/ragqs"


def _postgres_checkpoint_provider_class():
    assert hasattr(checkpoints, "PostgresCheckpointProvider")
    return checkpoints.PostgresCheckpointProvider


class RecordingPostgresSaverFactory:
    def __init__(self):
        self.calls = []
        self.contexts = []

    def __call__(self, dsn: str):
        self.calls.append(dsn)
        context = RecordingPostgresSaverContext()
        self.contexts.append(context)
        return context


class RecordingPostgresSaverContext:
    def __init__(self):
        self.checkpointer = RecordingCheckpointer()
        self.exit_count = 0

    def __enter__(self):
        return self.checkpointer

    def __exit__(self, exc_type, exc, traceback):
        self.exit_count += 1
        return False


class RecordingCheckpointer:
    def __init__(self):
        self.setup_count = 0

    def setup(self):
        self.setup_count += 1
