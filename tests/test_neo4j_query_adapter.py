"""Tests for Neo4jQueryAdapter (AC-06.2, AC-06.5-AC-06.10, AC-06.17)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from book_graph_rag.config import Settings
from book_graph_rag.domain.models import (
    Entity,
    EntityWithContext,
    QueryTimeoutError,
)
from book_graph_rag.infrastructure.neo4j_query_adapter import Neo4jQueryAdapter


class _FakeRecord:
    """Dict-like record returned by a fake Neo4j result."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def data(self) -> dict[str, Any]:
        return self._data


class _FakeResult:
    """Async iterable of records."""

    def __init__(self, records: list[_FakeRecord]) -> None:
        self._records = records

    async def __aiter__(self):
        for record in self._records:
            yield record


class _FakeRelationship:
    """Mimics a Neo4j Relationship object with ``start_node``/``end_node``."""

    def __init__(
        self,
        start_node: _FakeRecord,
        end_node: _FakeRecord,
        rel_type: str,
        description: str = "",
        source_page: int | None = None,
    ) -> None:
        self.start_node = start_node
        self.end_node = end_node
        self.type = rel_type
        self._data = {"description": description, "source_page": source_page}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)


class _FakeSession:
    """Records Cypher queries and yields configurable records."""

    def __init__(
        self,
        records: list[_FakeRecord] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._records = records or []
        self._raise = raise_exc
        self.queries: list[tuple[str, dict[str, Any]]] = []

    async def run(
        self, query: str, parameters: dict[str, Any] | None = None
    ) -> _FakeResult:
        self.queries.append((query, parameters or {}))
        if self._raise is not None:
            raise self._raise
        return _FakeResult(self._records)

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass


class _FakeDriver:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def session(self) -> _FakeSession:
        return self._session

    async def close(self) -> None:
        pass


class _FakeGraphDatabase:
    """Stand-in for ``neo4j.AsyncGraphDatabase`` that records driver construction."""

    def __init__(self) -> None:
        self.driver_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self._driver: _FakeDriver | None = None

    def driver(self, *args: Any, **kwargs: Any) -> _FakeDriver:
        self.driver_calls.append((args, kwargs))
        if self._driver is None:
            self._driver = _FakeDriver(_FakeSession())
        return self._driver


@pytest.fixture
def fake_graph_database() -> _FakeGraphDatabase:
    return _FakeGraphDatabase()


@pytest.fixture
def adapter(
    fake_graph_database: _FakeGraphDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> Neo4jQueryAdapter:
    monkeypatch.setattr(
        "book_graph_rag.infrastructure.neo4j_query_adapter.AsyncGraphDatabase",
        fake_graph_database,
    )
    settings = Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
        }
    )
    return Neo4jQueryAdapter(settings)


# ── T-06.5: constructor + timeout wrapper ────────────────────────────────────


def test_adapter_requires_settings() -> None:
    """Adapter constructor requires Settings."""
    with pytest.raises(TypeError):
        Neo4jQueryAdapter()  # type: ignore[call-arg]


def test_adapter_creates_driver_from_settings(
    adapter: Neo4jQueryAdapter,
    fake_graph_database: _FakeGraphDatabase,
) -> None:
    """Constructor creates the async driver using the same pattern as the command adapter."""
    assert len(fake_graph_database.driver_calls) == 1
    args, kwargs = fake_graph_database.driver_calls[0]
    assert args[0] == "bolt://localhost:7687"
    assert kwargs["auth"] == ("neo4j", "secret")


async def test_run_with_timeout_returns_result(adapter: Neo4jQueryAdapter) -> None:
    """A coroutine that finishes within the timeout returns its value."""

    async def coro() -> str:
        return "ok"

    result = await adapter._run_with_timeout(coro())
    assert result == "ok"


async def test_run_with_timeout_raises_query_timeout(adapter: Neo4jQueryAdapter) -> None:
    """A slow coroutine is converted into a domain QueryTimeoutError."""

    async def slow() -> None:
        await asyncio.sleep(10)

    with pytest.raises(QueryTimeoutError, match="Query exceeded 0.01s timeout"):
        await adapter._run_with_timeout(slow(), timeout=0.01)


# ── T-06.6: find_entity + find_entities_batch ────────────────────────────────


def _make_session(records: list[_FakeRecord]) -> _FakeSession:
    """Build a fake session that returns the supplied records."""
    return _FakeSession(records=records)


