"""Unit tests for the staleness predicate."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from book_graph_rag.domain.checkpoint_models import Checkpoint, CheckpointStatus, VersionDimensions
from book_graph_rag.domain.checkpoint_state_machine import is_stale


def _dims(**overrides: str) -> VersionDimensions:
    fields = {
        "source_version": "src123",
        "pipeline_version": "1.0.0",
        "model_version": "openai:gpt-4o-mini:2026-09-01",
        "schema_version": "1.0.0",
    }
    fields.update(overrides)
    return VersionDimensions(**fields)


def _checkpoint(versions: VersionDimensions) -> Checkpoint:
    return Checkpoint(
        source_id="agentic-patterns:test",
        chunk_index=0,
        status=CheckpointStatus.PROCESSED,
        versions=versions,
        updated_at=datetime.now(UTC),
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_version", "other-src"),
        ("pipeline_version", "2.0.0"),
        ("model_version", "openai:gpt-4o:2026-09-01"),
        ("schema_version", "2.0.0"),
    ],
)
def test_is_stale_each_dimension(field: str, value: str) -> None:
    """A mismatch on any single dimension marks the checkpoint stale."""
    current = _dims()
    record_versions = _dims(**{field: value})
    assert is_stale(_checkpoint(record_versions), current) is True


def test_is_stale_no_mismatch() -> None:
    """Identical dimensions are not stale."""
    dims = _dims()
    assert is_stale(_checkpoint(dims), dims) is False


def test_is_stale_multi_dimension() -> None:
    """Multiple mismatches still return stale."""
    current = _dims()
    record_versions = _dims(
        source_version="other-src",
        pipeline_version="2.0.0",
        schema_version="2.0.0",
    )
    assert is_stale(_checkpoint(record_versions), current) is True
