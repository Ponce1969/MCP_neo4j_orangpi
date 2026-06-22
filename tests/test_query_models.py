"""Tests for Fase 06 query domain models (AC-06.13, AC-06.14, AC-06.17)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from book_graph_rag.domain.models import (
    BatchSizeExceededError,
    Entity,
    EntityQuery,
    EntityWithContext,
    GraphPath,
    GraphQuery,
    GraphQueryResult,
    GraphQueryUnion,
    PathQuery,
    QueryMetadata,
    QueryTimeoutError,
    RelationQuery,
    Relationship,
    SimilarityQuery,
    UnsupportedQueryTypeError,
)


@pytest.mark.parametrize(
    ("raw", "expected_type"),
    [
        ({"type": "entity", "name": "MCP"}, EntityQuery),
        ({"type": "relation", "source_id": "e1"}, RelationQuery),
        ({"type": "path", "start_id": "a", "end_id": "b"}, PathQuery),
        ({"type": "similarity", "text": "prompt engineering"}, SimilarityQuery),
    ],
)
def test_graph_query_discriminated_union_parses_subtypes(
    raw: dict[str, Any], expected_type: type
) -> None:
    """The discriminated union routes each type value to the correct subtype."""
    adapter: TypeAdapter[Any] = TypeAdapter(GraphQueryUnion)
    parsed: GraphQuery = adapter.validate_python(raw)

    assert isinstance(parsed, expected_type)
    assert parsed.type == raw["type"]


def test_entity_query_defaults() -> None:
    """EntityQuery fills defaults and accepts optional entity_type."""
    query = EntityQuery(name="MCP")

    assert query.type == "entity"
    assert query.entity_type is None
    assert query.limit == 100


def test_entity_query_accepts_entity_type() -> None:
    """EntityQuery accepts a valid EntityType discriminator."""
    query = EntityQuery(name="Agent", entity_type="agent")

    assert query.entity_type == "agent"


def test_relation_query_defaults() -> None:
    """RelationQuery defaults depth to 1 and optional rel_type to None."""
    query = RelationQuery(source_id="e1")

    assert query.type == "relation"
    assert query.rel_type is None
    assert query.depth == 1


def test_relation_query_depth_zero_is_valid() -> None:
    """AC-06.17: depth=0 is a valid traversal request (start entity only)."""
    query = RelationQuery(source_id="e1", depth=0)

    assert query.depth == 0


def test_path_query_defaults() -> None:
    """PathQuery defaults max_depth to 3."""
    query = PathQuery(start_id="a", end_id="b")

    assert query.type == "path"
    assert query.max_depth == 3


def test_similarity_query_defaults() -> None:
    """SimilarityQuery defaults top_k to 10."""
    query = SimilarityQuery(text="semantic search")

    assert query.type == "similarity"
    assert query.top_k == 10


def test_invalid_query_type_raises_validation_error() -> None:
    """A discriminator value outside the union literals fails validation."""
    adapter: TypeAdapter[Any] = TypeAdapter(GraphQueryUnion)

    with pytest.raises(ValidationError):
        adapter.validate_python({"type": "unknown"})


def test_entity_with_context_optional_fields_default_to_none() -> None:
    """AC-06.13: status, confidence, and source default to None."""
    entity = Entity(id="e1", name="Agent", type="agent")
    wrapper = EntityWithContext(entity=entity)

    assert wrapper.status is None
    assert wrapper.confidence is None
    assert wrapper.source is None


def test_entity_with_context_can_set_fase_08_fields() -> None:
    """EntityWithContext preserves Fase 08 fields when provided."""
    entity = Entity(id="e1", name="Agent", type="agent")
    wrapper = EntityWithContext(
        entity=entity, status="confirmed", confidence=0.95, source="llm"
    )

    assert wrapper.status == "confirmed"
    assert wrapper.confidence == 0.95
    assert wrapper.source == "llm"


def test_query_metadata_timed_out_defaults_to_false() -> None:
    """QueryMetadata.timed_out defaults to False."""
    metadata = QueryMetadata(total_count=0, query_ms=1.23)

    assert metadata.total_count == 0
    assert metadata.query_ms == 1.23
    assert metadata.depth is None
    assert metadata.cursor is None
    assert metadata.timed_out is False


def test_query_metadata_full_fields() -> None:
    """QueryMetadata accepts all optional fields."""
    metadata = QueryMetadata(
        total_count=42, query_ms=12.3, depth=2, cursor=10, timed_out=True
    )

    assert metadata.total_count == 42
    assert metadata.query_ms == 12.3
    assert metadata.depth == 2
    assert metadata.cursor == 10
    assert metadata.timed_out is True


def test_graph_path_holds_nodes_and_relationships() -> None:
    """GraphPath carries ordered nodes and relationships."""
    node_a = Entity(id="a", name="A", type="concept")
    node_b = Entity(id="b", name="B", type="concept")
    rel = Relationship(source_entity_id="a", target_entity_id="b", type="requires")
    path = GraphPath(nodes=[node_a, node_b], relationships=[rel])

    assert path.nodes == [node_a, node_b]
    assert path.relationships == [rel]


def test_graph_query_result_empty_defaults() -> None:
    """AC-06.14: GraphQueryResult defaults to empty collections and total_count=0."""
    metadata = QueryMetadata(total_count=0, query_ms=0.0)
    result = GraphQueryResult(metadata=metadata)

    assert result.entities == []
    assert result.relationships == []
    assert result.paths == []
    assert result.chunks == []
    assert result.metadata.total_count == 0


def test_graph_query_result_populated() -> None:
    """GraphQueryResult carries all result types."""
    entity = Entity(id="e1", name="Agent", type="agent")
    wrapped = EntityWithContext(entity=entity)
    rel = Relationship(source_entity_id="e1", target_entity_id="e2", type="requires")
    metadata = QueryMetadata(total_count=1, query_ms=5.0)
    result = GraphQueryResult(
        entities=[wrapped], relationships=[rel], chunks=[{"text": "chunk"}], metadata=metadata
    )

    assert len(result.entities) == 1
    assert len(result.relationships) == 1
    assert result.chunks == [{"text": "chunk"}]
    assert result.metadata.total_count == 1


def test_query_timeout_error_is_exception() -> None:
    """QueryTimeoutError is a domain exception."""
    error = QueryTimeoutError()

    assert isinstance(error, Exception)


def test_batch_size_exceeded_error_stores_limit_and_received() -> None:
    """BatchSizeExceededError captures the configured limit and actual count."""
    error = BatchSizeExceededError(limit=200, received=201)

    assert isinstance(error, Exception)
    assert error.limit == 200
    assert error.received == 201
    assert "200" in str(error)
    assert "201" in str(error)


def test_unsupported_query_type_error_stores_query_type() -> None:
    """UnsupportedQueryTypeError captures the rejected query type."""
    error = UnsupportedQueryTypeError(query_type="semantic")

    assert isinstance(error, Exception)
    assert error.query_type == "semantic"
    assert "semantic" in str(error)
