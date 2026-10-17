from __future__ import annotations

import json
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


def _write_checkpoint_manifest(tmp_path: Path, properties: list[str]) -> None:
    manifest_path = tmp_path / "missing-schema.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "committed_at": "2026-10-17T00:00:00+00:00",
                "code_commit": "abc1234",
                "graph_version": "gv-manifest",
                "source": "native",
                "labels": {"Checkpoint": properties},
                "relationship_types": {},
            }
        ),
        encoding="utf-8",
    )


def _checkpoint_executor(
    *, native_properties: list[str] | None, apoc_properties: list[str] | None = None
) -> _SchemaExecutor:
    apoc_probe = "SHOW PROCEDURES YIELD name WHERE name = 'apoc.meta.data'"
    node_probe = "SHOW PROCEDURES YIELD name WHERE name = 'db.schema.nodeTypeProperties'"
    rel_probe = "SHOW PROCEDURES YIELD name WHERE name = 'db.schema.relTypeProperties'"
    responses: dict[str, list[dict[str, Any]] | Exception] = {
        apoc_probe: _procedure("apoc.meta.data"),
        "CALL apoc.meta.data()": [
            {
                "label": "Checkpoint",
                "elementType": "node",
                "type": "STRING",
                "property": property_name,
            }
            for property_name in (apoc_properties or ["status"])
        ],
        _CATALOG_STATS_CYPHER: [],
    }
    if native_properties is None:
        responses[node_probe] = []
        responses[rel_probe] = []
    else:
        responses[node_probe] = _procedure("db.schema.nodeTypeProperties")
        responses[rel_probe] = _procedure("db.schema.relTypeProperties")
        responses["CALL db.schema.nodeTypeProperties()"] = [
            {"nodeLabels": ["Checkpoint"], "propertyName": property_name}
            for property_name in native_properties
        ]
        responses["CALL db.schema.relTypeProperties()"] = []
    return _SchemaExecutor(responses)


async def test_apoc_description_uses_native_catalog_for_frozen_schema_comparison(
    tmp_path: Path,
) -> None:
    _write_checkpoint_manifest(tmp_path, ["status", "failed_at"])
    executor = _checkpoint_executor(native_properties=["status", "failed_at"])

    result = await Neo4jSchemaReader(executor, _settings(tmp_path)).read()

    assert result.source == "apoc"
    assert result.schema.labels == {"Checkpoint": ("status",)}
    assert result.drift.state == "match"
    assert result.drift.comparison_basis is not None
    assert result.drift.comparison_basis.model_dump(mode="json") == {
        "source": "native",
        "fallback_reason": None,
    }
    assert result.metadata["schema_drift"]["comparison_basis"] == {
        "source": "native",
        "fallback_reason": None,
    }
    assert (
        executor.calls.count(
            "SHOW PROCEDURES YIELD name WHERE name = 'db.schema.nodeTypeProperties'"
        )
        == 1
    )
    assert (
        executor.calls.count(
            "SHOW PROCEDURES YIELD name WHERE name = 'db.schema.relTypeProperties'"
        )
        == 1
    )
    assert executor.calls.count("CALL db.schema.nodeTypeProperties()") == 1
    assert executor.calls.count("CALL db.schema.relTypeProperties()") == 1


async def test_native_unavailable_reports_apoc_fallback_basis(tmp_path: Path) -> None:
    _write_checkpoint_manifest(tmp_path, ["status", "failed_at"])
    executor = _checkpoint_executor(native_properties=None)

    result = await Neo4jSchemaReader(executor, _settings(tmp_path)).read()

    assert result.source == "apoc"
    assert result.drift.state == "drift"
    assert result.drift.removed_label_properties == {"Checkpoint": ("failed_at",)}
    assert result.drift.comparison_basis is not None
    assert result.drift.comparison_basis.model_dump(mode="json") == {
        "source": "apoc",
        "fallback_reason": "native_procedures_unavailable",
    }
    assert "comparison_basis=apoc" in result.drift.summary()
    assert "fallback: native_procedures_unavailable" in result.drift.summary()


