"""Tests for the catalog-backed ScopeResolver port/adapter (T-B.1).

Covers valid namespace resolution and focused invalid/unknown scope rejection.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from book_graph_rag.domain.mcp_security import InvalidScopeError, ScopeContext
from book_graph_rag.domain.namespaces import SourceNamespace
from book_graph_rag.infrastructure.catalog_loader import CatalogLoader
from book_graph_rag.infrastructure.catalog_scope_resolver import CatalogScopeResolver
from book_graph_rag.ports.scope_resolver_port import ScopeResolverPort

_VALID_CATALOG = """\
version: 1
corpora:
  knowledge:
    label: "Knowledge Library"
    sources:
      agentic-architectural-patterns:
        label: "Agentic Architectural Patterns"
        file: "data/libro_Agentic_Architectural_Patterns.pdf"
        status: active
      inactive-patterns:
        label: "Inactive Patterns"
        file: "data/inactive.pdf"
        status: inactive
"""


def _write_catalog(tmp_path: Path, content: str = _VALID_CATALOG) -> Path:
    path = tmp_path / "catalog.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def _resolver(tmp_path: Path) -> CatalogScopeResolver:
    return CatalogScopeResolver(CatalogLoader(_write_catalog(tmp_path)))


def test_resolver_implements_port(tmp_path: Path) -> None:
    """The adapter exposes the ScopeResolverPort contract."""
    resolver = _resolver(tmp_path)

    assert isinstance(resolver, ScopeResolverPort)


def test_resolve_known_active_source_returns_scope_context(tmp_path: Path) -> None:
    """A known active (corpus, source) resolves to a validated ScopeContext."""
    resolver = _resolver(tmp_path)

    scope = resolver.resolve("knowledge:agentic-architectural-patterns")

    assert isinstance(scope, ScopeContext)
    assert scope.source == SourceNamespace(
        corpus="knowledge", source="agentic-architectural-patterns"
    )
    assert scope.book_ids == ()
    assert scope.entity_types == ()
    assert scope.relationship_types == ()


def test_resolve_canonicalizes_optional_filters(tmp_path: Path) -> None:
    """Optional book/entity/relationship filters are sorted and deduplicated."""
    resolver = _resolver(tmp_path)

    scope = resolver.resolve(
        "knowledge:agentic-architectural-patterns",
        book_ids=("z", "a", "a"),
        entity_types=("Agent", "Pattern"),
        relationship_types=("relates_to",),
    )

    assert scope.book_ids == ("a", "z")
    assert scope.entity_types == ("Agent", "Pattern")
    assert scope.relationship_types == ("relates_to",)


def test_resolve_malformed_source_id_raises(tmp_path: Path) -> None:
    """A source id without exactly two non-empty parts is rejected."""
    resolver = _resolver(tmp_path)

    for bad in ("", "only-one-part", "a:b:c", "corpus:", ":source"):
        with pytest.raises(InvalidScopeError, match="(?i)scope|source"):
            resolver.resolve(bad)


def test_resolve_unknown_corpus_raises(tmp_path: Path) -> None:
    """An unknown corpus produces InvalidScopeError, not a domain ValueError."""
    resolver = _resolver(tmp_path)

    with pytest.raises(InvalidScopeError, match="(?i)unknown.*corpus"):
        resolver.resolve("unknown:agentic-architectural-patterns")


def test_resolve_unknown_source_raises(tmp_path: Path) -> None:
    """An unknown source inside a known corpus is rejected."""
    resolver = _resolver(tmp_path)

    with pytest.raises(InvalidScopeError, match="(?i)unknown.*source"):
        resolver.resolve("knowledge:missing-source")


def test_resolve_inactive_source_raises(tmp_path: Path) -> None:
    """A source that is not active cannot authorize scoped access."""
    resolver = _resolver(tmp_path)

    with pytest.raises(InvalidScopeError, match="(?i)inactive|not active"):
        resolver.resolve("knowledge:inactive-patterns")


def test_resolve_empty_book_id_filter_raises(tmp_path: Path) -> None:
    """Empty strings in filter tuples are caught by the domain scope contract."""
    resolver = _resolver(tmp_path)

    with pytest.raises(ValidationError, match="scope identifiers must be non-empty"):
        resolver.resolve(
            "knowledge:agentic-architectural-patterns",
            book_ids=("",),
        )
