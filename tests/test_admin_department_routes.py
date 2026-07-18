from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import admin_departments
from app.main import create_app
from app.security.auth import AuthContext, get_current_auth_context
from app.security.department_store import DepartmentStore
from app.services.department_service import DepartmentService


def _client_for(tmp_path, *, roles):
    store = DepartmentStore(tmp_path / "auth.sqlite3")
    service = DepartmentService(department_store=store)
    application = FastAPI()
    application.include_router(admin_departments.router, prefix="/api")
    application.dependency_overrides[admin_departments.admin_department_service_dependency] = (
        lambda: service
    )
    application.dependency_overrides[get_current_auth_context] = lambda: AuthContext(
        user_id="caller", roles=set(roles), spaces=set()
    )
    return TestClient(application), service, store


def test_create_app_mounts_admin_department_routes(tmp_path):
    application = create_app(static_dir=str(tmp_path))
    paths = set(application.openapi()["paths"])

    assert "/api/admin/departments" in paths
    assert "/api/admin/departments/{department_id}" in paths


def test_super_admin_can_crud_department_over_http(tmp_path):
    client, _, _ = _client_for(tmp_path, roles={"super_admin"})

    created = client.post(
        "/api/admin/departments", json={"name": "工程部", "description": "研发"}
    )
    assert created.status_code == 200
    department = created.json()["data"]["department"]
    assert set(department) == {"id", "name", "description", "created_at"}

    listed = client.get("/api/admin/departments")
    assert listed.status_code == 200
    assert listed.json()["data"]["departments"] == [department]

    detail = client.get(f"/api/admin/departments/{department['id']}")
    assert detail.status_code == 200
    assert detail.json()["data"]["department"] == department

    updated = client.patch(
        f"/api/admin/departments/{department['id']}", json={"name": "研发部"}
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["department"]["name"] == "研发部"

    deleted = client.delete(f"/api/admin/departments/{department['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["data"] == {"deleted": True, "department_id": department["id"]}
    assert client.get(f"/api/admin/departments/{department['id']}").status_code == 404


def test_department_admin_gets_403_for_all_department_endpoints(tmp_path):
    client, _, store = _client_for(tmp_path, roles={"department_admin"})
    existing = store.create(name="工程部", description=None)

    responses = [
        client.get("/api/admin/departments"),
        client.get(f"/api/admin/departments/{existing.id}"),
        client.post("/api/admin/departments", json={"name": "新部门"}),
        client.patch(f"/api/admin/departments/{existing.id}", json={"name": "改名"}),
        client.delete(f"/api/admin/departments/{existing.id}"),
    ]

    assert [response.status_code for response in responses] == [403] * 5


def test_department_error_mapping_returns_stable_status_and_detail(tmp_path):
    client, _, store = _client_for(tmp_path, roles={"super_admin"})
    existing = store.create(name="工程部", description=None)

    duplicate = client.post("/api/admin/departments", json={"name": "工程部"})
    blank = client.post("/api/admin/departments", json={"name": "   "})
    missing = client.get("/api/admin/departments/missing")

    assert duplicate.status_code == 409
    assert blank.status_code == 422
    assert missing.status_code == 404
    assert duplicate.json()["detail"] == "department name already exists"
    assert blank.json()["detail"] == "invalid department input"
    assert missing.json()["detail"] == "department not found"


def test_delete_non_empty_department_returns_409(tmp_path):
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
    application = FastAPI()
    application.include_router(admin_departments.router, prefix="/api")
    application.dependency_overrides[admin_departments.admin_department_service_dependency] = (
        lambda: service
    )
    application.dependency_overrides[get_current_auth_context] = lambda: AuthContext(
        user_id="caller", roles={"super_admin"}, spaces=set()
    )

    response = TestClient(application).delete(f"/api/admin/departments/{department.id}")

    assert response.status_code == 409
    assert response.json()["detail"] == "department still has member users"
