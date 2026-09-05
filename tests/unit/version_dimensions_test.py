"""Unit tests for the checkpoint version-dimensions model."""

from __future__ import annotations

import hashlib

from book_graph_rag.domain.checkpoint_models import VersionDimensions


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
