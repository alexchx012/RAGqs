"""Tests for department-scoped knowledge space listing and creation."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import file as file_api
from app.knowledge.catalog import InMemoryKnowledgeCatalog
from app.security.auth import AuthContext, get_current_auth_context
from app.services.vector_index_service import VectorIndexService


def _client_with_catalog(catalog, *, roles, department_id=None):
    service = VectorIndexService(document_catalog=catalog)
    app = FastAPI()
    app.include_router(file_api.router, prefix="/api")
    app.dependency_overrides[get_current_auth_context] = lambda: AuthContext(
        user_id="caller", roles=set(roles), spaces={"*"}, department_id=department_id
    )
    original_service = file_api.vector_index_service
    file_api.vector_index_service = service
    return TestClient(app), original_service


def test_department_admin_lists_only_own_department_spaces():
    catalog = InMemoryKnowledgeCatalog()
    catalog.ensure_space("finance", name="Finance")
    catalog.update_space("finance", owning_department_id="dept-1")
    catalog.ensure_space("hr", name="HR")
    catalog.update_space("hr", owning_department_id="dept-2")

    client, original_service = _client_with_catalog(
        catalog, roles={"department_admin"}, department_id="dept-1"
    )
    try:
        response = client.get("/api/knowledge-spaces")
        assert response.status_code == 200
        space_ids = {space["space_id"] for space in response.json()["data"]["spaces"]}
        assert space_ids == {"finance"}
    finally:
        file_api.vector_index_service = original_service


def test_department_admin_with_no_department_sees_no_spaces():
    catalog = InMemoryKnowledgeCatalog()
    catalog.ensure_space("finance", name="Finance")
    catalog.update_space("finance", owning_department_id="dept-1")

    client, original_service = _client_with_catalog(
        catalog, roles={"department_admin"}, department_id=None
    )
    try:
        response = client.get("/api/knowledge-spaces")
        assert response.status_code == 200
        assert response.json()["data"]["spaces"] == []
    finally:
        file_api.vector_index_service = original_service


def test_maintainer_creating_space_without_department_is_rejected():
    catalog = InMemoryKnowledgeCatalog()
    client, original_service = _client_with_catalog(catalog, roles={"maintainer"})
    try:
        response = client.post(
            "/api/knowledge-spaces",
            json={"space_id": "ops", "name": "Ops"},
        )
        assert response.status_code == 422
    finally:
        file_api.vector_index_service = original_service


def test_super_admin_creating_space_without_department_succeeds():
    catalog = InMemoryKnowledgeCatalog()
    client, original_service = _client_with_catalog(catalog, roles={"super_admin"})
    try:
        response = client.post(
            "/api/knowledge-spaces",
            json={"space_id": "ops", "name": "Ops"},
        )
        assert response.status_code == 200
        assert response.json()["data"]["space"]["owning_department_id"] is None
    finally:
        file_api.vector_index_service = original_service


def test_maintainer_creating_space_with_department_succeeds():
    catalog = InMemoryKnowledgeCatalog()
    client, original_service = _client_with_catalog(catalog, roles={"maintainer"})
    try:
        response = client.post(
            "/api/knowledge-spaces",
            json={"space_id": "ops", "name": "Ops", "owning_department_id": "dept-1"},
        )
        assert response.status_code == 200
        assert response.json()["data"]["space"]["owning_department_id"] == "dept-1"
    finally:
        file_api.vector_index_service = original_service


def test_maintainer_can_update_name_and_rag_path():
    catalog = InMemoryKnowledgeCatalog()
    catalog.ensure_space("finance", name="Finance")
    catalog.update_space("finance", owning_department_id="dept-1")

    client, original_service = _client_with_catalog(catalog, roles={"maintainer"})
    try:
        response = client.patch(
            "/api/knowledge-spaces/finance",
            json={"name": "Finance Team", "rag_path": "agentic"},
        )
        assert response.status_code == 200
        body = response.json()["data"]["space"]
        assert body["name"] == "Finance Team"
        assert body["rag_path"] == "agentic"
    finally:
        file_api.vector_index_service = original_service


def test_maintainer_cannot_reassign_owning_department_id():
    catalog = InMemoryKnowledgeCatalog()
    catalog.ensure_space("finance", name="Finance")
    catalog.update_space("finance", owning_department_id="dept-1")

    client, original_service = _client_with_catalog(catalog, roles={"maintainer"})
    try:
        response = client.patch(
            "/api/knowledge-spaces/finance",
            json={"owning_department_id": "dept-2"},
        )
        assert response.status_code == 403
    finally:
        file_api.vector_index_service = original_service


def test_super_admin_can_reassign_owning_department_id():
    catalog = InMemoryKnowledgeCatalog()
    catalog.ensure_space("finance", name="Finance")
    catalog.update_space("finance", owning_department_id="dept-1")

    client, original_service = _client_with_catalog(catalog, roles={"super_admin"})
    try:
        response = client.patch(
            "/api/knowledge-spaces/finance",
            json={"owning_department_id": "dept-2"},
        )
        assert response.status_code == 200
        assert response.json()["data"]["space"]["owning_department_id"] == "dept-2"
    finally:
        file_api.vector_index_service = original_service


def test_department_admin_can_update_rag_path_for_own_space():
    catalog = InMemoryKnowledgeCatalog()
    catalog.ensure_space("finance", name="Finance")
    catalog.update_space("finance", owning_department_id="dept-1")

    client, original_service = _client_with_catalog(
        catalog, roles={"department_admin"}, department_id="dept-1"
    )
    try:
        response = client.patch(
            "/api/knowledge-spaces/finance",
            json={"rag_path": "agentic"},
        )
        assert response.status_code == 200
        assert response.json()["data"]["space"]["rag_path"] == "agentic"
    finally:
        file_api.vector_index_service = original_service


def test_department_admin_cannot_update_other_department_space():
    catalog = InMemoryKnowledgeCatalog()
    catalog.ensure_space("finance", name="Finance")
    catalog.update_space("finance", owning_department_id="dept-1")

    client, original_service = _client_with_catalog(
        catalog, roles={"department_admin"}, department_id="dept-2"
    )
    try:
        response = client.patch(
            "/api/knowledge-spaces/finance",
            json={"rag_path": "agentic"},
        )
        assert response.status_code == 403
    finally:
        file_api.vector_index_service = original_service


def test_department_admin_cannot_update_space_with_no_department_owner():
    catalog = InMemoryKnowledgeCatalog()
    catalog.ensure_space("finance", name="Finance")

    client, original_service = _client_with_catalog(
        catalog, roles={"department_admin"}, department_id="dept-1"
    )
    try:
        response = client.patch(
            "/api/knowledge-spaces/finance",
            json={"rag_path": "agentic"},
        )
        assert response.status_code == 403
    finally:
        file_api.vector_index_service = original_service


def test_department_admin_cannot_update_name_field():
    catalog = InMemoryKnowledgeCatalog()
    catalog.ensure_space("finance", name="Finance")
    catalog.update_space("finance", owning_department_id="dept-1")

    client, original_service = _client_with_catalog(
        catalog, roles={"department_admin"}, department_id="dept-1"
    )
    try:
        response = client.patch(
            "/api/knowledge-spaces/finance",
            json={"name": "Renamed"},
        )
        assert response.status_code == 403
    finally:
        file_api.vector_index_service = original_service
