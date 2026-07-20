"""Tests for KnowledgeSpace rag_path/owning_department_id and update_space."""

import pytest

from app.knowledge.catalog import (
    InMemoryKnowledgeCatalog,
    KnowledgeSpaceNotFoundError,
)


def test_knowledge_space_defaults_rag_path_and_owning_department_to_none():
    catalog = InMemoryKnowledgeCatalog()
    space = catalog.ensure_space("finance", name="Finance")
    assert space.rag_path is None
    assert space.owning_department_id is None


def test_get_space_returns_existing_space():
    catalog = InMemoryKnowledgeCatalog()
    catalog.ensure_space("finance", name="Finance")
    space = catalog.get_space("finance")
    assert space is not None
    assert space.space_id == "finance"


def test_get_space_returns_none_for_missing_space():
    catalog = InMemoryKnowledgeCatalog()
    assert catalog.get_space("missing") is None


def test_update_space_sets_rag_path():
    catalog = InMemoryKnowledgeCatalog()
    catalog.ensure_space("finance", name="Finance")
    updated = catalog.update_space("finance", rag_path="agentic")
    assert updated.rag_path == "agentic"
    assert catalog.get_space("finance").rag_path == "agentic"


def test_update_space_none_means_unchanged():
    catalog = InMemoryKnowledgeCatalog()
    catalog.ensure_space("finance", name="Finance")
    catalog.update_space("finance", rag_path="agentic")
    updated = catalog.update_space("finance", name="Finance Team")
    assert updated.rag_path == "agentic"
    assert updated.name == "Finance Team"


def test_update_space_clear_rag_path_sets_none():
    catalog = InMemoryKnowledgeCatalog()
    catalog.ensure_space("finance", name="Finance")
    catalog.update_space("finance", rag_path="agentic")
    updated = catalog.update_space("finance", clear_rag_path=True)
    assert updated.rag_path is None


def test_update_space_clear_owning_department_id_sets_none():
    catalog = InMemoryKnowledgeCatalog()
    catalog.ensure_space("finance", name="Finance")
    catalog.update_space("finance", owning_department_id="dept-1")
    updated = catalog.update_space("finance", clear_owning_department_id=True)
    assert updated.owning_department_id is None


def test_update_space_missing_space_raises():
    catalog = InMemoryKnowledgeCatalog()
    with pytest.raises(KnowledgeSpaceNotFoundError):
        catalog.update_space("missing", rag_path="agentic")
