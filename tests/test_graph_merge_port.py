"""Unit tests for the graph merge port contract."""

from __future__ import annotations

from abc import ABC

import pytest

from book_graph_rag.domain.merge_ledger_models import EdgeInverseMap, FoldedAlias
from book_graph_rag.ports.graph_merge_port import GraphMergePort, InverseMappingSnapshot


def test_inverse_mapping_snapshot_round_trips() -> None:
    """The snapshot is frozen, serializable, and carries aliases + edge inverse map."""
    snap = InverseMappingSnapshot(
        aliases_before={
            "book:ch1:dup-a": ("Alias A", "alias-a"),
            "book:ch1:dup-b": ("B",),
        },
        edge_inverse_map=[
            EdgeInverseMap(
                edge_kind="MENTIONS",
                duplicate_entity_id="book:ch1:dup-a",
                original_other_endpoint_id="book:ch1:chunk-0",
                edge_properties={"source_page": 12},
            ),
        ],
    )
    payload = snap.model_dump_json()
    restored = InverseMappingSnapshot.model_validate_json(payload)
    assert restored.aliases_before == snap.aliases_before
    assert restored.edge_inverse_map == snap.edge_inverse_map


def test_graph_merge_port_is_abstract() -> None:
    """GraphMergePort cannot be instantiated and declares the required methods."""
    assert issubclass(GraphMergePort, ABC)
    with pytest.raises(TypeError):
        GraphMergePort()  # type: ignore[abstract]

    assert callable(getattr(GraphMergePort, "capture_inverse_mapping", None))
    assert callable(getattr(GraphMergePort, "apply_merge", None))
    assert callable(getattr(GraphMergePort, "rollback_merge", None))


def test_edge_inverse_map_requires_known_kind() -> None:
    """Only MENTIONS or RELATED are valid edge kinds in the inverse map."""
    with pytest.raises(ValueError, match="edge_kind"):
        EdgeInverseMap(
            edge_kind="INVALID",  # type: ignore[arg-type]
            duplicate_entity_id="book:ch1:dup-a",
            original_other_endpoint_id="book:ch1:chunk-0",
            edge_properties={},
        )


def test_folded_alias_is_frozen_and_hashable() -> None:
    """FoldedAlias can be used in sets/dicts and serializes deterministically."""
    alias = FoldedAlias(from_entity_id="book:ch1:dup-a", alias_value="Alias A")
    assert hash(alias) is not None
    assert alias.model_dump_json() == alias.model_dump_json()
