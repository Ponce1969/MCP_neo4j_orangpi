"""Pin the repository ``catalog.yaml`` knowledge corpus and its statuses.

The catalog is authoritative for scope resolution: inactive sources are valid
catalog entries but must not be scope-addressable. These tests load the real
repo-root ``catalog.yaml`` so that adding a PDF to ``data/`` without declaring
it — or flipping a status — fails loudly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from book_graph_rag.domain.mcp_security import InvalidScopeError
from book_graph_rag.domain.namespaces import Catalog
from book_graph_rag.infrastructure.catalog_loader import CatalogLoader
from book_graph_rag.infrastructure.catalog_scope_resolver import CatalogScopeResolver

CATALOG_PATH = Path("catalog.yaml")


def _load_catalog() -> Catalog:
    return CatalogLoader(CATALOG_PATH).load()


def test_catalog_declares_three_knowledge_sources() -> None:
    """The knowledge corpus documents exactly the three expected sources."""
    catalog = _load_catalog()

    assert catalog.version == 1
    assert set(catalog.corpora) == {"knowledge"}
    sources = catalog.corpora["knowledge"].sources
    assert set(sources) == {
        "agentic-architectural-patterns",
        "graphrag-agentic",
        "essential-graphrag",
    }


def test_graphrag_sources_are_inactive() -> None:
    """The two new GraphRAG PDFs are declared but not yet active."""
    sources = _load_catalog().corpora["knowledge"].sources

    assert sources["graphrag-agentic"].status == "inactive"
    assert sources["essential-graphrag"].status == "inactive"


def test_agentic_patterns_source_stays_active() -> None:
    """The pre-existing source remains active."""
    sources = _load_catalog().corpora["knowledge"].sources

    assert sources["agentic-architectural-patterns"].status == "active"


def test_inactive_graphrag_source_is_not_scope_addressable() -> None:
    """Inactive sources are valid catalog entries but rejected by the resolver."""
    resolver = CatalogScopeResolver(CatalogLoader(CATALOG_PATH))

    with pytest.raises(InvalidScopeError, match="(?i)not active|inactive"):
        resolver.resolve("knowledge:graphrag-agentic")
