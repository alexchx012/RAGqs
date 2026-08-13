"""Alembic environment for the core platform schema."""

from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import create_engine, pool

from alembic import context
from app.chat.schema import chat_metadata
from app.documents.schema import documents_metadata
from app.identity.schema import identity_metadata
from app.indexing.schema import indexing_metadata
from app.outbox.schema import outbox_metadata
from app.platform.database import core_metadata
from app.usage.schema import usage_metadata

config = context.config
if config.config_file_name is not None:
    # disable_existing_loggers=False：迁移不应把应用里已创建的 logger
    # （如 app.outbox.dispatcher）置为 disabled，否则会破坏调用方对日志
    # 的捕获（caplog 等）并让依赖日志输出的行为静默丢失。
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = [
    core_metadata,
    identity_metadata,
    outbox_metadata,
    usage_metadata,
    documents_metadata,
    indexing_metadata,
    chat_metadata,
]


def _database_url() -> str:
    """Return the configured SQLAlchemy URL without exposing credentials."""

    url = config.get_main_option("sqlalchemy.url") or os.getenv("RAG_DATABASE_URL")
    if not url:
        raise RuntimeError("Set RAG_DATABASE_URL before running Alembic.")
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def run_migrations_offline() -> None:
    """Generate SQL without opening a database connection."""

    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against the configured database."""

    engine = create_engine(
        _database_url(),
        poolclass=pool.NullPool,
        future=True,
    )
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                compare_server_default=True,
                transaction_per_migration=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
