import pytest

from app.security.auth import AuthContext
from app.security.department_store import DepartmentStore
from app.services.department_service import (
    AdminDepartmentAlreadyExistsError,
    AdminDepartmentNotEmptyError,
    AdminDepartmentNotFoundError,
    AdminDepartmentScopeError,
    AdminDepartmentValidationError,
    DepartmentService,
)

_SUPER_ADMIN = AuthContext(user_id="root", roles={"super_admin"}, spaces={"*"})
_DEPARTMENT_ADMIN = AuthContext(
    user_id="lead", roles={"department_admin"}, spaces={"docs"}, metadata={}
)


def _build_service(tmp_path):
    store = DepartmentStore(tmp_path / "auth.sqlite3")
    return DepartmentService(department_store=store), store


def test_super_admin_can_create_list_get_update_delete_department(tmp_path):
    service, _ = _build_service(tmp_path)

    created = service.create_department(actor=_SUPER_ADMIN, name="工程部", description="研发")
    assert set(created) == {"id", "name", "description", "created_at"}

    assert service.list_departments() == [created]
    assert service.get_department(created["id"]) == created

    updated = service.update_department(actor=_SUPER_ADMIN, department_id=created["id"], name="研发部")
    assert updated["name"] == "研发部"

    result = service.delete_department(actor=_SUPER_ADMIN, department_id=created["id"])
    assert result == {"deleted": True, "department_id": created["id"]}
    with pytest.raises(AdminDepartmentNotFoundError):
        service.get_department(created["id"])


def test_department_admin_cannot_create_update_or_delete_department(tmp_path):
    service, store = _build_service(tmp_path)
    created = store.create(name="工程部", description=None)

    with pytest.raises(AdminDepartmentScopeError):
        service.create_department(actor=_DEPARTMENT_ADMIN, name="新部门")
    with pytest.raises(AdminDepartmentScopeError):
        service.update_department(actor=_DEPARTMENT_ADMIN, department_id=created.id, name="改名")
    with pytest.raises(AdminDepartmentScopeError):
        service.delete_department(actor=_DEPARTMENT_ADMIN, department_id=created.id)
    assert store.get_by_id(created.id).name == "工程部"


def test_create_rejects_blank_name(tmp_path):
    service, _ = _build_service(tmp_path)

    with pytest.raises(AdminDepartmentValidationError):
        service.create_department(actor=_SUPER_ADMIN, name="   ")


def test_duplicate_name_is_translated_to_service_error(tmp_path):
    service, _ = _build_service(tmp_path)
    service.create_department(actor=_SUPER_ADMIN, name="工程部")

    with pytest.raises(AdminDepartmentAlreadyExistsError):
        service.create_department(actor=_SUPER_ADMIN, name="工程部")


def test_delete_translates_not_empty_error(tmp_path):
    from app.security.user_store import UserStore

    db_path = tmp_path / "auth.sqlite3"
    store = DepartmentStore(db_path)
    users = UserStore(db_path)
    service = DepartmentService(department_store=store)
    department = store.create(name="工程部", description=None)
    users.create_user(
        username="alice", password_hash="h1", roles=["viewer"], spaces=[],
        department_ids=[department.id],
    )

    with pytest.raises(AdminDepartmentNotEmptyError):
        service.delete_department(actor=_SUPER_ADMIN, department_id=department.id)
