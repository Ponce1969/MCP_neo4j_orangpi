"""Tests for the read-side GraphQueryPort (AC-06.1)."""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from book_graph_rag.domain.models import (
    EntityType,
    EntityWithContext,
    GraphPath,
    Relationship,
    RelationshipType,
)
from book_graph_rag.ports.graph_query_port import GraphQueryPort


class _CompleteQueryAdapter(GraphQueryPort):
    """Every abstract method is implemented so instantiation should succeed."""

    async def find_entity(
        self, name: str, entity_type: EntityType | None
    ) -> list[EntityWithContext]:
        return []

    async def find_entities_batch(self, ids: list[str]) -> list[EntityWithContext]:
        return []

    async def traverse_relationships(
        self, source_id: str, rel_type: RelationshipType | None, depth: int
    ) -> tuple[list[EntityWithContext], list[Relationship]]:
        return [], []

    async def find_path(
        self, start_id: str, end_id: str, max_depth: int
    ) -> list[GraphPath]:
        return []

    async def search_chunks(self, query: str, limit: int) -> list[dict[str, Any]]:
        return []

    async def count_entities(self, entity_type: str | None) -> int:
        return 0

    async def list_entities(
        self, cursor: int, page_size: int
    ) -> tuple[list[EntityWithContext], int]:
        return [], 0

    async def ensure_indexes(self) -> None:
        return None


class _IncompleteQueryAdapter(GraphQueryPort):
    """Missing one method so instantiation should fail."""

    async def find_entity(
        self, name: str, entity_type: EntityType | None
    ) -> list[EntityWithContext]:
        return []

    async def find_entities_batch(self, ids: list[str]) -> list[EntityWithContext]:
        return []

    async def traverse_relationships(
        self, source_id: str, rel_type: RelationshipType | None, depth: int
    ) -> tuple[list[EntityWithContext], list[Relationship]]:
        return [], []

    async def find_path(
        self, start_id: str, end_id: str, max_depth: int
    ) -> list[GraphPath]:
        return []

    async def search_chunks(self, query: str, limit: int) -> list[dict[str, Any]]:
        return []

    async def count_entities(self, entity_type: str | None) -> int:
        return 0

    async def list_entities(
        self, cursor: int, page_size: int
    ) -> tuple[list[EntityWithContext], int]:
        return [], 0

    # ensure_indexes intentionally omitted


def test_graph_query_port_is_abstract() -> None:
    """GraphQueryPort cannot be instantiated directly."""
    with pytest.raises(TypeError):
        GraphQueryPort()


def test_graph_query_port_complete_subclass_can_be_instantiated() -> None:
    """A subclass implementing all 8 methods can be instantiated."""
    adapter = _CompleteQueryAdapter()

    assert adapter is not None


def test_graph_query_port_missing_method_cannot_be_instantiated() -> None:
    """A subclass missing a method is still abstract."""
    with pytest.raises(TypeError):
        _IncompleteQueryAdapter()


@pytest.mark.parametrize(
    "method_name",
    [
        "find_entity",
        "find_entities_batch",
        "traverse_relationships",
        "find_path",
        "search_chunks",
        "count_entities",
        "list_entities",
        "ensure_indexes",
    ],
)
def test_graph_query_port_methods_are_async(method_name: str) -> None:
    """Every port method is declared async."""
    method = getattr(GraphQueryPort, method_name)

    assert inspect.iscoroutinefunction(method)
