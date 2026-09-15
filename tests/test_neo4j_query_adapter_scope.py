"""Scope parameter binding tests for Neo4jQueryAdapter (T-B.2).

These tests verify that a validated ``ScopeContext`` is translated into
parameterized Cypher predicates without string concatenation of user input.
"""

from __future__ import annotations

from typing import Any

import pytest

from book_graph_rag.config import Settings
from book_graph_rag.domain.mcp_security import ScopeContext
from book_graph_rag.domain.namespaces import SourceNamespace
from book_graph_rag.infrastructure.neo4j_query_adapter import Neo4jQueryAdapter


class _FakeResult:
    """Async iterable that yields no records."""

    def __aiter__(self) -> _FakeResult:
        return self

    async def __anext__(self) -> Any:
        raise StopAsyncIteration


class _FakeTx:
    """Managed-transaction stand-in delegating ``run`` back to the session."""

    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def run(
        self, query: str, parameters: dict[str, Any] | None = None
    ) -> _FakeResult:
        return await self._session.run(query, parameters)


class _FakeSession:
    """Records the last query and parameters passed to ``run``."""

    def __init__(self) -> None:
        self.queries: list[tuple[str, dict[str, Any]]] = []

    async def run(
        self, query: str, parameters: dict[str, Any] | None = None
    ) -> _FakeResult:
        self.queries.append((query, parameters or {}))
        return _FakeResult()

    async def execute_read(self, tx_func: Any, *args: Any, **kwargs: Any) -> Any:
        return await tx_func(_FakeTx(self), *args, **kwargs)

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass


class _FakeDriver:
    """Driver that yields a recording session."""

    def __init__(self) -> None:
        self._session = _FakeSession()

    def session(self, **config: Any) -> _FakeSession:
        return self._session


class _TestableAdapter(Neo4jQueryAdapter):
    """Neo4jQueryAdapter with a fake driver for unit testing."""

    def __init__(self, driver: _FakeDriver, settings: Settings) -> None:  # noqa: D417
        self._driver = driver
        self._settings = settings


@pytest.fixture
def driver() -> _FakeDriver:
    return _FakeDriver()


@pytest.fixture
def adapter(driver: _FakeDriver) -> _TestableAdapter:
    settings = Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
        }
    )
    return _TestableAdapter(driver, settings)


@pytest.fixture
def scope() -> ScopeContext:
    return ScopeContext(
        source=SourceNamespace(corpus="book", source="default"),
        book_ids=("book-1", "book-2"),
        entity_types=("concept", "agent"),
        relationship_types=("requires", "enables"),
    )


async def test_search_chunks_binds_book_ids(adapter: _TestableAdapter, scope: ScopeContext) -> None:
    """Chunk search applies ``Chunk.book_id`` scope as a bound parameter."""
    await adapter.search_chunks("patterns", 10, scope=scope)

    query, params = adapter._driver._session.queries[-1]
    assert "node.book_id IN $scope_book_ids" in query
    assert params["scope_book_ids"] == ["book-1", "book-2"]


async def test_search_chunks_without_scope_omits_book_filter(
    adapter: _TestableAdapter,
) -> None:
    """When no scope is supplied, chunk search uses the legacy query shape."""
    await adapter.search_chunks("patterns", 10)

    query, params = adapter._driver._session.queries[-1]
    assert "scope_book_ids" not in query
    assert "scope_book_ids" not in params


async def test_count_entities_binds_entity_types(
    adapter: _TestableAdapter, scope: ScopeContext
) -> None:
    """Entity count applies ``entity_types`` scope as a bound parameter."""
    await adapter.count_entities(None, scope=scope)

    query, params = adapter._driver._session.queries[-1]
    assert "n.type IN $scope_entity_types" in query
    assert params["scope_entity_types"] == ["agent", "concept"]


async def test_count_entities_without_scope_omits_type_filter(
    adapter: _TestableAdapter,
) -> None:
    """When no scope is supplied, count uses only the legacy type filter."""
    await adapter.count_entities("agent")

    query, params = adapter._driver._session.queries[-1]
    assert "scope_entity_types" not in query
    assert params == {"type": "agent"}


async def test_list_entities_binds_entity_types(
    adapter: _TestableAdapter, scope: ScopeContext
) -> None:
    """Entity list applies ``entity_types`` scope as a bound parameter."""
    await adapter.list_entities(0, 50, scope=scope)

    query, params = adapter._driver._session.queries[-1]
    assert "n.type IN $scope_entity_types" in query
    assert params["scope_entity_types"] == ["agent", "concept"]


async def test_find_entity_binds_entity_types(
    adapter: _TestableAdapter, scope: ScopeContext
) -> None:
    """Entity lookup tiers apply ``entity_types`` scope as a bound parameter."""
    await adapter.find_entity("MCP", None, scope=scope)

    queries = [q for q, _ in adapter._driver._session.queries]
    # All tier queries include the parameterized entity-type predicate.
    assert all("n.type IN $scope_entity_types" in q for q in queries[:3])
    params = adapter._driver._session.queries[0][1]
    assert params["scope_entity_types"] == ["agent", "concept"]


async def test_traverse_relationships_binds_relationship_types(
    adapter: _TestableAdapter, scope: ScopeContext
) -> None:
    """Traversal applies ``relationship_types`` scope as a bound parameter."""
    await adapter.traverse_relationships("e1", None, 1, scope=scope)

    query, params = adapter._driver._session.queries[-1]
    assert "r.type IN $scope_rel_types" in query
    assert params["scope_rel_types"] == ["enables", "requires"]


async def test_traverse_relationships_without_scope_omits_rel_filter(
    adapter: _TestableAdapter,
) -> None:
    """When no scope is supplied, traversal uses the legacy relationship filter."""
    await adapter.traverse_relationships("e1", None, 1)

    query, params = adapter._driver._session.queries[-1]
    assert "scope_rel_types" not in query
    assert params == {"source_id": "e1", "rel_type": None}


async def test_scope_parameters_are_bound_not_concatenated(
    adapter: _TestableAdapter, scope: ScopeContext
) -> None:
    """User-provided scope values appear only in the parameter map, not the query text."""
    await adapter.count_entities(None, scope=scope)

    query, params = adapter._driver._session.queries[-1]
    for value in ("concept", "agent", "book-1"):
        assert value not in query
    assert params["scope_entity_types"] == ["agent", "concept"]
    assert params["scope_book_ids"] == ["book-1", "book-2"]