async def test_find_entity_by_name(adapter: Neo4jQueryAdapter) -> None:
    """find_entity returns matching entities."""
    node = _FakeRecord(
        {
            "id": "e1",
            "name": "MCP",
            "type": "mcp",
            "description": "Model Context Protocol",
            "source_page": 10,
        }
    )
    session = _make_session([_FakeRecord({"n": node})])
    adapter._driver = _FakeDriver(session)

    result = await adapter.find_entity("MCP", None)

    assert len(result) == 1
    assert result[0].entity.name == "MCP"
    assert result[0].entity.type == "mcp"
    query, params = session.queries[0]
    assert "MATCH (n:Entity {name: $name})" in query
    assert params["name"] == "MCP"
    assert params["type"] is None


async def test_find_entity_with_type_filter(adapter: Neo4jQueryAdapter) -> None:
    """find_entity forwards the entity_type filter to Cypher."""
    node = _FakeRecord(
        {"id": "e2", "name": "Agent", "type": "agent", "description": "", "source_page": None}
    )
    session = _make_session([_FakeRecord({"n": node})])
    adapter._driver = _FakeDriver(session)

    await adapter.find_entity("Agent", "agent")

    query, params = session.queries[0]
    assert "WHERE $type IS NULL OR n.type = $type" in query
    assert params["type"] == "agent"


async def test_find_entity_without_type_does_not_filter(adapter: Neo4jQueryAdapter) -> None:
    """find_entity with entity_type=None still includes the IS NULL guard."""
    node = _FakeRecord(
        {"id": "e3", "name": "Homonym", "type": "concept", "description": "", "source_page": None}
    )
    session = _make_session([_FakeRecord({"n": node})])
    adapter._driver = _FakeDriver(session)

    await adapter.find_entity("Homonym", None)

    query, params = session.queries[0]
    assert "WHERE $type IS NULL OR n.type = $type" in query
    assert params["type"] is None


async def test_find_entity_no_results_returns_empty_list(adapter: Neo4jQueryAdapter) -> None:
    """find_entity returns an empty list when nothing matches."""
    session = _make_session([])
    adapter._driver = _FakeDriver(session)

    result = await adapter.find_entity("missing", None)

    assert result == []


async def test_find_entities_batch_with_200_ids(adapter: Neo4jQueryAdapter) -> None:
    """Batch lookup issues a single UNWIND query."""
    ids = [f"id_{i}" for i in range(200)]
    node = _FakeRecord(
        {
            "id": "id_0",
            "name": "Entity 0",
            "type": "concept",
            "description": "",
            "source_page": None,
        }
    )
    session = _make_session([_FakeRecord({"n": node})])
    adapter._driver = _FakeDriver(session)

    result = await adapter.find_entities_batch(ids)

    assert len(result) == 1
    query, params = session.queries[0]
    assert "UNWIND $ids AS id" in query
    assert "MATCH (n:Entity {id: id})" in query
    assert params["ids"] == ids


async def test_find_entities_batch_returns_entity_with_context(adapter: Neo4jQueryAdapter) -> None:
    """Batch lookup maps Neo4j nodes to EntityWithContext."""
    node = _FakeRecord(
        {"id": "e1", "name": "MCP", "type": "mcp", "description": "desc", "source_page": 5}
    )
    session = _make_session([_FakeRecord({"n": node})])
    adapter._driver = _FakeDriver(session)

    result = await adapter.find_entities_batch(["e1"])

    assert len(result) == 1
    assert result[0] == EntityWithContext(
        entity=Entity(id="e1", name="MCP", type="mcp", description="desc", source_page=5)
    )
    assert result[0].status is None
    assert result[0].confidence is None
    assert result[0].source is None


async def test_node_to_entity_mapping(adapter: Neo4jQueryAdapter) -> None:
    """_node_to_entity builds EntityWithContext with defaults for Fase 08 fields."""
    node = _FakeRecord(
        {"id": "e1", "name": "MCP", "type": "mcp", "description": "desc", "source_page": 5}
    )

    entity_with_context = adapter._node_to_entity(node)

    assert entity_with_context.entity == Entity(
        id="e1", name="MCP", type="mcp", description="desc", source_page=5
    )
    assert entity_with_context.status is None
    assert entity_with_context.confidence is None
    assert entity_with_context.source is None


# ── T-06.7: traverse_relationships ───────────────────────────────────────────


