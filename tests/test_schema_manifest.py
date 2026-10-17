from __future__ import annotations

from datetime import UTC, datetime


def test_schema_manifest_preserves_sorted_schema_and_provenance() -> None:
    from book_graph_rag.domain.schema_manifest import GraphSchema, SchemaManifest

    schema = GraphSchema(
        labels={"Zeta": ("zeta", "alpha", "alpha"), "Alpha": ()},
        relationship_types={"LINKS": ("since",)},
    )
    manifest = SchemaManifest(
        schema_version="1.0.0",
        committed_at=datetime(2026, 10, 17, tzinfo=UTC),
        code_commit="abc1234",
        graph_version="gv-0123456789ab",
        source="native",
        labels=schema.labels,
        relationship_types=schema.relationship_types,
    )

    assert manifest.labels["Zeta"] == ("alpha", "zeta")
    assert manifest.relationship_types["LINKS"] == ("since",)
    assert manifest.model_dump(mode="json")["graph_version"] == "gv-0123456789ab"
    assert manifest.model_dump(mode="json")["source"] == "native"


def test_schema_drift_is_sorted_versioned_bounded_and_unknown_without_manifest() -> None:
    from book_graph_rag.domain.schema_manifest import (
        GraphSchema,
        SchemaManifest,
        compare_schema,
    )

    manifest = SchemaManifest(
        schema_version="1.0.0",
        committed_at=datetime(2026, 10, 17, tzinfo=UTC),
        code_commit="abc1234",
        graph_version="gv-manifest",
        source="apoc",
        labels={"Document": ("title", "obsolete"), "Removed": ()},
        relationship_types={"CITES": ("since",)},
    )
    live = GraphSchema(
        labels={"Document": ("title", "url"), "Added": ()},
        relationship_types={"REFERENCES": ("weight",)},
    )

    report = compare_schema(live, manifest, graph_version="gv-live", max_items=20)

    assert report.state == "drift"
    assert report.graph_version == "gv-live"
    assert report.added_labels == ("Added",)
    assert report.removed_labels == ("Removed",)
    assert report.added_relationship_types == ("REFERENCES",)
    assert report.removed_relationship_types == ("CITES",)
    assert report.added_label_properties == {"Document": ("url",)}
    assert report.removed_label_properties == {"Document": ("obsolete",)}

    bounded = compare_schema(live, manifest, graph_version="gv-live", max_items=2)
    bounded_items = (
        len(bounded.added_labels)
        + len(bounded.removed_labels)
        + len(bounded.added_relationship_types)
        + len(bounded.removed_relationship_types)
        + sum(map(len, bounded.added_label_properties.values()))
        + sum(map(len, bounded.removed_label_properties.values()))
        + sum(map(len, bounded.added_relationship_properties.values()))
        + sum(map(len, bounded.removed_relationship_properties.values()))
    )
    assert bounded_items <= 2
    assert bounded.truncated is True

    zero_cap = compare_schema(live, manifest, graph_version="gv-live", max_items=0)
    assert zero_cap.state == "drift"
    assert zero_cap.added_labels == ()
    assert zero_cap.truncated is True

    unknown = compare_schema(live, None, graph_version="gv-current")
    assert unknown.state == "unknown"
    assert unknown.graph_version == "gv-current"
    assert unknown.reason == "manifest_missing"


def test_relationship_only_schema_is_not_described_as_an_empty_graph() -> None:
    from book_graph_rag.domain.schema_manifest import GraphSchema

    description = GraphSchema(relationship_types={"CITES": ()}).describe()

    assert "- (empty graph)" not in description
    assert "- ()-[:CITES {}]->()" in description
