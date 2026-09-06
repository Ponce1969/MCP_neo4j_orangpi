"""Property: the staleness predicate is consistent across arbitrary dimensions.

The predicate ``is_stale`` must agree with the structural definition: a
checkpoint is stale iff at least one version dimension differs from the current
run's dimensions. This property holds regardless of which dimensions differ or
how many dimensions differ, and it is independent of checkpoint status.
"""

from __future__ import annotations

from datetime import UTC, datetime

from hypothesis import given, settings
from hypothesis import strategies as st

from book_graph_rag.domain.checkpoint_models import (
    Checkpoint,
    CheckpointStatus,
    VersionDimensions,
)
from book_graph_rag.domain.checkpoint_state_machine import is_stale

_VERSION_STRATEGY = st.text(
    alphabet=st.characters(
        codec="utf-8",
        blacklist_characters="\x00",
    ),
    min_size=1,
    max_size=32,
)


@given(
    source_version=_VERSION_STRATEGY,
    pipeline_version=_VERSION_STRATEGY,
    model_version=_VERSION_STRATEGY,
    schema_version=_VERSION_STRATEGY,
    current_source_version=_VERSION_STRATEGY,
    current_pipeline_version=_VERSION_STRATEGY,
    current_model_version=_VERSION_STRATEGY,
    current_schema_version=_VERSION_STRATEGY,
)
@settings(max_examples=100, deadline=None)
def test_staleness_consistent_with_dimension_equality(
    source_version: str,
    pipeline_version: str,
    model_version: str,
    schema_version: str,
    current_source_version: str,
    current_pipeline_version: str,
    current_model_version: str,
    current_schema_version: str,
) -> None:
    """``is_stale`` is True exactly when at least one dimension differs."""
    record_versions = VersionDimensions(
        source_version=source_version,
        pipeline_version=pipeline_version,
        model_version=model_version,
        schema_version=schema_version,
    )
    current_versions = VersionDimensions(
        source_version=current_source_version,
        pipeline_version=current_pipeline_version,
        model_version=current_model_version,
        schema_version=current_schema_version,
    )
    checkpoint = Checkpoint(
        source_id="prop:stale",
        chunk_index=0,
        status=CheckpointStatus.PROCESSED,
        versions=record_versions,
        updated_at=datetime.now(UTC),
    )

    expected = (
        source_version != current_source_version
        or pipeline_version != current_pipeline_version
        or model_version != current_model_version
        or schema_version != current_schema_version
    )

    assert is_stale(checkpoint, current_versions) is expected


@given(
    base=st.builds(
        VersionDimensions,
        source_version=_VERSION_STRATEGY,
        pipeline_version=_VERSION_STRATEGY,
        model_version=_VERSION_STRATEGY,
        schema_version=_VERSION_STRATEGY,
    ),
)
@settings(max_examples=50, deadline=None)
def test_equal_dimensions_are_never_stale(base: VersionDimensions) -> None:
    """A checkpoint whose dimensions exactly match the current run is not stale."""
    checkpoint = Checkpoint(
        source_id="prop:stale",
        chunk_index=0,
        status=CheckpointStatus.PROCESSED,
        versions=base,
        updated_at=datetime.now(UTC),
    )

    assert is_stale(checkpoint, base) is False