def _node(data: dict[str, Any]) -> _FakeRecord:
    """Shorthand for a fake node record."""
    return _FakeRecord(data)


async def test_traverse_depth_one_returns_connected_entities(adapter: Neo4jQueryAdapter) -> None:
    """Depth 1 traversal returns start and target entities plus the relationship."""
    start = _node({"id": "s", "name": "Source", "type": "concept"})
    end = _node({"id": "t", "name": "Target", "type": "concept"})
    rel = _FakeRelationship(start, end, "requires")
    session = _make_session([_FakeRecord({"start": start, "end": end, "rels": [rel]})])
    adapter._driver = _FakeDriver(session)

    entities, relationships = await adapter.traverse_relationships("s", None, 1)

    assert len(entities) == 2
    assert {e.entity.id for e in entities} == {"s", "t"}
    assert len(relationships) == 1
    assert relationships[0].source_entity_id == "s"
    assert relationships[0].target_entity_id == "t"
    assert relationships[0].type == "requires"


async def test_traverse_depth_two(adapter: Neo4jQueryAdapter) -> None:
    """Depth 2 traversal can span two hops in a single record."""
    a = _node({"id": "a", "name": "A", "type": "concept"})
    b = _node({"id": "b", "name": "B", "type": "concept"})
    c = _node({"id": "c", "name": "C", "type": "concept"})
    rel_ab = _FakeRelationship(a, b, "requires")
    rel_bc = _FakeRelationship(b, c, "enables")
    session = _make_session([_FakeRecord({"start": a, "end": c, "rels": [rel_ab, rel_bc]})])
    adapter._driver = _FakeDriver(session)

    entities, relationships = await adapter.traverse_relationships("a", None, 2)

    query, params = session.queries[0]
    assert "[:RELATED*1..2]" in query
    assert {e.entity.id for e in entities} == {"a", "b", "c"}
    assert len(relationships) == 2


async def test_traverse_depth_three(adapter: Neo4jQueryAdapter) -> None:
    """Depth 3 traversal uses the maximum allowed range."""
    a = _node({"id": "a", "name": "A", "type": "concept"})
    d = _node({"id": "d", "name": "D", "type": "concept"})
    session = _make_session([_FakeRecord({"start": a, "end": d, "rels": []})])
    adapter._driver = _FakeDriver(session)

    await adapter.traverse_relationships("a", None, 3)

    query, _ = session.queries[0]
    assert "[:RELATED*1..3]" in query


async def test_traverse_depth_zero_returns_only_start_entity(adapter: Neo4jQueryAdapter) -> None:
    """AC-06.17: depth=0 uses a dedicated query with no relationship expansion."""
    start = _node({"id": "s", "name": "Source", "type": "concept"})
    session = _make_session([_FakeRecord({"start": start})])
    adapter._driver = _FakeDriver(session)

    entities, relationships = await adapter.traverse_relationships("s", None, 0)

    query, params = session.queries[0]
    assert "MATCH (start:Entity {id: $source_id}) RETURN start" in query
    assert "-[:RELATED" not in query
    assert len(entities) == 1
    assert entities[0].entity.id == "s"
    assert relationships == []


async def test_traverse_depth_five_is_clamped_to_three(adapter: Neo4jQueryAdapter) -> None:
    """AC-06.9: depths greater than 3 are clamped to the maximum of 3."""
    start = _node({"id": "s", "name": "Source", "type": "concept"})
    end = _node({"id": "t", "name": "Target", "type": "concept"})
    session = _make_session([_FakeRecord({"start": start, "end": end, "rels": []})])
    adapter._driver = _FakeDriver(session)

    await adapter.traverse_relationships("s", None, 5)

    query, _ = session.queries[0]
    assert "[:RELATED*1..3]" in query


async def test_traverse_depth_negative_is_clamped_to_zero(adapter: Neo4jQueryAdapter) -> None:
    """Negative depth is clamped to 0 and uses the start-only query."""
    start = _node({"id": "s", "name": "Source", "type": "concept"})
    session = _make_session([_FakeRecord({"start": start})])
    adapter._driver = _FakeDriver(session)

    await adapter.traverse_relationships("s", None, -1)

    query, _ = session.queries[0]
    assert "MATCH (start:Entity {id: $source_id}) RETURN start" in query


