"""Read Neo4j schema through the shared APOC/native/manifest ladder."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import ValidationError

from book_graph_rag.config import Settings
from book_graph_rag.domain.checkpoint_models import format_redacted_error
from book_graph_rag.domain.graph_version import graph_version_from
from book_graph_rag.domain.models import CypherGenerationError
from book_graph_rag.domain.namespaces import Catalog
from book_graph_rag.domain.schema_manifest import (
    GraphSchema,
    SchemaDriftReport,
    SchemaManifest,
    compare_schema,
)
from book_graph_rag.infrastructure.catalog_loader import CatalogLoader

logger = logging.getLogger(__name__)

_APOC_PROCEDURE = "apoc.meta.data"
_NODE_SCHEMA_PROCEDURE = "db.schema.nodeTypeProperties"
_REL_SCHEMA_PROCEDURE = "db.schema.relTypeProperties"
_MAX_SCHEMA_SAMPLES = 100
_MAX_DRIFT_ITEMS = 100

# Keep this census aligned with mcp_server_main._CATALOG_STATS_CYPHER. These are
# the same counts graph_version_from already uses for the MCP's graph identity.
_CATALOG_STATS_CYPHER = """
CALL {
  MATCH (c:Chunk)
  WHERE c.book_id IS NOT NULL
  WITH c.book_id AS source_id, count(*) AS chunks
  RETURN source_id, chunks, 0 AS entities
  UNION ALL
  MATCH (e:Entity)
  WHERE e.id IS NOT NULL AND e.id CONTAINS ':'
    AND (e.merged_into IS NULL OR e.merged_into = '')
  WITH split(e.id, ':') AS parts, count(*) AS entities
  WHERE size(parts) >= 2
  RETURN parts[0] + ':' + parts[1] AS source_id, 0 AS chunks, entities
}
RETURN source_id, sum(chunks) AS chunks, sum(entities) AS entities
"""


class _SchemaQueryExecutor(Protocol):
    async def execute_read(
        self, cypher: str, parameters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]: ...


class SchemaInferenceError(CypherGenerationError):
    """No live schema or valid manifest could be read."""

    def __init__(self, message: str, *, unreachable: bool = False) -> None:
        super().__init__(message)
        self.unreachable = unreachable


@dataclass(frozen=True)
class SchemaReadResult:
    """Selected schema plus the runtime comparison metadata."""

    schema: GraphSchema
    source: Literal["apoc", "native", "manifest"]
    graph_version: str | None
    drift: SchemaDriftReport
    live_schema_available: bool
    graph_unreachable: bool = False

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "schema_drift": self.drift.model_dump(mode="json"),
            "graph_version": self.graph_version,
        }


class Neo4jSchemaReader:
    """The single schema ladder shared by runtime inference and schema commands."""

    def __init__(self, executor: _SchemaQueryExecutor, settings: Settings) -> None:
        self._executor = executor
        self._settings = settings

    async def read(self) -> SchemaReadResult:
        """Read schema tiers; only failed live reads decide whether the graph is unreachable.

        A successful live schema read overrides connection-like failures from other probes,
        including the graph-version census. If no live schema is available, those failures
        still mark the result unreachable, even when a manifest can supply a fallback schema.
        """
        failures: list[str] = []
        unreachable = [False]
        live_result = await self._read_live_schema(failures, unreachable)
        graph_version, version_unreachable = await self._graph_version()
        unreachable[0] = unreachable[0] or version_unreachable
        manifest, manifest_error = self._load_manifest()

        if live_result is not None:
            live_schema, source = live_result
            if manifest_error is not None:
                drift = SchemaDriftReport(
                    state="unknown",
                    graph_version=graph_version,
                    reason="manifest_invalid",
                )
                failures.append(manifest_error)
            else:
                drift = compare_schema(
                    live_schema,
                    manifest,
                    graph_version=graph_version,
                    max_items=_MAX_DRIFT_ITEMS,
                )
            if drift.state != "match":
                logger.warning(drift.summary())
            return SchemaReadResult(
                schema=live_schema,
                source=source,
                graph_version=graph_version,
                drift=drift,
                live_schema_available=True,
                graph_unreachable=False,
            )

        if manifest is not None:
            drift = SchemaDriftReport(
                state="unknown",
                graph_version=graph_version,
                reason="live_schema_unavailable",
            )
            logger.warning(drift.summary())
            return SchemaReadResult(
                schema=manifest.to_graph_schema(),
                source="manifest",
                graph_version=graph_version,
                drift=drift,
                live_schema_available=False,
                graph_unreachable=unreachable[0],
            )

        manifest_failure = manifest_error or "manifest: missing"
        raise SchemaInferenceError(
            "Schema inference failed; probes failed: " + "; ".join([*failures, manifest_failure]),
            unreachable=unreachable[0],
        )

    async def _read_live_schema(
        self, failures: list[str], unreachable: list[bool]
    ) -> tuple[GraphSchema, Literal["apoc", "native"]] | None:
        apoc = await self._procedure_available(_APOC_PROCEDURE, failures, unreachable)
        if apoc:
            try:
                records = await self._executor.execute_read("CALL apoc.meta.data()")
                schema = self._schema_from_apoc(records)
                if schema is not None:
                    return schema, "apoc"
                if records:
                    failures.append("apoc.meta.data returned rows but no usable schema records")
                else:
                    failures.append("apoc.meta.data returned no schema records")
            except Exception as exc:  # noqa: BLE001 - continue to the next schema tier
                self._record_failure("apoc.meta.data", exc, failures, unreachable)

        native = await self._read_native_schema(failures, unreachable)
        if native is not None:
            return native, "native"
        return None

    async def _procedure_available(
        self, name: str, failures: list[str], unreachable: list[bool]
    ) -> bool:
        query = f"SHOW PROCEDURES YIELD name WHERE name = '{name}'"
        try:
            rows = await self._executor.execute_read(query)
        except Exception as exc:  # noqa: BLE001 - use the next permitted tier
            self._record_failure(f"SHOW PROCEDURES {name}", exc, failures, unreachable)
            return False
        available = any(row.get("name") == name for row in rows)
        if not available:
            failures.append(f"{name}: unavailable")
        return available

    async def _read_native_schema(
        self, failures: list[str], unreachable: list[bool]
    ) -> GraphSchema | None:
        node_available = await self._procedure_available(
            _NODE_SCHEMA_PROCEDURE, failures, unreachable
        )
        rel_available = await self._procedure_available(
            _REL_SCHEMA_PROCEDURE, failures, unreachable
        )
        if node_available and rel_available:
            try:
                node_records = await self._executor.execute_read(
                    "CALL db.schema.nodeTypeProperties()"
                )
                rel_records = await self._executor.execute_read(
                    "CALL db.schema.relTypeProperties()"
                )
                # Empty property rows are valid for an empty graph; propertyTypes
                # deliberately has no role in deciding whether this tier worked.
                schema = self._schema_from_native(node_records, rel_records)
                if schema is not None:
                    return schema
                failures.append("db.schema.* returned rows but no usable schema records")
            except Exception as exc:  # noqa: BLE001 - use bounded samples next
                self._record_failure("db.schema.*", exc, failures, unreachable)

        return await self._sample_schema(failures, unreachable)

    async def _sample_schema(
        self, failures: list[str], unreachable: list[bool]
    ) -> GraphSchema | None:
        try:
            label_rows = await self._executor.execute_read("CALL db.labels()")
        except Exception as exc:  # noqa: BLE001 - no graph-wide node inventory
            self._record_failure("db.labels", exc, failures, unreachable)
            return None
        try:
            rel_rows = await self._executor.execute_read("CALL db.relationshipTypes()")
        except Exception as exc:  # noqa: BLE001 - no graph-wide relationship inventory
            self._record_failure("db.relationshipTypes", exc, failures, unreachable)
            return None

        labels = sorted({str(row["label"]) for row in label_rows if row.get("label") is not None})
        relationships = sorted(
            {
                str(row.get("relationshipType", row.get("relationship_type")))
                for row in rel_rows
                if row.get("relationshipType", row.get("relationship_type")) is not None
            }
        )
        if len(labels) + len(relationships) > _MAX_SCHEMA_SAMPLES:
            failures.append(
                f"db.labels()/db.relationshipTypes() exceeded the {_MAX_SCHEMA_SAMPLES}-sample cap"
            )
            return None

        label_properties: dict[str, set[str]] = {label: set() for label in labels}
        rel_properties: dict[str, set[str]] = {rel: set() for rel in relationships}
        try:
            for label in labels:
                escaped = label.replace("`", "``")
                query = f"MATCH (n:`{escaped}`) WITH n LIMIT 1 RETURN keys(n) AS properties"
                rows = await self._executor.execute_read(query)
                label_properties[label].update(self._sampled_properties(rows))
            for rel_type in relationships:
                escaped = rel_type.replace("`", "``")
                query = f"MATCH ()-[r:`{escaped}`]-() WITH r LIMIT 1 RETURN keys(r) AS properties"
                rows = await self._executor.execute_read(query)
                rel_properties[rel_type].update(self._sampled_properties(rows))
        except Exception as exc:  # noqa: BLE001 - a partial sample is not complete evidence
            self._record_failure("bounded schema sample", exc, failures, unreachable)
            return None
        return GraphSchema(
            labels={name: tuple(properties) for name, properties in label_properties.items()},
            relationship_types={
                name: tuple(properties) for name, properties in rel_properties.items()
            },
        )

    @staticmethod
    def _sampled_properties(rows: list[dict[str, Any]]) -> set[str]:
        if not rows:
            return set()
        values = rows[0].get("properties", rows[0].get("keys(n)", rows[0].get("keys(r)", [])))
        return {str(value) for value in values or [] if isinstance(value, str)}

    @staticmethod
    def _schema_from_apoc(records: list[dict[str, Any]]) -> GraphSchema | None:
        labels: dict[str, set[str]] = {}
        relationships: dict[str, set[str]] = {}
        for record in records:
            kind_value = (
                record.get("elementType") if "elementType" in record else record.get("type")
            )
            if not isinstance(kind_value, str):
                continue
            kind = kind_value.lower()
            if kind not in {"node", "relationship"}:
                continue
            raw_name = record.get("label")
            if not isinstance(raw_name, str) or not raw_name:
                raw_name = record.get("name")
            if not isinstance(raw_name, str) or not raw_name:
                continue
            destination = labels if kind == "node" else relationships
            name = raw_name.lstrip(":")
            properties = destination.setdefault(name, set())
            property_name = record.get("property")
            if not isinstance(property_name, str) or not property_name:
                property_name = record.get("propertyName")
            if isinstance(property_name, str) and property_name:
                properties.add(property_name)
        if not labels and not relationships:
            return None
        return GraphSchema(
            labels={name: tuple(properties) for name, properties in labels.items()},
            relationship_types={
                name: tuple(properties) for name, properties in relationships.items()
            },
        )

    @staticmethod
    def _schema_from_native(
        node_records: list[dict[str, Any]], rel_records: list[dict[str, Any]]
    ) -> GraphSchema | None:
        labels: dict[str, set[str]] = {}
        relationships: dict[str, set[str]] = {}
        for record in node_records:
            raw_labels = record.get("nodeLabels", [])
            if isinstance(raw_labels, str):
                raw_labels = [raw_labels]
            if not isinstance(raw_labels, (list, tuple, set, frozenset)):
                return None
            valid_labels = [label for label in raw_labels if isinstance(label, str) and label]
            if not valid_labels:
                return None
            property_name = record.get("propertyName")
            for raw_label in valid_labels:
                label = raw_label.lstrip(":")
                properties = labels.setdefault(label, set())
                if isinstance(property_name, str) and property_name:
                    properties.add(property_name)
        for record in rel_records:
            raw_type = record.get("relType")
            if not isinstance(raw_type, str) or not raw_type:
                return None
            property_name = record.get("propertyName")
            rel_type = raw_type.lstrip(":")
            properties = relationships.setdefault(rel_type, set())
            if isinstance(property_name, str) and property_name:
                properties.add(property_name)
        return GraphSchema(
            labels={name: tuple(properties) for name, properties in labels.items()},
            relationship_types={
                name: tuple(properties) for name, properties in relationships.items()
            },
        )

    async def _graph_version(self) -> tuple[str | None, bool]:
        try:
            rows = await self._executor.execute_read(_CATALOG_STATS_CYPHER)
            stats = _stats_from_rows(rows)
            catalog: Catalog = CatalogLoader(self._settings.catalog_path).load()
        except Exception as exc:  # noqa: BLE001 - version failure never blocks a runtime query
            logger.warning(
                "Graph version unavailable: %s",
                format_redacted_error(
                    exc,
                    key_id=self._settings.mcp_hmac_key_id,
                    secret=self._settings.mcp_hmac_key.get_secret_value(),
                ),
            )
            return None, _is_unreachable(exc)
        return graph_version_from(catalog.version, stats), False

    def _load_manifest(self) -> tuple[SchemaManifest | None, str | None]:
        path: Path = self._settings.schema_manifest_path
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None, None
        except Exception as exc:  # noqa: BLE001 - report only redacted metadata
            return None, f"manifest read failed: {self._redact(exc)}"
        try:
            value = json.loads(content)
            return SchemaManifest.model_validate(value), None
        except (ValueError, ValidationError) as exc:
            return None, f"manifest invalid: {self._redact(exc)}"

    def _record_failure(
        self,
        name: str,
        error: BaseException,
        failures: list[str],
        unreachable: list[bool],
    ) -> None:
        failures.append(f"{name}: {self._redact(error)}")
        unreachable[0] = unreachable[0] or _is_unreachable(error)

    def _redact(self, error: BaseException) -> str:
        return format_redacted_error(
            error,
            key_id=self._settings.mcp_hmac_key_id,
            secret=self._settings.mcp_hmac_key.get_secret_value(),
        )


def _stats_from_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    stats: dict[str, dict[str, int]] = {}
    for row in rows:
        source_id = row.get("source_id")
        if isinstance(source_id, str) and source_id:
            chunks = row.get("chunks")
            entities = row.get("entities")
            stats[source_id] = {
                "chunks": chunks if isinstance(chunks, int) else 0,
                "entities": entities if isinstance(entities, int) else 0,
            }
    return stats


def _is_unreachable(error: BaseException) -> bool:
    name = type(error).__name__.lower()
    return any(
        marker in name
        for marker in ("connection", "serviceunavailable", "sessionexpired", "timeout", "network")
    )
