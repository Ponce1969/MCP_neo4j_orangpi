"""RED-phase tests for QueryKnowledgeGraphUseCase (AC-06.4, AC-06.14-AC-06.17).

These tests drive the API of ``QueryKnowledgeGraphUseCase`` before the use case
itself is implemented. They are expected to fail with an ImportError until
T-06.10 (PR 3) makes them green.
"""

from __future__ import annotations

from typing import Any

import pytest

from book_graph_rag.application.query_knowledge_graph_use_case import (
    QueryKnowledgeGraphUseCase,
)
from book_graph_rag.domain.models import (
    BatchEntityQuery,
    BatchSizeExceededError,
    Entity,
    EntityQuery,
    EntityType,
    EntityWithContext,
    GraphPath,
    GraphQueryResult,
    PathQuery,
    QueryTimeoutError,
    RelationQuery,
    Relationship,
    RelationshipType,
    SimilarityQuery,
    UnsupportedQueryTypeError,
)
from book_graph_rag.ports.graph_query_port import GraphQueryPort


class _FakeGraphQueryPort(GraphQueryPort):
    """Configurable fake for use-case-level tests."""

    def __init__(self) -> None:
        self.find_entity_result: list[EntityWithContext] = []
        self.find_entity_raises: Exception | None = None

        self.find_entities_batch_result: list[EntityWithContext] = []
        self.find_entities_batch_raises: Exception | None = None

        self.traverse_result: tuple[list[EntityWithContext], list[Relationship]] = (
            [],
            [],
        )
        self.traverse_raises: Exception | None = None

        self.find_path_result: list[GraphPath] = []
        self.find_path_raises: Exception | None = None

        self.search_chunks_result: list[dict[str, Any]] = []
        self.search_chunks_raises: Exception | None = None

        self.count_entities_result: int = 0
        self.count_entities_raises: Exception | None = None

        self.list_entities_result: tuple[list[EntityWithContext], int] = ([], 0)
        self.list_entities_raises: Exception | None = None

        self.ensure_indexes_raises: Exception | None = None

        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def find_entity(
        self, name: str, entity_type: EntityType | None
    ) -> list[EntityWithContext]:
        self.calls.append({"method": "find_entity", "name": name, "entity_type": entity_type})
        if self.find_entity_raises is not None:
            raise self.find_entity_raises
        return self.find_entity_result

    async def find_entities_batch(self, ids: list[str]) -> list[EntityWithContext]:
        self.calls.append({"method": "find_entities_batch", "ids": ids})
        if self.find_entities_batch_raises is not None:
            raise self.find_entities_batch_raises
        return self.find_entities_batch_result

    async def traverse_relationships(
        self, source_id: str, rel_type: RelationshipType | None, depth: int
    ) -> tuple[list[EntityWithContext], list[Relationship]]:
        self.calls.append(
            {
                "method": "traverse_relationships",
                "source_id": source_id,
                "rel_type": rel_type,
                "depth": depth,
            }
        )
        if self.traverse_raises is not None:
            raise self.traverse_raises
        return self.traverse_result

    async def find_path(
        self, start_id: str, end_id: str, max_depth: int
    ) -> list[GraphPath]:
        self.calls.append(
            {"method": "find_path", "start_id": start_id, "end_id": end_id, "max_depth": max_depth}
        )
        if self.find_path_raises is not None:
            raise self.find_path_raises
        return self.find_path_result

    async def search_chunks(self, query: str, limit: int) -> list[dict[str, Any]]:
        self.calls.append({"method": "search_chunks", "query": query, "limit": limit})
        if self.search_chunks_raises is not None:
            raise self.search_chunks_raises
        return self.search_chunks_result

    async def count_entities(self, entity_type: str | None) -> int:
        self.calls.append({"method": "count_entities", "entity_type": entity_type})
        if self.count_entities_raises is not None:
            raise self.count_entities_raises
        return self.count_entities_result

    async def list_entities(
        self, cursor: int, page_size: int
    ) -> tuple[list[EntityWithContext], int]:
        self.calls.append({"method": "list_entities", "cursor": cursor, "page_size": page_size})
        if self.list_entities_raises is not None:
            raise self.list_entities_raises
        return self.list_entities_result

    async def ensure_indexes(self) -> None:
        self.calls.append({"method": "ensure_indexes"})
        if self.ensure_indexes_raises is not None:
            raise self.ensure_indexes_raises


