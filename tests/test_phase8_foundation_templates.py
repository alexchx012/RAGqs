from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.tools import tool

from app.operations.config_validation import validate_settings
from app.providers import (
    CheckpointProvider,
    FakeChatModelProvider,
    FakeEmbeddingProvider,
    FakeIngestionProvider,
    FakeVectorStoreProvider,
    InMemorySessionStoreProvider,
    PostgresSessionStoreProvider,
    SQLiteSessionStoreProvider,
)
from app.providers.checkpoints import SQLiteCheckpointProvider
from app.providers.factory import create_default_provider_container
from app.services.rag_agent_service import RagAgentService


def test_tool_registry_registers_builtin_and_business_tools():
    from app.extensions.tools import build_default_tool_registry

    @tool("crm_lookup")
    def crm_lookup(customer_id: str) -> str:
        """Look up a customer record."""
        return customer_id

    registry = build_default_tool_registry()
    registry.register(crm_lookup, category="business")

    assert registry.names() == ["retrieve_knowledge", "get_current_time", "crm_lookup"]
    assert [tool.name for tool in registry.build_tools(["crm_lookup"])] == ["crm_lookup"]
    assert registry.metadata("crm_lookup")["category"] == "business"


def test_tool_registry_rejects_duplicate_or_unknown_tools():
    from app.extensions.tools import ToolRegistry, UnknownToolError

    @tool("demo_tool")
    def demo_tool() -> str:
        """Return a demo value."""
        return "demo"

    registry = ToolRegistry()
    registry.register(demo_tool)

    with pytest.raises(ValueError, match="already registered"):
        registry.register(demo_tool)

    with pytest.raises(UnknownToolError, match="missing_tool"):
        registry.build_tools(["missing_tool"])


def test_prompt_profiles_are_named_and_extensible():
    from app.prompts.profiles import (
        PromptProfile,
        build_default_prompt_registry,
    )

    registry = build_default_prompt_registry()
    registry.register(
        PromptProfile(
            name="finance",
            description="Finance analyst RAG profile",
            system_prompt="Use only cited finance knowledge.",
        )
    )

    assert "default" in registry.names()
    assert "strict" in registry.names()
    assert registry.get("finance").system_prompt == "Use only cited finance knowledge."

    with pytest.raises(KeyError, match="missing"):
        registry.get("missing")


def test_rag_agent_service_uses_prompt_profile_and_enabled_tool_names():
    service = RagAgentService(
        streaming=False,
        chat_model_provider=FakeChatModelProvider(),
        prompt_profile="strict",
        enabled_tool_names=["get_current_time"],
    )

    assert [tool.name for tool in service.tools] == ["get_current_time"]
    assert "知识库中没有足够依据" in service.system_prompt


def test_provider_factory_can_switch_to_local_test_doubles_without_external_clients():
    settings = _settings(
        chat_provider="fake",
        embedding_provider="fake",
        vector_store_provider="fake",
        ingestion_provider="fake",
        session_store_provider="memory",
    )

    container = create_default_provider_container(settings=settings, milvus_manager=object())

    assert isinstance(container.chat_model_provider, FakeChatModelProvider)
    assert isinstance(container.embedding_provider, FakeEmbeddingProvider)
    assert isinstance(container.vector_store_provider, FakeVectorStoreProvider)
    assert isinstance(container.ingestion_provider, FakeIngestionProvider)
    assert isinstance(container.session_store_provider, InMemorySessionStoreProvider)
    assert isinstance(container.checkpoint_provider, CheckpointProvider)


def test_provider_factory_can_select_sqlite_session_store(tmp_path):
    settings = _settings(
        chat_provider="fake",
        embedding_provider="fake",
        vector_store_provider="fake",
        ingestion_provider="fake",
        session_store_provider="sqlite",
        session_store_sqlite_path=str(tmp_path / "sessions.db"),
    )

    container = create_default_provider_container(settings=settings, milvus_manager=object())

    assert isinstance(container.session_store_provider, SQLiteSessionStoreProvider)
    container.session_store_provider.append_message("s1", "user", "persist this")
    assert container.session_store_provider.get_messages("s1")[0].content == "persist this"
    container.session_store_provider.close()


