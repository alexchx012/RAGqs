"""Alembic environment for the Postgres business schema.

The migration environment deliberately has no dependency on application
models. The design documents are the schema authority until an ORM model
layer is introduced, so revisions use handwritten operations and keep
``target_metadata`` empty.
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, pool


config = context.config
target_metadata = None


def _database_url() -> str:
    """Return the configured SQLAlchemy URL without exposing it in config files."""

    url = (
        os.getenv("RAGQS_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or os.getenv("POSTGRES_DSN")
        or config.get_main_option("sqlalchemy.url")
    )
    if not url:
        raise RuntimeError(
            "Set RAGQS_DATABASE_URL (or DATABASE_URL/POSTGRES_DSN) before running Alembic."
        )
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
    """Run migrations against the configured Postgres database."""

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
