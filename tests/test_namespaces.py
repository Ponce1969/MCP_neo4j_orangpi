"""Tests for domain.namespaces (knowledge namespace id scheme)."""

from __future__ import annotations

import pytest

from book_graph_rag.domain.namespaces import SourceNamespace, UnknownNamespaceError


def test_book_id_is_corpus_colon_source() -> None:
    """Book ids are ``corpus:source``."""
    namespace = SourceNamespace(corpus="knowledge", source="agentic-architectural-patterns")
    assert namespace.book_id() == "knowledge:agentic-architectural-patterns"


def test_entity_id_is_corpus_colon_source_colon_slug_type() -> None:
    """Entity ids are ``corpus:source:slug-type``."""
    namespace = SourceNamespace(corpus="knowledge", source="agentic-architectural-patterns")
    assert namespace.entity_id("agent", "concept") == (
        "knowledge:agentic-architectural-patterns:agent-concept"
    )


def test_entity_id_preserves_slug_hyphens() -> None:
    """A slug that already contains hyphens is not split by the id builder."""
    namespace = SourceNamespace(corpus="knowledge", source="agentic-architectural-patterns")
    assert namespace.entity_id("multi-agent-system", "concept") == (
        "knowledge:agentic-architectural-patterns:multi-agent-system-concept"
    )


def test_book_id_round_trips_through_parse() -> None:
    """``parse_book_id`` inverts ``book_id``."""
    namespace = SourceNamespace(corpus="knowledge", source="agentic-architectural-patterns")
    assert SourceNamespace.parse_book_id(namespace.book_id()) == namespace


def test_entity_id_round_trips_through_parse() -> None:
    """``parse_entity_id`` inverts ``entity_id`` back into namespace + slug + type."""
    namespace = SourceNamespace(corpus="knowledge", source="agentic-architectural-patterns")
    parsed_ns, slug, entity_type = SourceNamespace.parse_entity_id(
        namespace.entity_id("agent", "concept")
    )
    assert parsed_ns == namespace
    assert slug == "agent"
    assert entity_type == "concept"


def test_entity_id_round_trips_slug_with_hyphens() -> None:
    """Parsing recovers the full hyphenated slug, not just its last segment."""
    namespace = SourceNamespace(corpus="knowledge", source="agentic-architectural-patterns")
    parsed_ns, slug, entity_type = SourceNamespace.parse_entity_id(
        namespace.entity_id("multi-agent-system", "concept")
    )
    assert parsed_ns == namespace
    assert slug == "multi-agent-system"
    assert entity_type == "concept"


def test_same_slug_in_different_sources_produce_distinct_ids() -> None:
    """The same entity slug in two sources must yield different entity ids."""
    first = SourceNamespace(corpus="knowledge", source="source-a")
    second = SourceNamespace(corpus="knowledge", source="source-b")
    assert first.entity_id("agent", "concept") != second.entity_id("agent", "concept")


def test_same_slug_in_different_corpora_produce_distinct_ids() -> None:
    """The same slug is also separated across corpora."""
    first = SourceNamespace(corpus="knowledge", source="source-a")
    second = SourceNamespace(corpus="internal", source="source-a")
    assert first.entity_id("agent", "concept") != second.entity_id("agent", "concept")


def test_parse_book_id_rejects_malformed_ids() -> None:
    """Malformed book ids (missing or extra separators) raise."""
    with pytest.raises(UnknownNamespaceError):
        SourceNamespace.parse_book_id("no-separator")
    with pytest.raises(UnknownNamespaceError):
        SourceNamespace.parse_book_id("a:b:c")


def test_parse_entity_id_rejects_malformed_ids() -> None:
    """Malformed entity ids (wrong segment count, missing type) raise."""
    with pytest.raises(UnknownNamespaceError):
        SourceNamespace.parse_entity_id("corpus:source")
    with pytest.raises(UnknownNamespaceError):
        SourceNamespace.parse_entity_id("corpus:source:notypedash")
