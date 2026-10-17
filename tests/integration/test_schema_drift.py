"""Live-graph schema contract check (opt-in through Neo4j environment settings)."""

from __future__ import annotations

import os

import pytest

from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.neo4j_query_adapter import Neo4jQueryAdapter
from book_graph_rag.infrastructure.neo4j_read_credentials import readonly_settings
from book_graph_rag.infrastructure.neo4j_schema_reader import (
    Neo4jSchemaReader,
    SchemaInferenceError,
)


async def test_live_schema_has_no_manifest_drift() -> None:
    if not os.environ.get("NEO4J_URI"):
        pytest.skip("set NEO4J_URI and the Neo4j credentials to check a live graph")
    try:
        settings = Settings.model_validate({})
    except Exception:
        pytest.skip("Neo4j settings are incomplete; no live graph is configured")

    adapter = Neo4jQueryAdapter(readonly_settings(settings))
    try:
        try:
            result = await Neo4jSchemaReader(adapter, settings).read()
        except SchemaInferenceError as exc:
            if exc.unreachable:
                pytest.skip("configured Neo4j graph is unreachable")
            raise
        if result.graph_unreachable:
            pytest.skip("configured Neo4j graph is unreachable")
        if result.drift.reason == "manifest_missing":
            pytest.skip("no generated schema manifest is available for comparison")
        if result.drift.state == "unknown":
            pytest.skip(f"schema comparison is incomplete: {result.drift.summary()}")
        assert result.drift.state == "match", result.drift.summary()
    finally:
        await adapter.close()
