"""Tests for McpServerAdapter (REQ-07.1, REQ-07.2)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp import FastMCP

from book_graph_rag.domain.models import (
    Entity,
    EntityType,
    EntityWithContext,
    QueryLogEntry,
    Relationship,
    RelationshipType,
)
from book_graph_rag.infrastructure.mcp.mcp_server_adapter import McpServerAdapter
from book_graph_rag.ports.graph_query_port import GraphQueryPort
from book_graph_rag.ports.query_logger_port import QueryLoggerPort


class _FakeGraphQueryPort(GraphQueryPort):
    """In-memory GraphQueryPort with configurable results and optional failures."""

    def __init__(
        self,
        find_entity_result: list[EntityWithContext] | None = None,
        traverse_result: tuple[list[EntityWithContext], list[Relationship]] | None = None,
        search_chunks_result: list[dict[str, Any]] | None = None,
        count_result: int = 0,
        list_entities_result: tuple[list[EntityWithContext], int] | None = None,
        raise_on: str | None = None,
    ) -> None:
        self.find_entity_result = find_entity_result or []
        self.traverse_result = traverse_result or ([], [])
        self.search_chunks_result = search_chunks_result or []
        self.count_result = count_result
        self.list_entities_result = list_entities_result or ([], 0)
        self.raise_on = raise_on
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def find_entity(
        self, name: str, entity_type: EntityType | None
    ) -> list[EntityWithContext]:
        self.calls.append(("find_entity", {"name": name, "entity_type": entity_type}))
        if self.raise_on == "find_entity":
            raise TimeoutError("neo4j timeout")
        return self.find_entity_result

    async def find_entities_batch(self, ids: list[str]) -> list[EntityWithContext]:
        self.calls.append(("find_entities_batch", {"ids": ids}))
        return []

    async def traverse_relationships(
        self, source_id: str, rel_type: RelationshipType | None, depth: int
    ) -> tuple[list[EntityWithContext], list[Relationship]]:
        self.calls.append(
            (
                "traverse_relationships",
                {"source_id": source_id, "rel_type": rel_type, "depth": depth},
            )
        )
        if self.raise_on == "traverse_relationships":
            raise TimeoutError("neo4j timeout")
        return self.traverse_result

    async def find_path(self, start_id: str, end_id: str, max_depth: int) -> list[Any]:
        return []

    async def search_chunks(self, query: str, limit: int) -> list[dict[str, Any]]:
        self.calls.append(("search_chunks", {"query": query, "limit": limit}))
        if self.raise_on == "search_chunks":
            raise TimeoutError("neo4j timeout")
        return self.search_chunks_result

    async def count_entities(self, entity_type: str | None) -> int:
        self.calls.append(("count_entities", {"entity_type": entity_type}))
        if self.raise_on == "count_entities":
            raise TimeoutError("neo4j timeout")
        return self.count_result

    async def list_entities(
        self, cursor: int, page_size: int
    ) -> tuple[list[EntityWithContext], int]:
        self.calls.append(("list_entities", {"cursor": cursor, "page_size": page_size}))
        if self.raise_on == "list_entities":
            raise TimeoutError("neo4j timeout")
        return self.list_entities_result

    async def ensure_indexes(self) -> None:
        pass


class _FakeQueryLoggerPort(QueryLoggerPort):
    """Captures every QueryLogEntry passed to it."""

    def __init__(self) -> None:
        self.entries: list[QueryLogEntry] = []
        self.closed = False

    async def log_query(self, entry: QueryLogEntry) -> None:
        self.entries.append(entry)

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def graph_query_port() -> _FakeGraphQueryPort:
    return _FakeGraphQueryPort()


@pytest.fixture
def query_logger() -> _FakeQueryLoggerPort:
    return _FakeQueryLoggerPort()


@pytest.fixture
def adapter(
    graph_query_port: _FakeGraphQueryPort, query_logger: _FakeQueryLoggerPort
) -> McpServerAdapter:
    return McpServerAdapter(graph_query_port, query_logger)


def _entity(
    name: str, entity_id: str = "", entity_type: EntityType = "concept"
) -> EntityWithContext:
    return EntityWithContext(
        entity=Entity(id=entity_id or name.lower(), name=name, type=entity_type)
    )


def _relationship(
    source_id: str, target_id: str, rel_type: RelationshipType = "requires"
) -> Relationship:
    return Relationship(
        source_entity_id=source_id, target_entity_id=target_id, type=rel_type
    )


# ── create_server ────────────────────────────────────────────────────────────


async def test_create_server_registers_five_tools(adapter: McpServerAdapter) -> None:
    """create_server exposes exactly the 5 MCP tools defined by REQ-07.1."""
    server = adapter.create_server()
    tools = await server.list_tools()
    names = {tool.name for tool in tools}
    assert names == {
        "find_entity",
        "traverse_relationships",
        "search_chunks",
        "list_entities",
        "count_entities",
    }


# ── find_entity ──────────────────────────────────────────────────────────────


async def test_find_entity_returns_matching_entities(
    adapter: McpServerAdapter,
    graph_query_port: _FakeGraphQueryPort,
    query_logger: _FakeQueryLoggerPort,
) -> None:
    entity = _entity("MCP", entity_id="e1", entity_type="mcp")
    graph_query_port.find_entity_result = [entity]

    result = await adapter.find_entity("MCP", entity_type="mcp")

    assert result["entity_not_found"] is False
    assert len(result["entities"]) == 1
    assert result["entities"][0]["entity"]["name"] == "MCP"
    assert graph_query_port.calls == [
        ("find_entity", {"name": "MCP", "entity_type": "mcp"})
    ]
    assert len(query_logger.entries) == 1
    entry = query_logger.entries[0]
    assert entry.tool_name == "find_entity"
    assert entry.result_count == 1
    assert entry.entity_not_found is False
    assert entry.zero_results is False


async def test_find_entity_not_found_returns_flag(
    adapter: McpServerAdapter, query_logger: _FakeQueryLoggerPort
) -> None:
    result = await adapter.find_entity("Missing")

    assert result == {"entities": [], "entity_not_found": True}
    entry = query_logger.entries[0]
    assert entry.entity_not_found is True
    assert entry.zero_results is True
    assert entry.result_count == 0


async def test_find_entity_error_is_propagated_and_logged(
    adapter: McpServerAdapter,
    graph_query_port: _FakeGraphQueryPort,
    query_logger: _FakeQueryLoggerPort,
) -> None:
    graph_query_port.raise_on = "find_entity"

    with pytest.raises(TimeoutError, match="neo4j timeout"):
        await adapter.find_entity("MCP")

    entry = query_logger.entries[0]
    assert entry.tool_name == "find_entity"
    assert entry.error == "neo4j timeout"
    assert entry.zero_results is True
    assert entry.result_count == 0


# ── traverse_relationships ───────────────────────────────────────────────────


async def test_traverse_relationships_returns_entities_and_relationships(
    adapter: McpServerAdapter,
    graph_query_port: _FakeGraphQueryPort,
    query_logger: _FakeQueryLoggerPort,
) -> None:
    source = _entity("Source", entity_id="s", entity_type="concept")
    target = _entity("Target", entity_id="t", entity_type="concept")
    rel = _relationship("s", "t", "requires")
    graph_query_port.traverse_result = ([source, target], [rel])

    result = await adapter.traverse_relationships("s", depth=1)

    assert len(result["entities"]) == 2
    assert len(result["relationships"]) == 1
    assert result["relationships"][0]["type"] == "requires"
    assert graph_query_port.calls == [
        ("traverse_relationships", {"source_id": "s", "rel_type": None, "depth": 1})
    ]
    entry = query_logger.entries[0]
    assert entry.tool_name == "traverse_relationships"
    assert entry.result_count == 2


async def test_traverse_relationships_depth_is_clamped(
    adapter: McpServerAdapter, graph_query_port: _FakeGraphQueryPort
) -> None:
    await adapter.traverse_relationships("s", depth=5)

    _, params = graph_query_port.calls[0]
    assert params["depth"] == 3


async def test_traverse_relationships_negative_depth_is_clamped(
    adapter: McpServerAdapter, graph_query_port: _FakeGraphQueryPort
) -> None:
    await adapter.traverse_relationships("s", depth=-1)

    _, params = graph_query_port.calls[0]
    assert params["depth"] == 0


async def test_traverse_relationships_error_is_propagated(
    adapter: McpServerAdapter, graph_query_port: _FakeGraphQueryPort
) -> None:
    graph_query_port.raise_on = "traverse_relationships"

    with pytest.raises(TimeoutError, match="neo4j timeout"):
        await adapter.traverse_relationships("s")


# ── search_chunks ────────────────────────────────────────────────────────────


async def test_search_chunks_returns_chunks(
    adapter: McpServerAdapter,
    graph_query_port: _FakeGraphQueryPort,
    query_logger: _FakeQueryLoggerPort,
) -> None:
    chunks = [{"text": "chunk one", "score": 0.9}, {"text": "chunk two", "score": 0.8}]
    graph_query_port.search_chunks_result = chunks

    result = await adapter.search_chunks("MCP", limit=5)

    assert result["chunks"] == chunks
    assert graph_query_port.calls == [
        ("search_chunks", {"query": "MCP", "limit": 5})
    ]
    entry = query_logger.entries[0]
    assert entry.tool_name == "search_chunks"
    assert entry.result_count == 2
    assert entry.zero_results is False


async def test_search_chunks_zero_results_logs_flag(
    adapter: McpServerAdapter, query_logger: _FakeQueryLoggerPort
) -> None:
    result = await adapter.search_chunks("unknown")

    assert result == {"chunks": []}
    entry = query_logger.entries[0]
    assert entry.zero_results is True
    assert entry.result_count == 0


async def test_search_chunks_error_is_propagated(
    adapter: McpServerAdapter, graph_query_port: _FakeGraphQueryPort
) -> None:
    graph_query_port.raise_on = "search_chunks"

    with pytest.raises(TimeoutError, match="neo4j timeout"):
        await adapter.search_chunks("MCP")


# ── list_entities ────────────────────────────────────────────────────────────


async def test_list_entities_returns_paginated_entities(
    adapter: McpServerAdapter,
    graph_query_port: _FakeGraphQueryPort,
    query_logger: _FakeQueryLoggerPort,
) -> None:
    entity = _entity("Entity 1", entity_id="e1")
    graph_query_port.list_entities_result = ([entity], 101)

    result = await adapter.list_entities(cursor=0, page_size=50)

    assert len(result["entities"]) == 1
    assert result["next_cursor"] == 101
    assert graph_query_port.calls == [
        ("list_entities", {"cursor": 0, "page_size": 50})
    ]
    entry = query_logger.entries[0]
    assert entry.tool_name == "list_entities"
    assert entry.result_count == 1


async def test_list_entities_error_is_propagated(
    adapter: McpServerAdapter, graph_query_port: _FakeGraphQueryPort
) -> None:
    graph_query_port.raise_on = "list_entities"

    with pytest.raises(TimeoutError, match="neo4j timeout"):
        await adapter.list_entities()


# ── count_entities ───────────────────────────────────────────────────────────


async def test_count_entities_returns_count(
    adapter: McpServerAdapter,
    graph_query_port: _FakeGraphQueryPort,
    query_logger: _FakeQueryLoggerPort,
) -> None:
    graph_query_port.count_result = 42

    result = await adapter.count_entities(entity_type="agent")

    assert result == {"count": 42}
    assert graph_query_port.calls == [
        ("count_entities", {"entity_type": "agent"})
    ]
    entry = query_logger.entries[0]
    assert entry.tool_name == "count_entities"
    assert entry.result_count == 42
    assert entry.zero_results is False


async def test_count_entities_zero_logs_zero_results(
    adapter: McpServerAdapter, query_logger: _FakeQueryLoggerPort
) -> None:
    result = await adapter.count_entities()

    assert result == {"count": 0}
    entry = query_logger.entries[0]
    assert entry.zero_results is True


async def test_count_entities_error_is_propagated(
    adapter: McpServerAdapter, graph_query_port: _FakeGraphQueryPort
) -> None:
    graph_query_port.raise_on = "count_entities"

    with pytest.raises(TimeoutError, match="neo4j timeout"):
        await adapter.count_entities()


# ── logging ──────────────────────────────────────────────────────────────────


async def test_log_entry_contains_timestamp_and_duration(
    adapter: McpServerAdapter, query_logger: _FakeQueryLoggerPort
) -> None:
    before = datetime.now(tz=UTC)
    await adapter.count_entities()
    after = datetime.now(tz=UTC)

    entry = query_logger.entries[0]
    assert before <= entry.timestamp <= after
    assert entry.duration_ms >= 0.0


async def test_log_entry_query_params_match_tool_inputs(
    adapter: McpServerAdapter, query_logger: _FakeQueryLoggerPort
) -> None:
    await adapter.find_entity("MCP", entity_type="mcp")

    entry = query_logger.entries[0]
    assert entry.query_params == {"name": "MCP", "entity_type": "mcp"}


# ── run_sse ──────────────────────────────────────────────────────────────────


async def test_run_sse_starts_server(
    adapter: McpServerAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_sse creates a server and awaits the SSE run coroutine."""
    run_sse_async_mock = AsyncMock()
    monkeypatch.setattr(FastMCP, "run_sse_async", run_sse_async_mock)

    await adapter.run_sse()

    run_sse_async_mock.assert_awaited_once()


async def test_run_sse_uses_configured_host_and_port(
    adapter: McpServerAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_sse passes host and port to the underlying FastMCP server."""
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    original_create_server = adapter.create_server

    def fake_create_server(*args: Any, **kwargs: Any) -> FastMCP:
        calls.append((args, kwargs))
        return original_create_server(*args, **kwargs)

    monkeypatch.setattr(adapter, "create_server", fake_create_server)
    monkeypatch.setattr(FastMCP, "run_sse_async", AsyncMock())

    await adapter.run_sse(host="127.0.0.1", port=9000)

    assert len(calls) == 1
    assert calls[0] == ((), {"host": "127.0.0.1", "port": 9000})


async def test_create_server_uses_provided_host_and_port(adapter: McpServerAdapter) -> None:
    """create_server configures FastMCP with the supplied host and port."""
    server = adapter.create_server(host="127.0.0.1", port=9000)

    assert server.settings.host == "127.0.0.1"
    assert server.settings.port == 9000