def test_provider_factory_can_select_postgres_session_store_without_connecting():
    settings = _settings(
        chat_provider="fake",
        embedding_provider="fake",
        vector_store_provider="fake",
        ingestion_provider="fake",
        session_store_provider="postgres",
        session_store_postgres_dsn="postgresql://rag:secret@db/ragqs",
    )

    container = create_default_provider_container(settings=settings, milvus_manager=object())

    assert isinstance(container.session_store_provider, PostgresSessionStoreProvider)
    assert container.session_store_provider.dsn == "postgresql://rag:secret@db/ragqs"


def test_provider_factory_can_select_sqlite_checkpoint_provider(tmp_path):
    settings = _settings(
        chat_provider="fake",
        embedding_provider="fake",
        vector_store_provider="fake",
        ingestion_provider="fake",
        checkpoint_provider="sqlite",
        checkpoint_sqlite_path=str(tmp_path / "checkpoints.db"),
    )

    container = create_default_provider_container(settings=settings, milvus_manager=object())

    assert isinstance(container.checkpoint_provider, SQLiteCheckpointProvider)
    checkpointer = container.checkpoint_provider.create_checkpointer()
    assert hasattr(checkpointer, "get")
    assert hasattr(checkpointer, "put")
    container.checkpoint_provider.close()


def test_provider_factory_rejects_unknown_provider_ids():
    settings = _settings(chat_provider="unknown")

    with pytest.raises(ValueError, match="CHAT_PROVIDER"):
        create_default_provider_container(settings=settings, milvus_manager=object())


def test_config_validation_checks_phase8_extension_settings():
    report = validate_settings(
        _settings(
            chat_provider="openai_compatible",
            embedding_provider="openai_compatible",
            openai_compatible_api_key="",
            openai_compatible_model="",
            prompt_profile="missing",
            enabled_tools="retrieve_knowledge,missing_tool",
        )
    )

    assert report.is_valid is False
    issues = {(issue.field, issue.message) for issue in report.errors}
    assert ("OPENAI_COMPATIBLE_API_KEY", "must be set when an OpenAI-compatible provider is selected") in issues
    assert ("OPENAI_COMPATIBLE_MODEL", "must be set when an OpenAI-compatible provider is selected") in issues
    assert ("PROMPT_PROFILE", "unsupported prompt profile: missing") in issues
    assert ("ENABLED_TOOLS", "unsupported tool: missing_tool") in issues


def test_config_validation_requires_sqlite_session_store_path():
    report = validate_settings(
        _settings(session_store_provider="sqlite", session_store_sqlite_path=" ")
    )

    assert report.is_valid is False
    issues = {(issue.field, issue.message) for issue in report.errors}
    assert ("SESSION_STORE_SQLITE_PATH", "must be set when SESSION_STORE_PROVIDER=sqlite") in issues


def test_config_validation_requires_postgres_session_store_dsn():
    report = validate_settings(
        _settings(session_store_provider="postgres", session_store_postgres_dsn=" ")
    )

    assert report.is_valid is False
    issues = {(issue.field, issue.message) for issue in report.errors}
    assert (
        "SESSION_STORE_POSTGRES_DSN",
        "must be set when SESSION_STORE_PROVIDER=postgres",
    ) in issues


def test_config_validation_requires_sqlite_indexing_job_store_path():
    report = validate_settings(
        _settings(indexing_job_store_provider="sqlite", indexing_job_store_sqlite_path=" ")
    )

    assert report.is_valid is False
    issues = {(issue.field, issue.message) for issue in report.errors}
    assert (
        "INDEXING_JOB_STORE_SQLITE_PATH",
        "must be set when INDEXING_JOB_STORE_PROVIDER=sqlite",
    ) in issues


