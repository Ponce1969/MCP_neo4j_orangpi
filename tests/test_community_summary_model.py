"""Tests for CommunitySummary domain model (PR1 Foundation)."""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from book_graph_rag.domain.models import CommunitySummary


def _expected_id(level: int, entity_ids: list[str]) -> str:
    """Mirror the stable-id algorithm from the model."""
    key = f"{level}:{','.join(sorted(entity_ids))}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def test_community_summary_can_be_created() -> None:
    """Minimal construction produces the expected id."""
    entity_ids = ["pattern-a", "agent-b"]
    summary = CommunitySummary(
        id="",
        level=0,
        summary="A root community summary.",
        entity_ids=entity_ids,
    )

    assert summary.id == _expected_id(0, entity_ids)
    assert summary.level == 0
    assert summary.summary == "A root community summary."
    assert summary.entity_ids == entity_ids
    assert summary.parent_id is None


def test_community_summary_id_is_stable() -> None:
    """Same membership and level produce the same id regardless of input order."""
    summary = CommunitySummary(
        id="",
        level=1,
        summary="Themes around agents.",
        entity_ids=["c", "a", "b"],
        parent_id="root",
    )

    assert summary.id == _expected_id(1, ["c", "a", "b"])
    assert summary.id == _expected_id(1, ["a", "b", "c"])


def test_community_summary_is_frozen() -> None:
    """CommunitySummary is immutable after construction."""
    summary = CommunitySummary(
        id="",
        level=0,
        summary="Root.",
        entity_ids=["a"],
    )

    with pytest.raises(ValidationError):
        summary.summary = "Mutated"


def test_community_summary_level_bounds() -> None:
    """Level must be between 0 and 3 inclusive."""
    with pytest.raises(ValidationError):
        CommunitySummary(id="", level=-1, summary="Invalid.", entity_ids=["a"])

    with pytest.raises(ValidationError):
        CommunitySummary(id="", level=4, summary="Invalid.", entity_ids=["a"])


def test_community_summary_level_zero_has_no_parent() -> None:
    """Level 0 must have parent_id=None."""
    with pytest.raises(ValidationError):
        CommunitySummary(
            id="",
            level=0,
            summary="Invalid.",
            entity_ids=["a"],
            parent_id="some-parent",
        )


def test_community_summary_non_zero_requires_parent() -> None:
    """Levels 1-3 must have a parent_id."""
    with pytest.raises(ValidationError):
        CommunitySummary(
            id="",
            level=1,
            summary="Invalid.",
            entity_ids=["a"],
        )


def test_community_summary_entity_ids_must_be_non_empty() -> None:
    """A community must contain at least one entity."""
    with pytest.raises(ValidationError):
        CommunitySummary(
            id="",
            level=0,
            summary="Invalid.",
            entity_ids=[],
        )


def test_community_summary_includes_level_and_entities_in_id() -> None:
    """Different levels or entity sets produce different ids."""
    s0 = CommunitySummary(id="", level=0, summary="Root.", entity_ids=["a"])
    s1 = CommunitySummary(id="", level=1, summary="Child.", entity_ids=["a"], parent_id=s0.id)
    s2 = CommunitySummary(
        id="", level=1, summary="Sibling.", entity_ids=["a", "b"], parent_id=s0.id
    )

    assert s0.id != s1.id
    assert s1.id != s2.id


def test_community_summary_rejects_manually_mismatched_id() -> None:
    """Providing an id that does not match the computed hash is rejected."""
    with pytest.raises(ValidationError):
        CommunitySummary(
            id="manual-id",
            level=0,
            summary="Invalid.",
            entity_ids=["a"],
        )
