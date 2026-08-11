"""Shared test helpers for the outbox and notifications change."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Column, MetaData, String, Table, create_engine
from sqlalchemy.pool import StaticPool

from alembic.config import Config
from app.identity.revocation import NoopGenerationRevocationPort
from app.identity.schema import identity_metadata
from app.identity.service import IdentityAccessService
from app.outbox.schema import outbox_metadata
from app.platform.config import load_platform_settings
from app.platform.database import core_metadata
from app.usage.schema import usage_metadata

# The explicit development signing secret. The production runtime derives the
# capability secret from the configured auth secret key; tests sign tokens
# with the same explicit fallback secret the publisher/lifecycle use when
# constructed directly.
CAPABILITY_SECRET = b"ragqs-development-capability-secret"


def make_settings(**overrides) -> object:
    values = {
        "RAG_PLATFORM_PROFILE": "development",
        "RAG_DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "RAG_OBJECT_STORAGE_ENDPOINT": "http://localhost:9000",
        "RAG_OBJECT_STORAGE_BUCKET": "rag-dev",
        "RAG_PROVIDER_NAME": "fake",
        "RAG_AUTH_SECRET_KEY": "test-secret-that-is-long-enough",
    }
    values.update(overrides)
    return load_platform_settings(values)


def make_publisher(engine, **kwargs):
    """Construct a test publisher with the explicit test signing secret.

    The production constructor fails closed when no secret is configured; the
    shared test secret makes direct constructions deterministic. The publisher
    defaults to the deterministic test clock (`fixed_now`): every dispatcher
    in the suite claims with that clock, and a publisher stamping real wall
    time would make freshly published deliveries not-yet-due on hosts whose
    clock is ahead of `fixed_now` (as real-PostgreSQL runs exposed).
    """
    from app.outbox.publisher import SqlAlchemyOutboxPublisher

    kwargs.setdefault("capability_secret", CAPABILITY_SECRET)
    kwargs.setdefault("now", lambda: fixed_now())
    return SqlAlchemyOutboxPublisher(engine, **kwargs)


def build_engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    core_metadata.create_all(engine)
    identity_metadata.create_all(engine)
    outbox_metadata.create_all(engine)
    usage_metadata.create_all(engine)
    _LIVE_ENGINES.append(engine)
    return engine


# Every engine created by build_engine() is kept alive until the session-end
# disposal below, so the underlying sqlite3 connections are closed cleanly
# instead of being GC'd mid-run (which would emit ResourceWarning).
_LIVE_ENGINES: list = []


def dispose_all_test_engines() -> None:
    """Close every in-memory SQLite engine created by build_engine().

    Idempotent: engines already disposed (e.g. by runtime.close()) are safe
    to dispose again.
    """
    for engine in list(_LIVE_ENGINES):
        try:
            engine.dispose()
        except Exception:
            pass
    _LIVE_ENGINES.clear()


publish_domain_metadata = MetaData()
publish_business_table = Table(
    "publish_test_document",
    publish_domain_metadata,
    Column("id", String(64), primary_key=True),
    Column("status", String(32), nullable=False),
)


def create_publish_domain_tables(engine) -> None:
    publish_domain_metadata.create_all(engine)


def provision_user(
    service: IdentityAccessService,
    *,
    username: str,
    role: str = "user",
) -> str:
    user = service.provision_user(
        username=username,
        password="Password1",
        real_name=username.title(),
        display_name=username.title(),
        role=role,  # type: ignore[arg-type]
        department_id=None,
    )
    return str(user["id"])


def build_identity_service(engine) -> IdentityAccessService:
    configured = make_settings()
    return IdentityAccessService(
        engine,
        configured.auth,
        revocation_port=NoopGenerationRevocationPort(),
    )


def fixed_now() -> datetime:
    return datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)


def cap(principal: str, *event_types: str, secret: bytes = CAPABILITY_SECRET) -> str:
    """Build an assembly-time signed producer capability token for tests.

    The token is opaque and signed; a caller can never construct one without
    the secret. When no event types are given the full PRODUCER_MATRIX scope
    for the principal is signed. `secret` defaults to the explicit development
    secret; pass the runtime-derived secret when publishing through a
    runtime-constructed publisher.
    """
    from app.outbox.capabilities import sign_token
    from app.outbox.publisher import PRODUCER_MATRIX

    if event_types:
        types = list(event_types)
    else:
        types = [
            event_type
            for event_type, (callers, _aggregate) in PRODUCER_MATRIX.items()
            if principal in callers
        ]
    return sign_token(
        secret,
        kind="producer",
        principal=principal,
        scope={"event_types": types},
    )


def docs_redaction_token(*, deletion_id: str, transaction_id: str) -> str:
    """Documents service token for an exact deletion/transaction."""
    from app.outbox.capabilities import LifecycleCapabilityIssuer

    return LifecycleCapabilityIssuer(CAPABILITY_SECRET).issue_documents_redaction(
        deletion_id=deletion_id,
        transaction_id=transaction_id,
    )


def retention_redaction_token(*, deletion_id: str, transaction_id: str) -> str:
    """Retention-ops token delegating an exact documents-issued transaction."""
    from app.outbox.capabilities import LifecycleCapabilityIssuer

    return LifecycleCapabilityIssuer(CAPABILITY_SECRET).issue_retention_redaction(
        deletion_id=deletion_id,
        transaction_id=transaction_id,
    )


def retention_token() -> str:
    """Retention-ops token for retirement/compaction commands."""
    from app.outbox.capabilities import LifecycleCapabilityIssuer

    return LifecycleCapabilityIssuer(CAPABILITY_SECRET).issue_retention()


def alembic_config(database_url: str) -> Config:
    """Build an Alembic Config configured with the given SQLAlchemy URL.

    ConfigParser treats a bare ``%`` as an interpolation marker, so percent
    signs in the URL (e.g. the percent-encoded ``options=-c%20search_path%3D``
    query of a schema-scoped PostgreSQL URL) must be stored doubled (``%%``);
    reading the option back yields the original single-percent URL.
    """
    from alembic.config import Config

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def pg_schema_context():
    """Per-test PostgreSQL schema isolation.

    Creates a unique temporary schema, migrates it to alembic head and drops
    it in finally, so integration tests never share state through the base
    URL. Returns None when RAGQS_TEST_POSTGRES_URL is not configured.
    """
    import os
    import uuid
    from urllib.parse import quote

    from sqlalchemy import create_engine, text

    base_url = os.environ.get("RAGQS_TEST_POSTGRES_URL")
    if not base_url:
        return None
    schema = f"outbox_it_{uuid.uuid4().hex[:16]}"

    def scoped_url(url: str) -> str:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}options=-c%20search_path%3D{quote(schema)}"

    admin = create_engine(base_url)
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    finally:
        admin.dispose()

    from alembic import command

    try:
        command.upgrade(alembic_config(scoped_url(base_url)), "head")

        # `schema` is the enclosing function local; a class body cannot read it
        # from an attribute assignment of the same name (`schema = schema` would
        # shadow it with the not-yet-bound class-local and raise NameError).
        context_schema = schema

        class _Context:
            url = base_url
            schema = context_schema
            engine = create_engine(scoped_url(base_url))

            def close(self) -> None:
                self.engine.dispose()
                admin = create_engine(self.url)
                try:
                    with admin.begin() as connection:
                        connection.execute(text(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE'))
                finally:
                    admin.dispose()

        return _Context()
    except BaseException:
        # The migration or the scoped engine creation failed: the schema we
        # just created must not leak. The engine (when already created) is
        # never exposed to the caller, so no dispose is needed here.
        admin = create_engine(base_url)
        try:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            admin.dispose()
        raise


def pg_test_schema_names() -> set[str]:
    """Names of the temporary integration schemas in the acceptance database.

    Used by tests that verify schema cleanup on both success and failure
    paths. Returns an empty set when PostgreSQL is not configured.
    """
    import os

    from sqlalchemy import create_engine, text

    base_url = os.environ.get("RAGQS_TEST_POSTGRES_URL")
    if not base_url:
        return set()
    engine = create_engine(base_url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT nspname FROM pg_namespace "
                    "WHERE left(nspname, 10) = 'outbox_it_' "
                    "OR left(nspname, 4) = 'mig_'"
                )
            ).all()
            return {row[0] for row in rows}
    finally:
        engine.dispose()