def test_config_validation_requires_postgres_indexing_job_store_dsn():
    report = validate_settings(
        _settings(
            indexing_job_store_provider="postgres",
            indexing_job_store_postgres_dsn=" ",
        )
    )

    assert report.is_valid is False
    issues = {(issue.field, issue.message) for issue in report.errors}
    assert (
        "INDEXING_JOB_STORE_POSTGRES_DSN",
        "must be set when INDEXING_JOB_STORE_PROVIDER=postgres",
    ) in issues


def test_config_validation_requires_sqlite_document_catalog_path():
    report = validate_settings(
        _settings(document_catalog_provider="sqlite", document_catalog_sqlite_path=" ")
    )

    assert report.is_valid is False
    issues = {(issue.field, issue.message) for issue in report.errors}
    assert (
        "DOCUMENT_CATALOG_SQLITE_PATH",
        "must be set when DOCUMENT_CATALOG_PROVIDER=sqlite",
    ) in issues


def test_config_validation_requires_postgres_document_catalog_dsn():
    report = validate_settings(
        _settings(
            document_catalog_provider="postgres",
            document_catalog_postgres_dsn=" ",
        )
    )

    assert report.is_valid is False
    issues = {(issue.field, issue.message) for issue in report.errors}
    assert (
        "DOCUMENT_CATALOG_POSTGRES_DSN",
        "must be set when DOCUMENT_CATALOG_PROVIDER=postgres",
    ) in issues


def test_config_validation_requires_sqlite_checkpoint_path():
    report = validate_settings(
        _settings(checkpoint_provider="sqlite", checkpoint_sqlite_path=" ")
    )

    assert report.is_valid is False
    issues = {(issue.field, issue.message) for issue in report.errors}
    assert (
        "CHECKPOINT_SQLITE_PATH",
        "must be set when CHECKPOINT_PROVIDER=sqlite",
    ) in issues


def test_config_validation_requires_postgres_checkpoint_dsn():
    report = validate_settings(
        _settings(checkpoint_provider="postgres", checkpoint_postgres_dsn=" ")
    )

    assert report.is_valid is False
    issues = {(issue.field, issue.message) for issue in report.errors}
    assert (
        "CHECKPOINT_POSTGRES_DSN",
        "must be set when CHECKPOINT_PROVIDER=postgres",
    ) in issues


def test_config_validation_rejects_unknown_checkpoint_provider():
    report = validate_settings(_settings(checkpoint_provider="unknown"))

    assert report.is_valid is False
    issues = {(issue.field, issue.message) for issue in report.errors}
    assert ("CHECKPOINT_PROVIDER", "unsupported provider: unknown") in issues


def test_config_validation_rejects_unknown_agent_runtime():
    report = validate_settings(_settings(agent_runtime="unknown"))

    assert report.is_valid is False
    issues = {(issue.field, issue.message) for issue in report.errors}
    assert ("AGENT_RUNTIME", "unsupported runtime: unknown") in issues


def test_config_validation_rejects_unknown_retrieval_enhancer_settings():
    report = validate_settings(
        _settings(
            retrieval_profile="unknown",
            retrieval_high_recall_top_k_multiplier=0,
            retrieval_relaxed_filter_preserve_keys=" ",
            query_rewriter_provider="unknown",
            reranker_provider="unknown",
            context_compressor_provider="unknown",
            context_compressor_max_characters=0,
        )
    )

    assert report.is_valid is False
    issues = {(issue.field, issue.message) for issue in report.errors}
    assert ("RETRIEVAL_PROFILE", "unsupported profile: unknown") in issues
    assert (
        "RETRIEVAL_HIGH_RECALL_TOP_K_MULTIPLIER",
        "must be greater than or equal to 1",
    ) in issues
    assert (
        "RETRIEVAL_RELAXED_FILTER_PRESERVE_KEYS",
        "must contain at least one filter key",
    ) in issues
    assert ("QUERY_REWRITER_PROVIDER", "unsupported provider: unknown") in issues
    assert ("RERANKER_PROVIDER", "unsupported provider: unknown") in issues
    assert ("CONTEXT_COMPRESSOR_PROVIDER", "unsupported provider: unknown") in issues
    assert (
        "CONTEXT_COMPRESSOR_MAX_CHARACTERS",
        "must be greater than or equal to 1",
    ) in issues