async def test_real_native_schema_difference_still_reports_drift(tmp_path: Path) -> None:
    _write_checkpoint_manifest(tmp_path, ["status", "failed_at"])
    executor = _checkpoint_executor(native_properties=["status"])

    result = await Neo4jSchemaReader(executor, _settings(tmp_path)).read()

    assert result.drift.state == "drift"
    assert result.drift.removed_label_properties == {"Checkpoint": ("failed_at",)}
    assert result.drift.comparison_basis is not None
    assert result.drift.comparison_basis.model_dump(mode="json") == {
        "source": "native",
        "fallback_reason": None,
    }
    assert "comparison_basis=native" in result.drift.summary()


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
    assert result.drift.comparison_basis is not None
    assert result.drift.comparison_basis.model_dump(mode="json") == {
        "source": "sample",
        "fallback_reason": "native_schema_unusable",
    }
    assert node_sample in executor.calls


def test_native_parser_normalizes_live_colon_backtick_schema_names() -> None:
    schema = Neo4jSchemaReader._schema_from_native(
        node_records=[{"nodeLabels": [":`Book`"], "propertyName": "title"}],
        rel_records=[{"relType": ":`CONTAINS`", "propertyName": "source"}],
    )

    assert schema is not None
    assert schema.labels == {"Book": ("title",)}
    assert schema.relationship_types == {"CONTAINS": ("source",)}


def test_native_parser_preserves_propertyless_relationship_types() -> None:
    propertyless_relationships = [
        "CONTAINS",
        "HAS_CHUNK",
        "HAS_SECTION",
        "HAS_SUBSECTION",
    ]
    schema = Neo4jSchemaReader._schema_from_native(
        node_records=[],
        rel_records=[
            {"relType": f":`{name}`", "propertyName": None} for name in propertyless_relationships
        ],
    )

    assert schema is not None
    assert schema.relationship_types == {
        "CONTAINS": (),
        "HAS_CHUNK": (),
        "HAS_SECTION": (),
        "HAS_SUBSECTION": (),
    }


async def test_sample_tier_uses_the_same_clean_names_as_native(tmp_path: Path) -> None:
    native = Neo4jSchemaReader._schema_from_native(
        node_records=[{"nodeLabels": [":`Book`"], "propertyName": "title"}],
        rel_records=[{"relType": ":`CONTAINS`", "propertyName": "source"}],
    )
    node_sample = "MATCH (n:`Book`) WITH n LIMIT 1 RETURN keys(n) AS properties"
    rel_sample = "MATCH ()-[r:`CONTAINS`]-() WITH r LIMIT 1 RETURN keys(r) AS properties"
    executor = _SchemaExecutor(
        {
            "CALL db.labels()": [{"label": "Book"}],
            "CALL db.relationshipTypes()": [{"relationshipType": "CONTAINS"}],
            node_sample: [{"properties": ["title"]}],
            rel_sample: [{"properties": ["source"]}],
        }
    )

    sampled = await Neo4jSchemaReader(executor, _settings(tmp_path))._sample_schema([], [False])

    assert native is not None
    assert sampled is not None
    assert sampled.labels == native.labels
    assert sampled.relationship_types == native.relationship_types


def test_apoc_parser_ignores_relationship_endpoint_labels_as_properties() -> None:
    schema = Neo4jSchemaReader._schema_from_apoc(
        [
            {
                "label": "RELATED",
                "elementType": "relationship",
                "type": "RELATIONSHIP",
                "property": "Entity",
            },
            {
                "label": "RELATED",
                "elementType": "relationship",
                "type": "INTEGER",
                "property": "source_page",
            },
            {
                "label": "RELATED",
                "elementType": "relationship",
                "type": "STRING",
                "property": "type",
            },
            {
                "label": "HAS_CHUNK",
                "elementType": "relationship",
                "type": "RELATIONSHIP",
                "property": "Chapter",
            },
            {
                "label": "CONTAINS",
                "elementType": "relationship",
                "type": "RELATIONSHIP",
                "property": "Book",
            },
            {
                "label": "MENTIONS",
                "elementType": "relationship",
                "type": "INTEGER",
                "property": "chunk_index",
            },
        ]
    )

    assert schema is not None
    assert schema.labels == {"Book": (), "Chapter": (), "Entity": ()}
    assert schema.relationship_types == {
        "CONTAINS": (),
        "HAS_CHUNK": (),
        "MENTIONS": ("chunk_index",),
        "RELATED": ("source_page", "type"),
    }


