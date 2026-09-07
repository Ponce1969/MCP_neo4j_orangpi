"""Tests for the NeighborhoodQueryPort (Slice D).

This port feeds S3 with raw mention-source and related-neighbor sets.
The concrete Neo4j adapter ships in slice E3.
"""

from __future__ import annotations

import inspect

from book_graph_rag.ports.neighborhood_query_port import NeighborhoodQueryPort


def test_neighborhood_query_port_is_abstract() -> None:
    assert inspect.isabstract(NeighborhoodQueryPort)


def test_neighborhood_query_port_declares_required_methods() -> None:
    methods = {name for name, _ in inspect.getmembers(NeighborhoodQueryPort, predicate=inspect.isfunction)}
    assert "mention_sources" in methods
    assert "related_neighbors" in methods