def test_phase8_docs_cover_extension_and_second_business_templates():
    extension_docs = (ROOT / "docs" / "extension-guide.md").read_text(encoding="utf-8")
    template_docs = (ROOT / "docs" / "templates" / "business-rag-template.md").read_text(
        encoding="utf-8"
    )

    for phrase in [
        "Tool Registry",
        "Tool Planning",
        "Provider Switching",
        "Prompt Profiles",
        "Retrieval Enhancers",
        "Second-Business Template",
    ]:
        assert phrase in extension_docs

    for phrase in [
        "ENABLED_TOOLS",
        "TOOL_PLANNING_ENABLED",
        "PROMPT_PROFILE",
        "CHAT_PROVIDER",
        "CHECKPOINT_PROVIDER",
        "RETRIEVAL_PROFILE",
        "QUERY_REWRITER_PROVIDER",
        "RERANKER_PROVIDER",
        "CONTEXT_COMPRESSOR_PROVIDER",
        "do not modify core code",
    ]:
        assert phrase in template_docs


def _settings(**overrides):
    values = {
        "dashscope_api_key": "sk-valid",
        "rag_model": "qwen-max",
        "rag_top_k": 3,
        "milvus_host": "127.0.0.1",
        "milvus_port": 19530,
        "chat_provider": "dashscope",
        "embedding_provider": "dashscope",
        "vector_store_provider": "milvus",
        "session_store_provider": "memory",
        "session_store_sqlite_path": "data/sessions.sqlite3",
        "session_store_postgres_dsn": "",
        "indexing_job_store_provider": "memory",
        "indexing_job_store_sqlite_path": "data/indexing-jobs.sqlite3",
        "indexing_job_store_postgres_dsn": "",
        "document_catalog_provider": "memory",
        "document_catalog_sqlite_path": "data/document-catalog.sqlite3",
        "document_catalog_postgres_dsn": "",
        "checkpoint_provider": "memory",
        "checkpoint_sqlite_path": "data/checkpoints.sqlite3",
        "checkpoint_postgres_dsn": "",
        "agent_runtime": "explicit_graph",
        "ingestion_provider": "vector_index",
        "openai_compatible_api_key": "sk-compatible",
        "openai_compatible_base_url": "https://api.example.com/v1",
        "openai_compatible_model": "compatible-chat",
        "prompt_profile": "default",
        "retrieval_profile": "default",
        "retrieval_high_recall_top_k_multiplier": 2,
        "retrieval_relaxed_filter_preserve_keys": "space_id,spaceId,tenant_id,tenantId",
        "query_rewriter_provider": "none",
        "reranker_provider": "none",
        "context_compressor_provider": "none",
        "context_compressor_max_characters": 1200,
        "enabled_tools": "retrieve_knowledge,get_current_time",
        "cors_allow_origins": "http://127.0.0.1:9900",
        "cors_allow_credentials": True,
        "chunk_max_size": 800,
        "chunk_overlap": 100,
        "host": "0.0.0.0",
        "port": 9900,
        "milvus_timeout": 10000,
        "dashscope_embedding_model": "text-embedding-v4",
        "openai_compatible_embedding_model": "compatible-embedding",
    }
    values.update(overrides)
    return SimpleNamespace(**values)
ROOT = Path(__file__).resolve().parents[1]