# Exact rows from the operator's production dump (label | elementType | type | property | other).
_APOC_LIVE_PRODUCTION_ROWS = [
    {
        "label": "CONTAINS",
        "elementType": "relationship",
        "type": "RELATIONSHIP",
        "property": "Book",
        "other": ["Chapter"],
    },
    {
        "label": "HAS_CHUNK",
        "elementType": "relationship",
        "type": "RELATIONSHIP",
        "property": "Chapter",
        "other": ["Chunk"],
    },
    {
        "label": "RELATED",
        "elementType": "relationship",
        "type": "INTEGER",
        "property": "source_page",
        "other": [],
    },
    {
        "label": "RELATED",
        "elementType": "relationship",
        "type": "STRING",
        "property": "description",
        "other": [],
    },
    {
        "label": "MENTIONS",
        "elementType": "relationship",
        "type": "RELATIONSHIP",
        "property": "Chunk",
        "other": ["Entity"],
    },
    {
        "label": "Book",
        "elementType": "node",
        "type": "RELATIONSHIP",
        "property": "CONTAINS",
        "other": ["Chapter"],
    },
    {
        "label": "Book",
        "elementType": "node",
        "type": "STRING",
        "property": "author",
        "other": [],
    },
]


def test_apoc_shape_a_endpoint_routes_relationship_type_and_node_label() -> None:
    schema = Neo4jSchemaReader._schema_from_apoc([_APOC_LIVE_PRODUCTION_ROWS[0]])

    assert schema is not None
    assert schema.labels == {"Book": ()}
    assert schema.relationship_types == {"CONTAINS": ()}


def test_apoc_shape_a_scalar_type_routes_relationship_property() -> None:
    schema = Neo4jSchemaReader._schema_from_apoc([_APOC_LIVE_PRODUCTION_ROWS[2]])

    assert schema is not None
    assert schema.labels == {}
    assert schema.relationship_types == {"RELATED": ("source_page",)}


# Shape B mirrors the relationship extraction query in the installed consumer:
# .venv/Lib/site-packages/langchain_community/graphs/neo4j_graph.py:37-44
# (CALL apoc.meta.data(), WHERE type = "RELATIONSHIP" AND elementType = "node",
# then RETURN {start: label, type: property, end: toString(other_node)}).
_APOC_SHAPE_B_RELATIONSHIP_ROWS = [
    {
        "label": "Book",
        "other": ["Chapter"],
        "elementType": "node",
        "type": "RELATIONSHIP",
        "property": "CONTAINS",
    },
    {
        "label": "Chapter",
        "other": ["Chunk"],
        "elementType": "node",
        "type": "RELATIONSHIP",
        "property": "HAS_CHUNK",
    },
    {
        "label": "Chapter",
        "other": ["Section"],
        "elementType": "node",
        "type": "RELATIONSHIP",
        "property": "HAS_SECTION",
    },
    {
        "label": "Section",
        "other": ["Section"],
        "elementType": "node",
        "type": "RELATIONSHIP",
        "property": "HAS_SUBSECTION",
    },
]


def test_apoc_shape_b_endpoint_routes_node_label_and_relationship_type() -> None:
    schema = Neo4jSchemaReader._schema_from_apoc([_APOC_LIVE_PRODUCTION_ROWS[5]])

    assert schema is not None
    assert schema.labels == {"Book": ()}
    assert schema.relationship_types == {"CONTAINS": ()}


def test_apoc_shape_b_scalar_type_routes_node_property() -> None:
    schema = Neo4jSchemaReader._schema_from_apoc([_APOC_LIVE_PRODUCTION_ROWS[6]])

    assert schema is not None
    assert schema.labels == {"Book": ("author",)}
    assert schema.relationship_types == {}


def test_apoc_parser_preserves_propertyless_relationship_types_from_consumer_shape() -> None:
    schema = Neo4jSchemaReader._schema_from_apoc(_APOC_SHAPE_B_RELATIONSHIP_ROWS)

    assert schema is not None
    assert schema.relationship_types == {
        "CONTAINS": (),
        "HAS_CHUNK": (),
        "HAS_SECTION": (),
        "HAS_SUBSECTION": (),
    }
    assert schema.labels == {"Book": (), "Chapter": (), "Section": ()}


def test_apoc_endpoint_labels_are_never_relationship_properties_in_either_shape() -> None:
    schema = Neo4jSchemaReader._schema_from_apoc(
        [*_APOC_LIVE_PRODUCTION_ROWS[:5], *_APOC_SHAPE_B_RELATIONSHIP_ROWS]
    )

    assert schema is not None
    properties = {
        property_name
        for values in (*schema.labels.values(), *schema.relationship_types.values())
        for property_name in values
    }
    assert properties.isdisjoint({"Book", "Chapter", "Chunk", "Entity", "Section"})


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
