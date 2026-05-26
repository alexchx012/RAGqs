from io import StringIO
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


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
            indexing_queue_provider="postgres",
            indexing_queue_postgres_dsn="postgresql://rag:secret@db/ragqs-queue",
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
        ("postgresql://rag:secret@db/ragqs-queue", 2.5),
        ("postgresql://rag:secret@db/ragqs-indexing", 2.5),
        ("postgresql://rag:secret@db/ragqs-documents", 2.5),
        ("postgresql://rag:secret@db/ragqs-checkpoints", 2.5),
    ]
    assert [(check.name, check.status) for check in report.checks] == [
        ("sessionStore", "healthy"),
        ("retrievalAuditStore", "healthy"),
        ("indexingQueue", "healthy"),
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


def test_postgres_smoke_can_validate_write_path_for_configured_store(monkeypatch):
    from app.operations import postgres_smoke

    calls = []

    def write_probe(dsn: str, timeout_seconds: float):
        calls.append((dsn, timeout_seconds))
        return {"serverReachable": True, "writePathValidated": True}

    monkeypatch.setattr(postgres_smoke, "probe_postgres_write_path", write_probe)

    report = postgres_smoke.run_postgres_smoke(
        settings=_settings(
            session_store_provider="postgres",
            session_store_postgres_dsn="postgresql://rag:secret@db/ragqs",
        ),
        timeout_seconds=3.5,
        validate_write_path=True,
    )

    assert report.ready is True
    assert calls == [("postgresql://rag:secret@db/ragqs", 3.5)]
    assert [(check.name, check.status, check.message) for check in report.checks] == [
        ("sessionStore", "healthy", "write path validated")
    ]
    assert report.checks[0].details["writePathValidated"] is True


def test_postgres_smoke_marks_failed_write_path_validation_unready():
    from app.operations.postgres_smoke import run_postgres_smoke

    report = run_postgres_smoke(
        settings=_settings(
            session_store_provider="postgres",
            session_store_postgres_dsn="postgresql://rag:secret@db/ragqs",
        ),
        postgres_probe=lambda dsn, timeout_seconds: {
            "serverReachable": True,
            "writePathValidated": False,
        },
    )

    assert report.ready is False
    assert [(issue.field, issue.message) for issue in report.errors] == [
        ("SESSION_STORE_POSTGRES_DSN", "write path validation failed")
    ]
    assert [(check.name, check.status, check.message) for check in report.checks] == [
        ("sessionStore", "unhealthy", "write path validation failed")
    ]


def test_probe_postgres_write_path_uses_temp_table_and_rolls_back():
    from app.operations.postgres_smoke import probe_postgres_write_path

    class FakeCursor:
        def __init__(self):
            self.statements = []
            self.result = {"ok": 1}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def execute(self, statement, params=None):
            self.statements.append((statement, params))
            if "SELECT value" in statement:
                self.result = {"value": "ok"}

        def fetchone(self):
            return self.result

    class FakeConnection:
        def __init__(self):
            self.cursor_instance = FakeCursor()
            self.rollback_count = 0

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def cursor(self):
            return self.cursor_instance

        def rollback(self):
            self.rollback_count += 1

    connection = FakeConnection()
    calls = []

    def connector(dsn, **kwargs):
        calls.append((dsn, kwargs))
        return connection

    result = probe_postgres_write_path(
        "postgresql://rag:secret@db/ragqs",
        timeout_seconds=2.4,
        connector=connector,
    )

    statements = [statement for statement, _ in connection.cursor_instance.statements]
    assert result == {"serverReachable": True, "writePathValidated": True}
    assert calls == [
        (
            "postgresql://rag:secret@db/ragqs",
            {"connect_timeout": 2, "row_factory": None},
        )
    ]
    assert statements[0] == "SELECT 1 AS ok"
    assert "CREATE TEMP TABLE ragqs_smoke_write_path" in statements[1]
    assert "ON COMMIT DROP" in statements[1]
    assert statements[2].startswith("INSERT INTO ragqs_smoke_write_path")
    assert statements[3].startswith("SELECT value FROM ragqs_smoke_write_path")
    assert connection.rollback_count == 1


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


def test_deployment_docs_list_multi_instance_data_layer_gate_without_claiming_validation():
    docs = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")

    for phrase in [
        "Multi-Instance Data Layer Gate",
        "not validated real multi-instance production data behavior",
        "SESSION_STORE_PROVIDER=postgres",
        "RETRIEVAL_AUDIT_STORE_PROVIDER=postgres",
        "INDEXING_QUEUE_PROVIDER=postgres",
        "INDEXING_JOB_STORE_PROVIDER=postgres",
        "DOCUMENT_CATALOG_PROVIDER=postgres",
        "CHECKPOINT_PROVIDER=postgres",
        "run-postgres-smoke.ps1 -RequireConfigured -ValidateWritePath -Json",
    ]:
        assert phrase in docs


def _settings(**overrides):
    values = {
        "session_store_provider": "sqlite",
        "session_store_postgres_dsn": "",
        "retrieval_audit_store_provider": "sqlite",
        "retrieval_audit_postgres_dsn": "",
        "indexing_queue_provider": "memory",
        "indexing_queue_postgres_dsn": "",
        "indexing_job_store_provider": "sqlite",
        "indexing_job_store_postgres_dsn": "",
        "document_catalog_provider": "sqlite",
        "document_catalog_postgres_dsn": "",
        "checkpoint_provider": "sqlite",
        "checkpoint_postgres_dsn": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)
