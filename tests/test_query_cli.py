"""Tests for the book-graph-rag query CLI command."""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from book_graph_rag.domain.models import (
    BatchSizeExceededError,
    Entity,
    EntityWithContext,
    GraphPath,
    GraphQueryResult,
    QueryMetadata,
    QueryTimeoutError,
    Relationship,
    UnsupportedQueryTypeError,
)
from book_graph_rag.main import cli


class _FakeSettings:
    def __init__(self) -> None:
        self.neo4j_uri = "bolt://localhost:7687"
        self.neo4j_user = "neo4j"
        self.neo4j_password = "fake-password"  # pragma: allowlist secret


class _FakeQueryAdapter:
    def __init__(self, settings: object) -> None:
        self.settings = settings

    async def close(self) -> None:
        pass


class _FakeUseCase:
    def __init__(self, port: object) -> None:
        self.port = port
        self.executed: list[Any] = []
        self.result = GraphQueryResult(
            entities=[],
            relationships=[],
            paths=[],
            chunks=[],
            metadata=QueryMetadata(total_count=0, query_ms=1.0),
        )
        self.raises: Exception | None = None

    async def execute(self, query: Any) -> GraphQueryResult:
        self.executed.append(query)
        if self.raises is not None:
            raise self.raises
        return self.result


@pytest.fixture
def fake_use_case(monkeypatch: pytest.MonkeyPatch) -> _FakeUseCase:
    use_case = _FakeUseCase(port=None)
    monkeypatch.setattr("book_graph_rag.main.Neo4jQueryAdapter", _FakeQueryAdapter)
    monkeypatch.setattr("book_graph_rag.main.QueryKnowledgeGraphUseCase", lambda port: use_case)
    monkeypatch.setattr("book_graph_rag.main.Settings", _FakeSettings)
    return use_case


def test_query_entity_outputs_json_with_entities(fake_use_case: _FakeUseCase) -> None:
    """AC-06.11: entity query returns valid JSON with an entities list."""
    entity = Entity(id="e1", name="MCP", type="mcp")
    fake_use_case.result = GraphQueryResult(
        entities=[EntityWithContext(entity=entity)],
        metadata=QueryMetadata(total_count=1, query_ms=2.0),
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["query", "--type", "entity", "--query", '{"name": "MCP"}'])

    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed["entities"][0]["entity"]["name"] == "MCP"
    assert parsed["metadata"]["total_count"] == 1


def test_query_relation_outputs_json_with_relationships(fake_use_case: _FakeUseCase) -> None:
    """AC-06.11: relation query returns valid JSON with a relationships list."""
    rel = Relationship(source_entity_id="a", target_entity_id="b", type="requires")
    fake_use_case.result = GraphQueryResult(
        entities=[],
        relationships=[rel],
        metadata=QueryMetadata(total_count=0, query_ms=1.5),
    )

    runner = CliRunner()
    result = runner.invoke(
        cli, ["query", "--type", "relation", "--query", '{"source_id": "a", "depth": 2}']
    )

    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed["relationships"][0]["type"] == "requires"


def test_query_path_outputs_json_with_paths(fake_use_case: _FakeUseCase) -> None:
    """AC-06.11: path query returns valid JSON with a paths list."""
    node_a = Entity(id="a", name="A", type="concept")
    node_b = Entity(id="b", name="B", type="concept")
    fake_use_case.result = GraphQueryResult(
        paths=[GraphPath(nodes=[node_a, node_b], relationships=[])],
        metadata=QueryMetadata(total_count=1, query_ms=3.0),
    )

    runner = CliRunner()
    result = runner.invoke(
        cli, ["query", "--type", "path", "--query", '{"start_id": "a", "end_id": "b"}']
    )

    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed["paths"][0]["nodes"][0]["id"] == "a"


