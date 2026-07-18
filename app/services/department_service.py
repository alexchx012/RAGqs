"""Administrator-facing service for managing departments safely."""

from __future__ import annotations

from typing import Any

from app.config import config
from app.security.auth import AuthContext
from app.security.department_store import (
    DepartmentNameAlreadyExistsError,
    DepartmentNotEmptyError,
    DepartmentNotFoundError,
    DepartmentRecord,
    DepartmentStore,
)

_VALIDATION_ERROR = "invalid department input"
_ALREADY_EXISTS_ERROR = "department name already exists"
_NOT_FOUND_ERROR = "department not found"
_NOT_EMPTY_ERROR = "department still has member users"
_SCOPE_ERROR = "only super_admin can manage departments"


class AdminDepartmentServiceError(Exception):
    """Base error for administrator department operations."""


class AdminDepartmentValidationError(AdminDepartmentServiceError):
    """Raised when department input violates a domain rule."""


class AdminDepartmentAlreadyExistsError(AdminDepartmentServiceError):
    """Raised when a department name is already registered."""


class AdminDepartmentNotFoundError(AdminDepartmentServiceError):
    """Raised when a requested department does not exist."""


class AdminDepartmentNotEmptyError(AdminDepartmentServiceError):
    """Raised when deleting a department that still has member users."""


class AdminDepartmentScopeError(AdminDepartmentServiceError):
    """Raised when a non-super_admin actor attempts a department write operation."""


class DepartmentService:
    """Apply super_admin-only write rules around the department store."""

    def __init__(
        self,
        *,
        settings: Any = None,
        department_store: DepartmentStore | None = None,
    ) -> None:
        self.settings = settings if settings is not None else config
        db_path = _auth_setting(self.settings, "local_db_path", "data/auth.sqlite3")
        self.department_store = (
            department_store if department_store is not None else DepartmentStore(db_path)
        )

    def list_departments(self) -> list[dict[str, Any]]:
        """Return every department."""

        return [self._safe_department(d) for d in self.department_store.list()]

    def get_department(self, department_id: str) -> dict[str, Any]:
        """Return one department."""

        department = self.department_store.get_by_id(department_id)
        if department is None:
            raise AdminDepartmentNotFoundError(_NOT_FOUND_ERROR)
        return self._safe_department(department)

    def create_department(
        self, *, actor: AuthContext, name: str, description: str | None = None
    ) -> dict[str, Any]:
        """Validate and persist a new department; only super_admin may call this."""

        self._require_super_admin(actor)
        normalized_name = _normalize_department_name(name)
        try:
            department = self.department_store.create(
                name=normalized_name, description=description
            )
        except DepartmentNameAlreadyExistsError as exc:
            raise AdminDepartmentAlreadyExistsError(_ALREADY_EXISTS_ERROR) from exc
        return self._safe_department(department)

    def update_department(
        self,
        *,
        actor: AuthContext,
        department_id: str,
        name: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Update optional name/description fields; only super_admin may call this."""

        self._require_super_admin(actor)
        normalized_name = None if name is None else _normalize_department_name(name)
        try:
            department = self.department_store.update(
                department_id=department_id, name=normalized_name, description=description
            )
        except DepartmentNotFoundError as exc:
            raise AdminDepartmentNotFoundError(_NOT_FOUND_ERROR) from exc
        except DepartmentNameAlreadyExistsError as exc:
            raise AdminDepartmentAlreadyExistsError(_ALREADY_EXISTS_ERROR) from exc
        return self._safe_department(department)

    def delete_department(
        self, *, actor: AuthContext, department_id: str
    ) -> dict[str, bool | str]:
        """Delete an empty department; only super_admin may call this."""

        self._require_super_admin(actor)
        try:
            self.department_store.delete(department_id)
        except DepartmentNotFoundError as exc:
            raise AdminDepartmentNotFoundError(_NOT_FOUND_ERROR) from exc
        except DepartmentNotEmptyError as exc:
            raise AdminDepartmentNotEmptyError(_NOT_EMPTY_ERROR) from exc
        return {"deleted": True, "department_id": department_id}

    @staticmethod
    def _require_super_admin(actor: AuthContext) -> None:
        if "super_admin" not in actor.roles:
            raise AdminDepartmentScopeError(_SCOPE_ERROR)

    @staticmethod
    def _safe_department(department: DepartmentRecord) -> dict[str, Any]:
        return {
            "id": department.id,
            "name": department.name,
            "description": department.description,
            "created_at": department.created_at,
        }


def _normalize_department_name(name: str) -> str:
    if not isinstance(name, str):
        raise AdminDepartmentValidationError(_VALIDATION_ERROR)
    normalized = name.strip()
    if not normalized:
        raise AdminDepartmentValidationError(_VALIDATION_ERROR)
    return normalized


def _auth_setting(settings: Any, name: str, default: Any) -> Any:
    group = getattr(settings, "auth", None)
    if group is not None and hasattr(group, name):
        return getattr(group, name)
    return getattr(settings, f"auth_{name}", default)
