from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.neo4j_schema_reader import (
    _CATALOG_STATS_CYPHER,
    Neo4jSchemaReader,
    SchemaInferenceError,
)


class _SchemaExecutor:
    def __init__(self, responses: dict[str, list[dict[str, Any]] | Exception]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    async def execute_read(
        self, cypher: str, parameters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append(cypher)
        response = self.responses.get(cypher, [])
        if isinstance(response, Exception):
            raise response
        return response


def _procedure(name: str) -> list[dict[str, Any]]:
    return [{"name": name}]


def _settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "not-a-real-secret",
            "schema_manifest_path": tmp_path / "missing-schema.json",
        }
    )


async def test_apoc_rows_without_usable_kind_report_unusable_records(tmp_path: Path) -> None:
    apoc_probe = "SHOW PROCEDURES YIELD name WHERE name = 'apoc.meta.data'"
    executor = _SchemaExecutor(
        {
            apoc_probe: _procedure("apoc.meta.data"),
            "CALL apoc.meta.data()": [
                {
                    "label": "Document",
                    "elementType": "unexpected",
                    "type": "node",
                    "property": "title",
                }
            ],
            "CALL db.labels()": RuntimeError("labels unavailable"),
        }
    )

    with pytest.raises(SchemaInferenceError) as error:
        await Neo4jSchemaReader(executor, _settings(tmp_path)).read()

    assert "apoc.meta.data returned rows but no usable schema records" in str(error.value)
    assert "apoc.meta.data returned no schema records" not in str(error.value)


async def test_live_native_schema_wins_over_timed_out_apoc_probe(tmp_path: Path) -> None:
    apoc_probe = "SHOW PROCEDURES YIELD name WHERE name = 'apoc.meta.data'"
    node_probe = "SHOW PROCEDURES YIELD name WHERE name = 'db.schema.nodeTypeProperties'"
    rel_probe = "SHOW PROCEDURES YIELD name WHERE name = 'db.schema.relTypeProperties'"
    executor = _SchemaExecutor(
        {
            apoc_probe: TimeoutError("APOC availability probe timed out"),
            node_probe: _procedure("db.schema.nodeTypeProperties"),
            rel_probe: _procedure("db.schema.relTypeProperties"),
            "CALL db.schema.nodeTypeProperties()": [
                {"nodeLabels": ["Document"], "propertyName": "title"}
            ],
            "CALL db.schema.relTypeProperties()": [],
        }
    )

    result = await Neo4jSchemaReader(executor, _settings(tmp_path)).read()

    assert result.live_schema_available is True
    assert result.graph_unreachable is False
    assert result.schema.labels == {"Document": ("title",)}


async def test_live_apoc_schema_wins_over_timed_out_version_census(tmp_path: Path) -> None:
    apoc_probe = "SHOW PROCEDURES YIELD name WHERE name = 'apoc.meta.data'"
    executor = _SchemaExecutor(
        {
            apoc_probe: _procedure("apoc.meta.data"),
            "CALL apoc.meta.data()": [
                {
                    "label": "Document",
                    "other": None,
                    "elementType": "node",
                    "type": "STRING",
                    "property": "title",
                }
            ],
            # The census is deliberately not a schema probe.
            _CATALOG_STATS_CYPHER: TimeoutError("version census timed out"),
        }
    )

    result = await Neo4jSchemaReader(executor, _settings(tmp_path)).read()

    assert result.source == "apoc"
    assert result.graph_unreachable is False
    assert result.schema.labels == {"Document": ("title",)}


async def test_unparseable_native_rows_fall_through_to_bounded_samples(tmp_path: Path) -> None:
    apoc_probe = "SHOW PROCEDURES YIELD name WHERE name = 'apoc.meta.data'"
    node_probe = "SHOW PROCEDURES YIELD name WHERE name = 'db.schema.nodeTypeProperties'"
    rel_probe = "SHOW PROCEDURES YIELD name WHERE name = 'db.schema.relTypeProperties'"
    node_sample = "MATCH (n:`Document`) WITH n LIMIT 1 RETURN keys(n) AS properties"
    executor = _SchemaExecutor(
        {
            apoc_probe: [],
            node_probe: _procedure("db.schema.nodeTypeProperties"),
            rel_probe: _procedure("db.schema.relTypeProperties"),
            "CALL db.schema.nodeTypeProperties()": [{"labels": ["Document"], "name": "title"}],
            "CALL db.schema.relTypeProperties()": [],
            "CALL db.labels()": [{"label": "Document"}],
            "CALL db.relationshipTypes()": [],
            node_sample: [{"properties": ["title"]}],
        }
    )

    result = await Neo4jSchemaReader(executor, _settings(tmp_path)).read()

    assert result.source == "native"
    assert result.schema.labels == {"Document": ("title",)}
    assert node_sample in executor.calls


def test_apoc_parser_uses_element_type_and_real_property_columns() -> None:
    schema = Neo4jSchemaReader._schema_from_apoc(
        [
            {
                "label": "Document",
                "other": None,
                "elementType": "node",
                "type": "STRING",
                "property": "title",
            },
            {
                "label": "CITES",
                "other": "Document",
                "elementType": "relationship",
                "type": "STRING",
                "property": "since",
            },
            {
                "label": "REFERENCES",
                "other": "Document",
                "elementType": "relationship",
                "type": "RELATIONSHIP",
                "property": None,
            },
            {
                "label": "Fallback",
                "elementType": "NODE",
                "type": "INTEGER",
                "propertyName": "count",
            },
            {"label": "Ignored", "type": "STRING", "property": "name"},
            {"label": "Legacy", "type": "NODE", "property": "name"},
        ]
    )

    assert schema is not None
    assert schema.labels == {"Document": ("title",), "Fallback": ("count",), "Legacy": ("name",)}
    assert schema.relationship_types == {"CITES": ("since",), "REFERENCES": ()}
