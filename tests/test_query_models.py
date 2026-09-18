"""Tests for Fase 06 query domain models (AC-06.13, AC-06.14, AC-06.17)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from book_graph_rag.domain.models import (
    QUERY_LOG_SCHEMA_VERSION,
    REDACTED_PLACEHOLDER,
    BatchEntityQuery,
    BatchSizeExceededError,
    Entity,
    EntityQuery,
    EntityWithContext,
    GraphPath,
    GraphQuery,
    GraphQueryResult,
    GraphQueryUnion,
    PathQuery,
    QueryLogEntry,
    QueryMetadata,
    QueryTimeoutError,
    RelationQuery,
    Relationship,
    SimilarityQuery,
    UnsupportedQueryTypeError,
    migrate_query_log_record,
    redact_sensitive,
    redact_sensitive_metadata,
)


@pytest.mark.parametrize(
    ("raw", "expected_type"),
    [
        ({"type": "entity", "name": "MCP"}, EntityQuery),
        ({"type": "relation", "source_id": "e1"}, RelationQuery),
        ({"type": "path", "start_id": "a", "end_id": "b"}, PathQuery),
        ({"type": "similarity", "text": "prompt engineering"}, SimilarityQuery),
        ({"type": "batch_entity", "ids": ["e1", "e2"]}, BatchEntityQuery),
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


def test_batch_entity_query_defaults() -> None:
    """BatchEntityQuery requires ids and defaults type to batch_entity."""
    query = BatchEntityQuery(ids=["e1", "e2"])

    assert query.type == "batch_entity"
    assert query.ids == ["e1", "e2"]


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


def test_query_log_entry_requires_all_fields() -> None:
    """QueryLogEntry requires every mandatory field to be provided."""
    now = datetime.now(tz=UTC)
    entry = QueryLogEntry(
        timestamp=now,
        tool_name="find_entity",
        query_type="entity",
        query_metadata={"name_set": True, "param_count": 1},
        result_count=1,
        zero_results=False,
        entity_not_found=False,
        duration_ms=45.2,
    )

    assert entry.timestamp == now
    assert entry.tool_name == "find_entity"
    assert entry.query_type == "entity"
    assert entry.query_metadata == {"name_set": True, "param_count": 1}
    assert entry.result_count == 1
    assert entry.zero_results is False
    assert entry.entity_not_found is False
    assert entry.duration_ms == 45.2
    assert entry.error_code is None
    assert entry.query_fingerprint is None
    assert entry.prompt_fingerprint is None


def test_query_log_entry_flags_are_boolean() -> None:
    """zero_results and entity_not_found are bool fields, not ints."""
    entry = QueryLogEntry(
        timestamp=datetime.now(tz=UTC),
        tool_name="find_entity",
        query_type="entity",
        query_metadata={"name_set": True, "param_count": 1},
        result_count=0,
        zero_results=True,
        entity_not_found=True,
        duration_ms=12.0,
    )

    assert isinstance(entry.zero_results, bool)
    assert isinstance(entry.entity_not_found, bool)
    assert entry.zero_results is True
    assert entry.entity_not_found is True


def test_query_log_entry_serializes_to_iso_timestamp() -> None:
    """The datetime timestamp serializes as an ISO 8601 string."""
    now = datetime(2026, 6, 22, 14, 30, 0, tzinfo=UTC)
    entry = QueryLogEntry(
        timestamp=now,
        tool_name="search_chunks",
        query_type="search",
        query_metadata={"query_set": True, "limit": 10, "param_count": 2},
        result_count=3,
        zero_results=False,
        entity_not_found=False,
        duration_ms=33.0,
        error_code=None,
    )

    payload = entry.model_dump(mode="json")

    assert payload["timestamp"] == "2026-06-22T14:30:00Z"
    assert payload["tool_name"] == "search_chunks"
    assert payload["query_metadata"] == {"query_set": True, "limit": 10, "param_count": 2}
    assert payload["result_count"] == 3
    assert payload["error_code"] is None


def test_query_log_entry_error_code_is_optional() -> None:
    """error_code accepts a stable code and serializes it (R5)."""
    entry = QueryLogEntry(
        timestamp=datetime.now(tz=UTC),
        tool_name="search_chunks",
        query_type="search",
        query_metadata={"query_set": True, "param_count": 1},
        result_count=0,
        zero_results=True,
        entity_not_found=False,
        duration_ms=0.0,
        error_code="TimeoutError",
    )

    assert entry.error_code == "TimeoutError"
    assert entry.model_dump(mode="json")["error_code"] == "TimeoutError"


# ── Logging privacy: secret redaction (R5, T-G.2) ──────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # Realistic secret-shaped fixtures (all clearly synthetic).
        ("sk-abcdefghijklmnopqrstuvwxyz123456", REDACTED_PLACEHOLDER),
        ("AKIAIOSFODNN7EXAMPLE", REDACTED_PLACEHOLDER),
        ("ghp_" + "a" * 36, REDACTED_PLACEHOLDER),
        ("xoxb-" + "a" * 20, REDACTED_PLACEHOLDER),
        ("eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12, REDACTED_PLACEHOLDER),
        ("Bearer abcdefghijklmnop", REDACTED_PLACEHOLDER),
        ("Basic dXNlcjpwYXNz", REDACTED_PLACEHOLDER),
        # Benign values pass through untouched.
        ("hello world", "hello world"),
        ("MCP", "MCP"),
        ("limit", "limit"),
    ],
)
def test_redact_sensitive_detects_secret_shaped_values(
    value: str, expected: str
) -> None:
    """Secret-shaped values are replaced with a fixed placeholder (R5)."""
    assert redact_sensitive(value) == expected


def test_redact_sensitive_redacts_credential_key_names() -> None:
    """A credential-like key redacts its value regardless of content (R5)."""
    for key in ("api_key", "apikey", "access_token", "password", "client_secret"):
        assert redact_sensitive("any-value", key=key) == REDACTED_PLACEHOLDER


def test_redact_sensitive_preserves_benign_key_and_scalars() -> None:
    """Non-sensitive keys and non-string scalars pass through unchanged."""
    assert redact_sensitive("limit-value", key="limit") == "limit-value"
    assert redact_sensitive(10, key="limit") == 10
    assert redact_sensitive(True, key="include_relations") is True
    assert redact_sensitive(None, key="cursor") is None


def test_redact_sensitive_metadata_redacts_sensitive_entries() -> None:
    """query_metadata redaction replaces secret values in place (R5)."""
    metadata: dict[str, str | int | float | bool | None] = {
        "param_count": 2,
        "limit": 7,
        "api_key": "sk-abcdefghijklmnopqrstuvwxyz123456",
        "query_set": True,
    }

    assert redact_sensitive_metadata(metadata) == {
        "param_count": 2,
        "limit": 7,
        "api_key": REDACTED_PLACEHOLDER,
        "query_set": True,
    }



# ── Logging privacy: schema version + v1→v2 migration (R5, T-G.3) ──────────


def test_query_log_entry_schema_version_defaults_to_v2() -> None:
    """QueryLogEntry stamps schema_version=2 and persists it (R5)."""
    entry = QueryLogEntry(
        timestamp=datetime.now(tz=UTC),
        tool_name="find_entity",
        query_type="entity",
        query_metadata={"name_set": True},
        result_count=1,
        zero_results=False,
        entity_not_found=False,
        duration_ms=45.0,
    )

    assert entry.schema_version == 2
    assert entry.model_dump(mode="json")["schema_version"] == 2
    assert QUERY_LOG_SCHEMA_VERSION == 2


def _v1_record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "timestamp": "2026-06-22T14:30:00Z",
        "tool_name": "find_entity",
        "query_type": "entity",
        "query_params": {"name": "MCP", "limit": 10},
        "result_count": 1,
        "zero_results": False,
        "entity_not_found": False,
        "duration_ms": 45.0,
        "error": None,
    }
    record.update(overrides)
    return record


def test_migrate_query_log_record_v1_maps_to_metadata_only() -> None:
    """A v1 line is migrated to the v2 metadata-only shape (R5)."""
    migrated = migrate_query_log_record(_v1_record(error="TimeoutError"))

    assert "query_params" not in migrated
    assert "error" not in migrated
    assert migrated["error_code"] == "TimeoutError"
    assert migrated["query_metadata"] == {}
    assert migrated["schema_version"] == 2
    assert migrated["tool_name"] == "find_entity"
    assert migrated["result_count"] == 1


def test_migrate_query_log_record_drops_free_text_error() -> None:
    """A free-text v1 error is dropped, never promoted to error_code (R5)."""
    migrated = migrate_query_log_record(
        _v1_record(error="Neo4j connection failed: wrong credentials")
    )

    assert migrated["error_code"] is None
    assert "Neo4j connection failed" not in json.dumps(migrated)


def test_migrate_query_log_record_v2_passes_through() -> None:
    """A v2 line passes through unchanged (schema_version == 2)."""
    v2: dict[str, Any] = {
        "timestamp": "2026-06-22T14:30:00Z",
        "tool_name": "find_entity",
        "query_type": "entity",
        "query_metadata": {"name_set": True},
        "result_count": 1,
        "zero_results": False,
        "entity_not_found": False,
        "duration_ms": 45.0,
        "error_code": None,
        "schema_version": 2,
    }

    assert migrate_query_log_record(v2) == v2


def test_migrate_query_log_record_v2_without_schema_version_stamps_it() -> None:
    """A v2-shaped line without schema_version is stamped with the current one."""
    v2: dict[str, Any] = {
        "timestamp": "2026-06-22T14:30:00Z",
        "tool_name": "find_entity",
        "query_type": "entity",
        "query_metadata": {"name_set": True},
        "result_count": 1,
        "zero_results": False,
        "entity_not_found": False,
        "duration_ms": 45.0,
        "error_code": None,
    }

    migrated = migrate_query_log_record(v2)

    assert migrated["schema_version"] == 2
    assert "query_params" not in migrated


def test_migrated_v1_record_contains_no_raw_or_secret_values() -> None:
    """A migrated v1 record never carries raw query/prompt/error or secrets (R5)."""
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    assert redact_sensitive(secret) == REDACTED_PLACEHOLDER  # fixture is secret-shaped

    v1 = _v1_record(
        query_params={"query": secret, "prompt": "secret prompt text"},
        error=f"secret error text {secret}",
    )

    migrated = migrate_query_log_record(v1)
    entry = QueryLogEntry.model_validate(migrated)
    serialized = entry.model_dump_json()
    parsed = json.loads(serialized)

    assert "secret prompt text" not in serialized
    assert "secret error text" not in serialized
    assert secret not in serialized
    assert "query_params" not in parsed
    assert "error" not in parsed
    assert parsed["error_code"] is None
    assert entry.query_metadata == {}
    assert entry.error_code is None
    assert entry.query_fingerprint is None
    assert entry.prompt_fingerprint is None
