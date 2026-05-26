"""Ingestion provider implementations."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

from app.providers.contracts import IngestionResult


@dataclass
class VectorIndexIngestionProvider:
    """Adapter around the current vector index service."""

    index_service: Any
    execution_mode: str = "sync"
    background_worker: Any | None = None

    def index_file(self, file_path: str, space_id: str = "default") -> IngestionResult:
        try:
            if _normalize_mode(self.execution_mode) == "background":
                if self.background_worker is None:
                    raise RuntimeError("background indexing worker is not configured")
                job = _call_with_optional_space_id(
                    self.index_service.create_pending_indexing_job,
                    file_path,
                    space_id=space_id,
                )
                self.background_worker.enqueue(job.job_id)
                return IngestionResult(
                    success=True,
                    source=file_path,
                    document_count=0,
                    metadata={
                        "indexing_job": job,
                        "queued": True,
                        "execution_mode": "background",
                    },
                )

            job = _call_with_optional_space_id(
                self.index_service.index_single_file,
                file_path,
                space_id=space_id,
            )
            return IngestionResult(
                success=True,
                source=file_path,
                document_count=1,
                metadata={"indexing_job": job, "execution_mode": "sync"},
            )
        except Exception as e:
            return IngestionResult(success=False, source=file_path, error_message=str(e))

    def index_directory(self, directory_path: str, space_id: str = "default") -> IngestionResult:
        try:
            result = _call_with_optional_space_id(
                self.index_service.index_directory,
                directory_path,
                space_id=space_id,
            )
            result_data = result.to_dict()
            return IngestionResult(
                success=bool(result_data.get("success")),
                source=directory_path,
                document_count=int(result_data.get("success_count", 0)),
                error_message=getattr(result, "error_message", ""),
                metadata=result_data,
            )
        except Exception as e:
            return IngestionResult(success=False, source=directory_path, error_message=str(e))


def _call_with_optional_space_id(method: Any, path: str, *, space_id: str) -> Any:
    if _accepts_keyword(method, "space_id"):
        return method(path, space_id=space_id)
    return method(path)


def _normalize_mode(value: str) -> str:
    return str(value).strip().lower().replace("-", "_")


def _accepts_keyword(method: Any, keyword: str) -> bool:
    parameters = inspect.signature(method).parameters.values()
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD or parameter.name == keyword
        for parameter in parameters
    )