@pytest.fixture
def fake_port() -> _FakeGraphQueryPort:
    return _FakeGraphQueryPort()


@pytest.fixture
def use_case(fake_port: _FakeGraphQueryPort) -> QueryKnowledgeGraphUseCase:
    return QueryKnowledgeGraphUseCase(fake_port)


async def test_entity_query_dispatches_to_find_entity(
    fake_port: _FakeGraphQueryPort, use_case: QueryKnowledgeGraphUseCase
) -> None:
    """AC-06.4: entity query calls port.find_entity and returns a GraphQueryResult."""
    entity = Entity(id="e1", name="MCP", type="mcp")
    fake_port.find_entity_result = [EntityWithContext(entity=entity)]

    result = await use_case.execute(EntityQuery(name="MCP"))

    assert isinstance(result, GraphQueryResult)
    assert result.entities == fake_port.find_entity_result
    assert result.metadata.total_count == 1
    assert fake_port.calls == [
        {"method": "find_entity", "name": "MCP", "entity_type": None}
    ]


async def test_entity_query_with_type_filter_passes_entity_type(
    fake_port: _FakeGraphQueryPort, use_case: QueryKnowledgeGraphUseCase
) -> None:
    """EntityQuery with entity_type forwards the filter to the port."""
    await use_case.execute(EntityQuery(name="Agent", entity_type="agent"))

    assert fake_port.calls == [
        {"method": "find_entity", "name": "Agent", "entity_type": "agent"}
    ]


async def test_relation_query_dispatches_to_traverse(
    fake_port: _FakeGraphQueryPort, use_case: QueryKnowledgeGraphUseCase
) -> None:
    """AC-06.4: relation query calls port.traverse_relationships."""
    source = Entity(id="x", name="X", type="concept")
    target = Entity(id="y", name="Y", type="concept")
    rel = Relationship(source_entity_id="x", target_entity_id="y", type="requires")
    fake_port.traverse_result = (
        [EntityWithContext(entity=source), EntityWithContext(entity=target)],
        [rel],
    )

    result = await use_case.execute(RelationQuery(source_id="x", depth=2))

    assert result.entities == fake_port.traverse_result[0]
    assert result.relationships == fake_port.traverse_result[1]
    assert result.metadata.total_count == 2
    assert fake_port.calls == [
        {"method": "traverse_relationships", "source_id": "x", "rel_type": None, "depth": 2}
    ]


async def test_path_query_dispatches_to_find_path(
    fake_port: _FakeGraphQueryPort, use_case: QueryKnowledgeGraphUseCase
) -> None:
    """AC-06.4: path query calls port.find_path."""
    node_a = Entity(id="a", name="A", type="concept")
    node_b = Entity(id="b", name="B", type="concept")
    path = GraphPath(nodes=[node_a, node_b], relationships=[])
    fake_port.find_path_result = [path]

    result = await use_case.execute(PathQuery(start_id="a", end_id="b"))

    assert result.paths == fake_port.find_path_result
    assert result.metadata.total_count == 1
    assert fake_port.calls == [
        {"method": "find_path", "start_id": "a", "end_id": "b", "max_depth": 3}
    ]


async def test_empty_result_returns_zero_total_count(
    fake_port: _FakeGraphQueryPort, use_case: QueryKnowledgeGraphUseCase
) -> None:
    """AC-06.14: empty port results produce a GraphQueryResult with total_count=0."""
    result = await use_case.execute(EntityQuery(name="missing"))

    assert result.entities == []
    assert result.relationships == []
    assert result.paths == []
    assert result.chunks == []
    assert result.metadata.total_count == 0