async def test_traverse_with_rel_type_filter(adapter: Neo4jQueryAdapter) -> None:
    """Traversal forwards the relationship type filter."""
    start = _node({"id": "s", "name": "Source", "type": "concept"})
    end = _node({"id": "t", "name": "Target", "type": "concept"})
    session = _make_session([_FakeRecord({"start": start, "end": end, "rels": []})])
    adapter._driver = _FakeDriver(session)

    await adapter.traverse_relationships("s", "requires", 1)

    query, params = session.queries[0]
    assert "ALL(r IN relationships(p) WHERE r.type = $rel_type)" in query
    assert params["rel_type"] == "requires"


async def test_traverse_without_rel_type_allows_all_types(adapter: Neo4jQueryAdapter) -> None:
    """Traversal with rel_type=None omits the relationship filter."""
    start = _node({"id": "s", "name": "Source", "type": "concept"})
    end = _node({"id": "t", "name": "Target", "type": "concept"})
    session = _make_session([_FakeRecord({"start": start, "end": end, "rels": []})])
    adapter._driver = _FakeDriver(session)

    await adapter.traverse_relationships("s", None, 1)

    query, params = session.queries[0]
    assert "WHERE $rel_type IS NULL OR" in query
    assert params["rel_type"] is None


async def test_traverse_respects_limit_100(adapter: Neo4jQueryAdapter) -> None:
    """Traversal Cypher includes a hard LIMIT 100."""
    start = _node({"id": "s", "name": "Source", "type": "concept"})
    end = _node({"id": "t", "name": "Target", "type": "concept"})
    session = _make_session([_FakeRecord({"start": start, "end": end, "rels": []})])
    adapter._driver = _FakeDriver(session)

    await adapter.traverse_relationships("s", None, 1)

    query, _ = session.queries[0]
    assert "LIMIT 100" in query


async def test_traverse_raises_query_timeout(adapter: Neo4jQueryAdapter) -> None:
    """A TimeoutError from the session is surfaced as QueryTimeoutError."""
    session = _FakeSession(raise_exc=TimeoutError("neo4j timeout"))
    adapter._driver = _FakeDriver(session)

    with pytest.raises(QueryTimeoutError):
        await adapter.traverse_relationships("s", None, 1)


# ── T-06.8: find_path + search_chunks + count_entities + list_entities ───────


class _FakePath:
    """Mimics a Neo4j Path object."""

    def __init__(
        self,
        nodes: list[_FakeRecord],
        relationships: list[_FakeRelationship],
    ) -> None:
        self.nodes = nodes
        self.relationships = relationships


async def test_find_path_returns_graph_paths(adapter: Neo4jQueryAdapter) -> None:
    """find_path maps a Neo4j shortestPath to a GraphPath list."""
    a = _node({"id": "a", "name": "A", "type": "concept"})
    b = _node({"id": "b", "name": "B", "type": "concept"})
    rel = _FakeRelationship(a, b, "requires")
    path = _FakePath(nodes=[a, b], relationships=[rel])
    session = _make_session([_FakeRecord({"p": path})])
    adapter._driver = _FakeDriver(session)

    result = await adapter.find_path("a", "b", 3)

    assert len(result) == 1
    graph_path = result[0]
    assert [n.id for n in graph_path.nodes] == ["a", "b"]
    assert len(graph_path.relationships) == 1
    assert graph_path.relationships[0].source_entity_id == "a"
    assert graph_path.relationships[0].target_entity_id == "b"
    query, params = session.queries[0]
    assert "shortestPath" in query
    assert params["start_id"] == "a"
    assert params["end_id"] == "b"


async def test_find_path_no_path_returns_empty_list(adapter: Neo4jQueryAdapter) -> None:
    """find_path returns an empty list when Neo4j finds no path."""
    session = _make_session([])
    adapter._driver = _FakeDriver(session)

    result = await adapter.find_path("a", "z", 3)

    assert result == []


async def test_search_chunks_uses_fulltext_index(adapter: Neo4jQueryAdapter) -> None:
    """AC-06.6: chunk search uses db.fulltext.queryNodes, not CONTAINS."""
    session = _make_session([])
    adapter._driver = _FakeDriver(session)

    await adapter.search_chunks("dependency injection", 10)

    query, params = session.queries[0]
    assert "CALL db.fulltext.queryNodes" in query
    assert "CONTAINS" not in query
    assert params["query"] == "dependency injection"
    assert params["limit"] == 10


