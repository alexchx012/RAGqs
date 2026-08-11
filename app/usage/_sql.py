"""SQL helper shims for the usage/quota domain (PostgreSQL + SQLite)."""

from __future__ import annotations

__all__ = ["_insert_do_nothing"]


def _insert_do_nothing(connection, table, values, index_elements) -> bool:
    """Execute ``INSERT ... ON CONFLICT DO NOTHING`` and return whether it inserted."""
    if connection.dialect.name == "postgresql":
        from sqlalchemy import literal
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        pg_statement = (
            pg_insert(table).values(**values).on_conflict_do_nothing(index_elements=index_elements)
        )
        # psycopg3 reports rowcount=-1 for INSERTs without RETURNING.  A returned
        # row is authoritative: the conflict path returns no row.
        return (
            connection.execute(pg_statement.returning(literal(1))).scalar_one_or_none() is not None
        )
    if connection.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        sqlite_statement = (
            sqlite_insert(table)
            .values(**values)
            .on_conflict_do_nothing(index_elements=index_elements)
        )
        return connection.execute(sqlite_statement).rowcount == 1
    return connection.execute(table.insert().values(**values)).rowcount == 1
