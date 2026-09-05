"""Unit tests for the checkpoint version-dimensions model."""

from __future__ import annotations

import hashlib

import pytest

from book_graph_rag.config import Settings
from book_graph_rag.domain.checkpoint_models import VersionDimensions
from book_graph_rag.infrastructure.version_dimensions import compute_version_dimensions


def _source_version(pdf_bytes: bytes) -> str:
    return hashlib.sha256(pdf_bytes).hexdigest()[:16]


def test_version_dimensions_compute_stable() -> None:
    """Identical PDF bytes + identical settings produce the same source_version."""
    pdf_bytes = b"stable pdf content for source hash"

    def dims(content: bytes) -> VersionDimensions:
        return VersionDimensions(
            source_version=_source_version(content),
            pipeline_version="1.0.0",
            model_version="openai:gpt-4o-mini:2026-09-01",
            schema_version="1.0.0",
        )

    assert dims(pdf_bytes) == dims(pdf_bytes)
    assert dims(pdf_bytes).source_version != dims(b"different content").source_version


@pytest.fixture
def settings() -> Settings:
    return Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
            "pipeline_version": "1.0.0",
            "schema_version": "1.0.0",
            "graph_llm_model_date": "2026-09-01",
            "graph_llm_model_name": "gpt-4o-mini",
            "graph_llm_base_url": "https://api.openai.com/v1",
        }
    )


def test_compute_version_dimensions_matches_manual(settings: Settings) -> None:
    """compute_version_dimensions builds the four dimensions from bytes + Settings."""
    pdf_bytes = b"pdf bytes for hashing"
    expected = VersionDimensions(
        source_version=hashlib.sha256(pdf_bytes).hexdigest()[:16],
        pipeline_version=settings.pipeline_version,
        model_version=f"openai:{settings.graph_llm_model_name}:{settings.graph_llm_model_date}",
        schema_version=settings.schema_version,
    )

    result = compute_version_dimensions(pdf_bytes, settings)

    assert result == expected


def test_compute_version_dimensions_changes_with_pdf(settings: Settings) -> None:
    """A different PDF byte stream yields a different source_version."""
    a = compute_version_dimensions(b"a", settings)
    b = compute_version_dimensions(b"b", settings)

    assert a.source_version != b.source_version
    assert a.pipeline_version == b.pipeline_version
    assert a.schema_version == b.schema_version
