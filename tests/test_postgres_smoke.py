from io import StringIO
from types import SimpleNamespace


def test_postgres_smoke_skips_when_no_postgres_stores_are_configured():
    from app.operations.postgres_smoke import run_postgres_smoke

    report = run_postgres_smoke(
        settings=_settings(),
        postgres_probe=lambda dsn, timeout_seconds: {"ok": True},
    )

    assert report.ready is True
    assert [(check.name, check.status) for check in report.checks] == [
        ("postgres", "skipped")
    ]
    assert report.errors == []


def test_postgres_smoke_can_require_at_least_one_postgres_store():
    from app.operations.postgres_smoke import run_postgres_smoke

    report = run_postgres_smoke(
        settings=_settings(),
        postgres_probe=lambda dsn, timeout_seconds: {"ok": True},
        require_configured=True,
    )

    assert report.ready is False
    assert [(issue.field, issue.message) for issue in report.errors] == [
        ("POSTGRES", "no postgres-backed stores configured")
    ]


def test_postgres_smoke_checks_each_configured_postgres_store_without_leaking_passwords():
    from app.operations.postgres_smoke import run_postgres_smoke

    calls = []

    def probe(dsn: str, timeout_seconds: float):
        calls.append((dsn, timeout_seconds))
        return {"serverReachable": True}

    report = run_postgres_smoke(
        settings=_settings(
            session_store_provider="postgres",
            session_store_postgres_dsn="postgresql://rag:secret@db/ragqs",
            retrieval_audit_store_provider="postgres",
            retrieval_audit_postgres_dsn="postgresql://rag:secret@db/ragqs-audits",
            indexing_job_store_provider="postgres",
            indexing_job_store_postgres_dsn="postgresql://rag:secret@db/ragqs-indexing",
            document_catalog_provider="postgres",
            document_catalog_postgres_dsn="postgresql://rag:secret@db/ragqs-documents",
            checkpoint_provider="postgres",
            checkpoint_postgres_dsn="postgresql://rag:secret@db/ragqs-checkpoints",
        ),
        postgres_probe=probe,
        timeout_seconds=2.5,
    )

    assert report.ready is True
    assert calls == [
        ("postgresql://rag:secret@db/ragqs", 2.5),
        ("postgresql://rag:secret@db/ragqs-audits", 2.5),
        ("postgresql://rag:secret@db/ragqs-indexing", 2.5),
        ("postgresql://rag:secret@db/ragqs-documents", 2.5),
        ("postgresql://rag:secret@db/ragqs-checkpoints", 2.5),
    ]
    assert [(check.name, check.status) for check in report.checks] == [
        ("sessionStore", "healthy"),
        ("retrievalAuditStore", "healthy"),
        ("indexingJobStore", "healthy"),
        ("documentCatalog", "healthy"),
        ("checkpointStore", "healthy"),
    ]
    assert all(check.details["dsn"] == "postgresql://rag:***@db/..." for check in report.checks)
    assert "secret" not in report.model_dump_json()


def test_postgres_smoke_reports_missing_dsn_for_selected_postgres_store():
    from app.operations.postgres_smoke import run_postgres_smoke

    report = run_postgres_smoke(
        settings=_settings(session_store_provider="postgres", session_store_postgres_dsn=" "),
        postgres_probe=lambda dsn, timeout_seconds: {"ok": True},
    )

    assert report.ready is False
    assert [(issue.field, issue.message) for issue in report.errors] == [
        ("SESSION_STORE_POSTGRES_DSN", "must be set when SESSION_STORE_PROVIDER=postgres")
    ]
    assert [(check.name, check.status) for check in report.checks] == [
        ("sessionStore", "unhealthy")
    ]


def test_postgres_smoke_marks_probe_failure_unready():
    from app.operations.postgres_smoke import PostgresSmokeError, run_postgres_smoke

    def failing_probe(dsn: str, timeout_seconds: float):
        raise PostgresSmokeError("connection refused")

    report = run_postgres_smoke(
        settings=_settings(
            session_store_provider="postgres",
            session_store_postgres_dsn="postgresql://rag:secret@db/ragqs",
        ),
        postgres_probe=failing_probe,
    )

    assert report.ready is False
    assert [(issue.field, issue.message) for issue in report.errors] == [
        ("SESSION_STORE_POSTGRES_DSN", "connection refused")
    ]
    assert [(check.name, check.status) for check in report.checks] == [
        ("sessionStore", "unhealthy")
    ]


def test_postgres_smoke_cli_outputs_json_report():
    from app.operations.postgres_smoke import main

    output = StringIO()
    exit_code = main(
        ["--json"],
        settings=_settings(
            session_store_provider="postgres",
            session_store_postgres_dsn="postgresql://rag:secret@db/ragqs",
        ),
        postgres_probe=lambda dsn, timeout_seconds: {"serverReachable": True},
        output=output,
    )

    assert exit_code == 0
    rendered = output.getvalue()
    assert '"ready": true' in rendered
    assert '"sessionStore"' in rendered
    assert "secret" not in rendered


def _settings(**overrides):
    values = {
        "session_store_provider": "sqlite",
        "session_store_postgres_dsn": "",
        "retrieval_audit_store_provider": "sqlite",
        "retrieval_audit_postgres_dsn": "",
        "indexing_job_store_provider": "sqlite",
        "indexing_job_store_postgres_dsn": "",
        "document_catalog_provider": "sqlite",
        "document_catalog_postgres_dsn": "",
        "checkpoint_provider": "sqlite",
        "checkpoint_postgres_dsn": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)