def test_query_batch_entity_outputs_json_with_entities(fake_use_case: _FakeUseCase) -> None:
    """AC-06.11: batch_entity query returns valid JSON with an entities list."""
    entity = Entity(id="e1", name="Agent", type="agent")
    fake_use_case.result = GraphQueryResult(
        entities=[EntityWithContext(entity=entity)],
        metadata=QueryMetadata(total_count=1, query_ms=1.0),
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["query", "--type", "batch_entity", "--query", '{"ids": ["mcp", "agent", "tool"]}'],
    )

    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed["entities"][0]["entity"]["type"] == "agent"


def test_query_invalid_json_exits_nonzero(fake_use_case: _FakeUseCase) -> None:
    """Invalid JSON input produces a non-zero exit code."""
    runner = CliRunner()
    result = runner.invoke(cli, ["query", "--type", "entity", "--query", "not-json"])

    assert result.exit_code != 0


def test_query_builds_entity_query_object(fake_use_case: _FakeUseCase) -> None:
    """The CLI parses the JSON and builds the correct EntityQuery."""
    runner = CliRunner()
    runner.invoke(cli, ["query", "--type", "entity", "--query", '{"name": "MCP"}'])

    assert len(fake_use_case.executed) == 1
    query = fake_use_case.executed[0]
    assert query.type == "entity"
    assert query.name == "MCP"
    assert query.entity_type is None


def test_query_builds_relation_query_object(fake_use_case: _FakeUseCase) -> None:
    """The CLI parses the JSON and builds the correct RelationQuery."""
    runner = CliRunner()
    runner.invoke(
        cli, ["query", "--type", "relation", "--query", '{"source_id": "a", "depth": 2}']
    )

    assert len(fake_use_case.executed) == 1
    query = fake_use_case.executed[0]
    assert query.type == "relation"
    assert query.source_id == "a"
    assert query.depth == 2


def test_query_builds_path_query_object(fake_use_case: _FakeUseCase) -> None:
    """The CLI parses the JSON and builds the correct PathQuery."""
    runner = CliRunner()
    runner.invoke(
        cli, ["query", "--type", "path", "--query", '{"start_id": "a", "end_id": "b"}']
    )

    assert len(fake_use_case.executed) == 1
    query = fake_use_case.executed[0]
    assert query.type == "path"
    assert query.start_id == "a"
    assert query.end_id == "b"


def test_query_builds_batch_entity_query_object(fake_use_case: _FakeUseCase) -> None:
    """The CLI parses the JSON and builds the correct BatchEntityQuery."""
    runner = CliRunner()
    runner.invoke(
        cli,
        ["query", "--type", "batch_entity", "--query", '{"ids": ["mcp", "agent"]}'],
    )

    assert len(fake_use_case.executed) == 1
    query = fake_use_case.executed[0]
    assert query.type == "batch_entity"
    assert query.ids == ["mcp", "agent"]


def test_query_unsupported_type_error_exits_nonzero(fake_use_case: _FakeUseCase) -> None:
    """UnsupportedQueryTypeError from the use case results in a non-zero exit."""
    fake_use_case.raises = UnsupportedQueryTypeError("similarity")

    runner = CliRunner()
    result = runner.invoke(cli, ["query", "--type", "entity", "--query", '{"name": "x"}'])

    assert result.exit_code != 0
    assert "similarity" in result.output


def test_query_batch_size_exceeded_exits_nonzero(fake_use_case: _FakeUseCase) -> None:
    """BatchSizeExceededError from the use case results in a non-zero exit."""
    fake_use_case.raises = BatchSizeExceededError(limit=200, received=201)

    runner = CliRunner()
    result = runner.invoke(cli, ["query", "--type", "entity", "--query", '{"name": "x"}'])

    assert result.exit_code != 0


def test_query_timeout_error_exits_nonzero(fake_use_case: _FakeUseCase) -> None:
    """QueryTimeoutError from the use case results in a non-zero exit."""
    fake_use_case.raises = QueryTimeoutError("neo4j timeout")

    runner = CliRunner()
    result = runner.invoke(cli, ["query", "--type", "entity", "--query", '{"name": "x"}'])

    assert result.exit_code != 0
