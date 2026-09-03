"""Tests for infrastructure.catalog_loader (catalog YAML → domain Catalog)."""

from __future__ import annotations

from pathlib import Path

import pytest

from book_graph_rag.domain.namespaces import Catalog, SourceNamespace, UnknownNamespaceError
from book_graph_rag.infrastructure.catalog_loader import CatalogLoader, CatalogLoadError

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
"""


def _write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "catalog.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_load_returns_domain_catalog(tmp_path: Path) -> None:
    """Valid YAML parses into the domain Catalog model."""
    catalog = CatalogLoader(_write(tmp_path, _VALID_CATALOG)).load()

    assert isinstance(catalog, Catalog)
    assert catalog.version == 1
    assert set(catalog.corpora) == {"knowledge"}
    corpus = catalog.corpora["knowledge"]
    assert corpus.label == "Knowledge Library"
    source = corpus.sources["agentic-architectural-patterns"]
    assert source.label == "Agentic Architectural Patterns"
    assert source.file == "data/libro_Agentic_Architectural_Patterns.pdf"
    assert source.status == "active"


def test_load_resolves_known_namespace(tmp_path: Path) -> None:
    """A known (corpus, source) resolves to the expected namespace."""
    namespace = (
        CatalogLoader(_write(tmp_path, _VALID_CATALOG))
        .load()
        .resolve_source("knowledge", "agentic-architectural-patterns")
    )

    assert namespace == SourceNamespace(corpus="knowledge", source="agentic-architectural-patterns")


def test_load_unknown_corpus_raises(tmp_path: Path) -> None:
    """An unknown corpus fails fast with UnknownNamespaceError."""
    catalog = CatalogLoader(_write(tmp_path, _VALID_CATALOG)).load()

    with pytest.raises(UnknownNamespaceError, match="Unknown corpus"):
        catalog.resolve_source("unknown", "agentic-architectural-patterns")


def test_load_unknown_source_raises(tmp_path: Path) -> None:
    """An unknown source inside a known corpus fails fast."""
    catalog = CatalogLoader(_write(tmp_path, _VALID_CATALOG)).load()

    with pytest.raises(UnknownNamespaceError, match="Unknown source"):
        catalog.resolve_source("knowledge", "missing-source")


def test_load_missing_file_raises(tmp_path: Path) -> None:
    """A missing catalog file raises CatalogLoadError."""
    with pytest.raises(CatalogLoadError, match="Cannot read"):
        CatalogLoader(tmp_path / "missing.yaml").load()


def test_load_invalid_yaml_raises(tmp_path: Path) -> None:
    """Unparsable YAML raises CatalogLoadError."""
    with pytest.raises(CatalogLoadError, match="Invalid YAML"):
        CatalogLoader(_write(tmp_path, "version: [unclosed")).load()


def test_load_non_mapping_yaml_raises(tmp_path: Path) -> None:
    """A YAML document that is not a mapping raises CatalogLoadError."""
    with pytest.raises(CatalogLoadError, match="mapping"):
        CatalogLoader(_write(tmp_path, "- just\n- a\n- list\n")).load()