async def test_search_chunks_returns_scored_results(adapter: Neo4jQueryAdapter) -> None:
    """search_chunks returns dicts with text, page range, and Lucene score."""
    node = _node(
        {
            "text": "dependency injection example",
            "page_start": 10,
            "page_end": 11,
        }
    )
    session = _make_session([_FakeRecord({"node": node, "score": 0.95})])
    adapter._driver = _FakeDriver(session)

    result = await adapter.search_chunks("dependency injection", 5)

    assert result == [
        {
            "text": "dependency injection example",
            "page_start": 10,
            "page_end": 11,
            "score": 0.95,
        }
    ]


async def test_count_entities_without_type_filter(adapter: Neo4jQueryAdapter) -> None:
    """count_entities returns the total when no type is supplied."""
    session = _make_session([_FakeRecord({"count": 42})])
    adapter._driver = _FakeDriver(session)

    result = await adapter.count_entities(None)

    assert result == 42
    query, params = session.queries[0]
    assert "RETURN count(n) AS count" in query
    assert params["type"] is None


async def test_count_entities_with_type_filter(adapter: Neo4jQueryAdapter) -> None:
    """count_entities forwards the type filter to Cypher."""
    session = _make_session([_FakeRecord({"count": 7})])
    adapter._driver = _FakeDriver(session)

    result = await adapter.count_entities("agent")

    assert result == 7
    query, params = session.queries[0]
    assert "WHERE $type IS NULL OR n.type = $type" in query
    assert params["type"] == "agent"


async def test_list_entities_first_page(adapter: Neo4jQueryAdapter) -> None:
    """AC-06.7: list_entities uses cursor pagination from cursor=0."""
    node = _node({"id": "e1", "name": "Entity 1", "type": "concept"})
    session = _make_session([_FakeRecord({"n": node, "internal_id": 101})])
    adapter._driver = _FakeDriver(session)

    entities, next_cursor = await adapter.list_entities(0, 50)

    assert len(entities) == 1
    assert entities[0].entity.id == "e1"
    assert next_cursor == 101
    query, params = session.queries[0]
    assert "WHERE id(n) > $cursor" in query
    assert "ORDER BY id(n)" in query
    assert "SKIP" not in query
    assert params["cursor"] == 0
    assert params["page_size"] == 50


async def test_list_entities_second_page(adapter: Neo4jQueryAdapter) -> None:
    """Cursor pagination advances using the last internal Neo4j id."""
    node = _node({"id": "e2", "name": "Entity 2", "type": "concept"})
    session = _make_session([_FakeRecord({"n": node, "internal_id": 202})])
    adapter._driver = _FakeDriver(session)

    entities, next_cursor = await adapter.list_entities(101, 50)

    assert next_cursor == 202
    query, params = session.queries[0]
    assert params["cursor"] == 101
    assert "SKIP" not in query


# ── T-06.9: ensure_indexes ───────────────────────────────────────────────────


async def test_ensure_indexes_executes_five_create_statements(adapter: Neo4jQueryAdapter) -> None:
    """ensure_indexes runs one CREATE statement per index."""
    session = _make_session([])
    adapter._driver = _FakeDriver(session)

    await adapter.ensure_indexes()

    assert len(session.queries) == 5


async def test_ensure_indexes_is_idempotent(adapter: Neo4jQueryAdapter) -> None:
    """Every index statement uses IF NOT EXISTS."""
    session = _make_session([])
    adapter._driver = _FakeDriver(session)

    await adapter.ensure_indexes()

    for query, _ in session.queries:
        assert "IF NOT EXISTS" in query


async def test_ensure_indexes_creates_expected_indexes(adapter: Neo4jQueryAdapter) -> None:
    """The five expected indexes are created with correct names."""
    session = _make_session([])
    adapter._driver = _FakeDriver(session)

    await adapter.ensure_indexes()

    queries = [q for q, _ in session.queries]
    assert any("entity_name" in q for q in queries)
    assert any("entity_type" in q for q in queries)
    assert any("entity_id" in q for q in queries)
    assert any("rel_type" in q for q in queries)
    assert any("chunk_text_index" in q for q in queries)


async def test_ensure_indexes_fulltext_uses_on_each(adapter: Neo4jQueryAdapter) -> None:
    """Full-text index creation uses ON EACH syntax."""
    session = _make_session([])
    adapter._driver = _FakeDriver(session)

    await adapter.ensure_indexes()

    fulltext_query = next(
        q for q, _ in session.queries if "FULLTEXT INDEX chunk_text_index" in q
    )
    assert "ON EACH [n.text]" in fulltext_query
