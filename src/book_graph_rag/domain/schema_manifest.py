"""Domain models for the frozen graph-schema manifest and drift reports."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _normalize_properties(value: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        raise ValueError("schema properties must be a mapping")
    normalized: dict[str, tuple[str, ...]] = {}
    for raw_name, raw_properties in sorted(value.items(), key=lambda item: str(item[0])):
        if not isinstance(raw_name, str) or not raw_name:
            raise ValueError("schema names must be non-empty strings")
        if not isinstance(raw_properties, (list, tuple, set, frozenset)):
            raise ValueError("schema properties must be a sequence of names")
        properties = {
            property_name
            for property_name in raw_properties
            if isinstance(property_name, str) and property_name
        }
        normalized[raw_name] = tuple(sorted(properties))
    return normalized


class GraphSchema(BaseModel):
    """A stable label/relationship-property shape observed from Neo4j."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    labels: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    relationship_types: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @field_validator("labels", "relationship_types", mode="before")
    @classmethod
    def normalize_schema_names(cls, value: Any) -> dict[str, tuple[str, ...]]:
        return _normalize_properties(value)

    def describe(self) -> str:
        """Render the schema shape without asserting relationship endpoints."""
        lines = ["Node labels:"]
        if self.labels:
            lines.extend(
                f"- {name} {{{', '.join(properties)}}}" for name, properties in self.labels.items()
            )
        else:
            lines.append("- (empty graph)")
        lines.append("Relationship types:")
        lines.extend(
            f"- ()-[:{name} {{{', '.join(properties)}}}]->()"
            for name, properties in self.relationship_types.items()
        )
        return "\n".join(lines)


class SchemaManifest(BaseModel):
    """Schema body plus the provenance of the graph observation that produced it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(min_length=1)
    committed_at: datetime
    code_commit: str = Field(min_length=1)
    graph_version: str = Field(min_length=1)
    source: Literal["apoc", "native"]
    labels: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    relationship_types: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @field_validator("labels", "relationship_types", mode="before")
    @classmethod
    def normalize_schema_names(cls, value: Any) -> dict[str, tuple[str, ...]]:
        return _normalize_properties(value)

    def to_graph_schema(self) -> GraphSchema:
        """Return the schema body without manifest provenance."""
        return GraphSchema(labels=self.labels, relationship_types=self.relationship_types)


class SchemaDriftReport(BaseModel):
    """Bounded, versioned comparison between a live schema and its manifest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["unknown", "match", "drift"]
    graph_version: str | None
    reason: str | None = None
    added_labels: tuple[str, ...] = ()
    removed_labels: tuple[str, ...] = ()
    added_relationship_types: tuple[str, ...] = ()
    removed_relationship_types: tuple[str, ...] = ()
    added_label_properties: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    removed_label_properties: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    added_relationship_properties: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    removed_relationship_properties: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    truncated: bool = False

    def summary(self) -> str:
        """Return a concise operator-facing description of this comparison."""
        version = self.graph_version or "unknown"
        if self.state == "unknown":
            return f"Schema drift unknown at graph_version={version}: {self.reason or 'unknown'}"
        if self.state == "match":
            return f"Schema matches manifest at graph_version={version}"

        changes = (
            ("added labels", self.added_labels),
            ("removed labels", self.removed_labels),
            ("added relationship types", self.added_relationship_types),
            ("removed relationship types", self.removed_relationship_types),
            ("added label properties", self.added_label_properties),
            ("removed label properties", self.removed_label_properties),
            ("added relationship properties", self.added_relationship_properties),
            ("removed relationship properties", self.removed_relationship_properties),
        )
        details = "; ".join(f"{name}={value}" for name, value in changes if value)
        if self.truncated:
            details = f"{details}; diff truncated" if details else "diff truncated"
        return f"Schema drift at graph_version={version}: {details}"


def compare_schema(
    live: GraphSchema,
    manifest: SchemaManifest | None,
    *,
    graph_version: str | None,
    max_items: int = 100,
) -> SchemaDriftReport:
    """Compare live graph names/properties with a manifest using a global item cap."""
    if max_items < 0:
        raise ValueError("max_items must be non-negative")
    if manifest is None:
        return SchemaDriftReport(
            state="unknown", graph_version=graph_version, reason="manifest_missing"
        )
    if graph_version is None:
        return SchemaDriftReport(
            state="unknown", graph_version=None, reason="graph_version_unavailable"
        )

    frozen = manifest.to_graph_schema()
    live_labels = set(live.labels)
    frozen_labels = set(frozen.labels)
    live_relationships = set(live.relationship_types)
    frozen_relationships = set(frozen.relationship_types)
    remaining = max_items
    truncated = False

    def take_names(values: set[str]) -> tuple[str, ...]:
        nonlocal remaining, truncated
        ordered = sorted(values)
        kept = tuple(ordered[:remaining])
        if len(kept) < len(ordered):
            truncated = True
        remaining -= len(kept)
        return kept

    def take_properties(
        live_values: dict[str, tuple[str, ...]], frozen_values: dict[str, tuple[str, ...]]
    ) -> dict[str, tuple[str, ...]]:
        nonlocal remaining, truncated
        result: dict[str, tuple[str, ...]] = {}
        for name in sorted(set(live_values) | set(frozen_values)):
            values = sorted(set(live_values.get(name, ())) - set(frozen_values.get(name, ())))
            if not values:
                continue
            kept = tuple(values[:remaining])
            if kept:
                result[name] = kept
            if len(kept) < len(values):
                truncated = True
            remaining -= len(kept)
            if remaining == 0 and any(
                set(live_values.get(later, ())) - set(frozen_values.get(later, ()))
                for later in sorted(set(live_values) | set(frozen_values))
                if later > name
            ):
                truncated = True
                break
        return result

    added_labels = take_names(live_labels - frozen_labels)
    removed_labels = take_names(frozen_labels - live_labels)
    added_relationship_types = take_names(live_relationships - frozen_relationships)
    removed_relationship_types = take_names(frozen_relationships - live_relationships)
    added_label_properties = take_properties(live.labels, frozen.labels)
    removed_label_properties = take_properties(frozen.labels, live.labels)
    added_relationship_properties = take_properties(
        live.relationship_types, frozen.relationship_types
    )
    removed_relationship_properties = take_properties(
        frozen.relationship_types, live.relationship_types
    )
    has_drift = any(
        (
            added_labels,
            removed_labels,
            added_relationship_types,
            removed_relationship_types,
            added_label_properties,
            removed_label_properties,
            added_relationship_properties,
            removed_relationship_properties,
        )
    )
    return SchemaDriftReport(
        state="drift" if has_drift else "match",
        graph_version=graph_version,
        added_labels=added_labels,
        removed_labels=removed_labels,
        added_relationship_types=added_relationship_types,
        removed_relationship_types=removed_relationship_types,
        added_label_properties=added_label_properties,
        removed_label_properties=removed_label_properties,
        added_relationship_properties=added_relationship_properties,
        removed_relationship_properties=removed_relationship_properties,
        truncated=truncated,
    )