async def test_batch_entity_query_dispatches_to_find_entities_batch(
    fake_port: _FakeGraphQueryPort, use_case: QueryKnowledgeGraphUseCase
) -> None:
    """AC-06.4 / AC-06.15: batch_entity query calls port.find_entities_batch."""
    entity = Entity(id="e1", name="MCP", type="mcp")
    fake_port.find_entities_batch_result = [EntityWithContext(entity=entity)]

    result = await use_case.execute(BatchEntityQuery(ids=["e1", "e2"]))

    assert result.entities == fake_port.find_entities_batch_result
    assert result.metadata.total_count == 1
    assert fake_port.calls == [
        {"method": "find_entities_batch", "ids": ["e1", "e2"]}
    ]


async def test_batch_size_exceeded_raises_before_calling_port(
    fake_port: _FakeGraphQueryPort, use_case: QueryKnowledgeGraphUseCase
) -> None:
    """AC-06.15: batch > 200 raises BatchSizeExceededError before the port is called."""
    ids = [f"id_{i}" for i in range(201)]

    with pytest.raises(BatchSizeExceededError) as exc_info:
        await use_case.execute(BatchEntityQuery(ids=ids))

    assert exc_info.value.limit == 200
    assert exc_info.value.received == 201
    assert fake_port.calls == []


async def test_unknown_query_type_raises_domain_error(
    fake_port: _FakeGraphQueryPort, use_case: QueryKnowledgeGraphUseCase
) -> None:
    """AC-06.16: unknown dispatch type raises UnsupportedQueryTypeError, not ValueError."""
    class UnknownQuery:
        type = "unknown_type"

    with pytest.raises(UnsupportedQueryTypeError) as exc_info:
        await use_case.execute(UnknownQuery())

    assert exc_info.value.query_type == "unknown_type"


async def test_depth_zero_is_valid_and_calls_port(
    fake_port: _FakeGraphQueryPort, use_case: QueryKnowledgeGraphUseCase
) -> None:
    """AC-06.17: depth=0 relation query returns the start entity without traversal."""
    source = Entity(id="x", name="X", type="concept")
    fake_port.traverse_result = ([EntityWithContext(entity=source)], [])

    result = await use_case.execute(RelationQuery(source_id="x", depth=0))

    assert result.entities == [EntityWithContext(entity=source)]
    assert result.relationships == []
    assert fake_port.calls == [
        {"method": "traverse_relationships", "source_id": "x", "rel_type": None, "depth": 0}
    ]


async def test_timeout_error_propagates(
    fake_port: _FakeGraphQueryPort, use_case: QueryKnowledgeGraphUseCase
) -> None:
    """AC-06.10: QueryTimeoutError raised by the port is not swallowed."""
    fake_port.find_entity_raises = QueryTimeoutError("neo4j timeout")

    with pytest.raises(QueryTimeoutError):
        await use_case.execute(EntityQuery(name="slow"))


async def test_depth_is_clamped_to_maximum_three(
    fake_port: _FakeGraphQueryPort, use_case: QueryKnowledgeGraphUseCase
) -> None:
    """RelationQuery depth > 3 is clamped to 3 before calling the port."""
    await use_case.execute(RelationQuery(source_id="x", depth=5))

    assert fake_port.calls == [
        {"method": "traverse_relationships", "source_id": "x", "rel_type": None, "depth": 3}
    ]


async def test_similarity_query_raises_unsupported(
    fake_port: _FakeGraphQueryPort, use_case: QueryKnowledgeGraphUseCase
) -> None:
    """SimilarityQuery is reserved and raises UnsupportedQueryTypeError."""
    with pytest.raises(UnsupportedQueryTypeError) as exc_info:
        await use_case.execute(SimilarityQuery(text="semantic search"))

    assert exc_info.value.query_type == "similarity"
    assert fake_port.calls == []
