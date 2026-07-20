from datetime import UTC, datetime
from types import SimpleNamespace

import app.knowledge as knowledge
from app.ingestion import IndexingJob
from app.knowledge import DocumentStatus
from app.services.vector_index_service import _build_default_document_catalog


def test_postgres_knowledge_catalog_persists_spaces_documents_and_deletions():
    now = datetime(2026, 5, 24, 12, 0, tzinfo=UTC)
    database = FakePostgresCatalogDatabase()
    catalog_class = _postgres_catalog_class()
    catalog = catalog_class(
        "postgresql://rag:secret@db/ragqs",
        connector=database.connect,
    )
    job = IndexingJob.create(
        document_id="doc-1",
        source_path="/docs/guide.md",
        job_id="job-1",
        space_id="finance",
        created_at=now,
    )
    job.start(now=now)
    job.complete(total_chunks=2, indexed_chunks=2, now=now)

    catalog.ensure_space("finance", name="Finance", description="Finance docs")
    indexed = catalog.upsert_from_job(job)
    deleted = catalog.mark_deleted("finance", indexed.document_id)

    second_catalog = catalog_class(
        "postgresql://rag:secret@db/ragqs",
        connector=database.connect,
    )
    spaces = second_catalog.list_spaces()
    loaded = second_catalog.get_document("finance", "doc-1")

    assert database.dsns == ["postgresql://rag:secret@db/ragqs"]
    assert any(space.space_id == "finance" and space.name == "Finance" for space in spaces)
    assert deleted.status is DocumentStatus.DELETED
    assert loaded is not None
    assert loaded.status is DocumentStatus.DELETED
    assert loaded.file_name == "guide.md"
    assert loaded.latest_job_id == "job-1"
    assert loaded.total_chunks == 2
    assert [record.document_id for record in second_catalog.list_documents("finance")] == ["doc-1"]


def test_postgres_knowledge_catalog_defers_connection_until_first_operation():
    database = FakePostgresCatalogDatabase()

    catalog = _postgres_catalog_class()(
        "postgresql://rag:secret@db/ragqs",
        connector=database.connect,
    )

    assert database.connect_count == 0

    catalog.list_spaces()

    assert database.connect_count == 1


def test_vector_index_service_can_select_postgres_document_catalog_by_config():
    catalog = _build_default_document_catalog(
        SimpleNamespace(
            document_catalog_provider="postgres",
            document_catalog_postgres_dsn="postgresql://rag:secret@db/ragqs",
        )
    )

    assert isinstance(catalog, _postgres_catalog_class())
    assert catalog.dsn == "postgresql://rag:secret@db/ragqs"


def _postgres_catalog_class():
    assert hasattr(knowledge, "PostgresKnowledgeCatalog")
    return knowledge.PostgresKnowledgeCatalog


class FakePostgresCatalogDatabase:
    def __init__(self):
        self.space_rows = []
        self.document_rows = []
        self.connect_count = 0
        self.dsns = []
        self.next_space_sequence = 1
        self.next_document_sequence = 1
        # Baseline CREATE TABLE columns; ALTER TABLE ADD COLUMN extends this set.
        self.knowledge_space_columns = {
            "sequence",
            "space_id",
            "name",
            "description",
            "created_at",
        }

    def connect(self, dsn: str):
        self.connect_count += 1
        if dsn not in self.dsns:
            self.dsns.append(dsn)
        return FakeConnection(self)


class FakeConnection:
    def __init__(self, database: FakePostgresCatalogDatabase):
        self.database = database

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def cursor(self):
        return FakeCursor(self.database)

    def close(self):
        pass


class FakeCursor:
    def __init__(self, database: FakePostgresCatalogDatabase):
        self.database = database
        self.results = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql: str, params=()):
        normalized = " ".join(sql.lower().split())
        if normalized.startswith("create table") or normalized.startswith("create index"):
            self.results = []
            return self
        if normalized.startswith(
            "select column_name from information_schema.columns where table_name = 'knowledge_spaces'"
        ):
            self.results = [
                {"column_name": name} for name in sorted(self.database.knowledge_space_columns)
            ]
            return self
        if normalized.startswith("alter table knowledge_spaces add column"):
            # e.g. "alter table knowledge_spaces add column rag_path text"
            parts = normalized.split()
            # ["alter","table","knowledge_spaces","add","column","rag_path","text"]
            column_name = parts[5]
            self.database.knowledge_space_columns.add(column_name)
            for row in self.database.space_rows:
                row.setdefault(column_name, None)
            self.results = []
            return self
        if normalized.startswith("select * from knowledge_spaces where space_id"):
            space_id = params[0]
            self.results = [
                row for row in self.database.space_rows if row["space_id"] == space_id
            ]
            return self
        if normalized.startswith("insert into knowledge_spaces"):
            self._insert_space(params)
            self.results = []
            return self
        if normalized.startswith("update knowledge_spaces set"):
            self._update_space(normalized, params)
            self.results = []
            return self
        if normalized.startswith("select * from knowledge_spaces"):
            self.results = sorted(
                self.database.space_rows,
                key=lambda row: row["sequence"],
            )
            return self
        if normalized.startswith("insert into document_records"):
            self._upsert_document(params)
            self.results = []
            return self
        if normalized.startswith("select * from document_records where space_id = %s and document_id = %s"):
            space_id, document_id = params
            self.results = [
                row
                for row in self.database.document_rows
                if row["space_id"] == space_id and row["document_id"] == document_id
            ]
            return self
        if normalized.startswith("select * from document_records where space_id"):
            space_id = params[0]
            self.results = [
                row
                for row in sorted(
                    self.database.document_rows,
                    key=lambda item: item["sequence"],
                )
                if row["space_id"] == space_id
            ]
            return self
        if normalized.startswith("update document_records set status"):
            status, updated_at, space_id, document_id = params
            for row in self.database.document_rows:
                if row["space_id"] == space_id and row["document_id"] == document_id:
                    row["status"] = status
                    row["updated_at"] = updated_at
            self.results = []
            return self
        raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self):
        return self.results[0] if self.results else None

    def fetchall(self):
        return list(self.results)

    def _insert_space(self, params):
        space_id, name, description, created_at = params
        if any(row["space_id"] == space_id for row in self.database.space_rows):
            return
        self.database.space_rows.append(
            {
                "sequence": self.database.next_space_sequence,
                "space_id": space_id,
                "name": name,
                "description": description,
                "created_at": created_at,
                "rag_path": None,
                "owning_department_id": None,
            }
        )
        self.database.next_space_sequence += 1

    def _update_space(self, normalized: str, params):
        # UPDATE ... WHERE space_id = %s  — last param is always space_id.
        space_id = params[-1]
        set_clause = normalized.split(" set ", 1)[1].split(" where ", 1)[0]
        assignments = [part.strip() for part in set_clause.split(",")]
        values = list(params[:-1])
        if len(values) != len(assignments):
            raise AssertionError(
                f"update knowledge_spaces param mismatch: {assignments!r} vs {params!r}"
            )
        for row in self.database.space_rows:
            if row["space_id"] != space_id:
                continue
            for assignment, value in zip(assignments, values, strict=True):
                column = assignment.split("=", 1)[0].strip()
                row[column] = value
            return

    def _upsert_document(self, params):
        (
            document_id,
            space_id,
            source_path,
            file_name,
            status,
            latest_job_id,
            total_chunks,
            indexed_chunks,
            errors_json,
            updated_at,
        ) = params
        existing = next(
            (
                row
                for row in self.database.document_rows
                if row["space_id"] == space_id and row["document_id"] == document_id
            ),
            None,
        )
        row = {
            "document_id": document_id,
            "space_id": space_id,
            "source_path": source_path,
            "file_name": file_name,
            "status": status,
            "latest_job_id": latest_job_id,
            "total_chunks": total_chunks,
            "indexed_chunks": indexed_chunks,
            "errors_json": errors_json,
            "updated_at": updated_at,
        }
        if existing is None:
            row["sequence"] = self.database.next_document_sequence
            self.database.next_document_sequence += 1
            self.database.document_rows.append(row)
        else:
            row["sequence"] = existing["sequence"]
            existing.update(row)
