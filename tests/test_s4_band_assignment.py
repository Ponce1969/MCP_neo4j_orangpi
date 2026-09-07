"""Tests for S4 confidence-band assignment (Slice D).

Band assignment is a pure, threshold-driven routing function.
"""

from __future__ import annotations

import pytest

from book_graph_rag.domain.resolution_models import ConfidenceBand, S3ContextSignals
from book_graph_rag.domain.s4_band_assignment import BandThresholds, assign_band


def _s3(
    mentions: float = 0.0,
    related: float = 0.0,
    desc: float = 0.0,
    conflict: bool = False,
) -> S3ContextSignals:
    return S3ContextSignals(
        mentions_jaccard=mentions,
        related_jaccard=related,
        description_overlap=desc,
        mentions_source_count=1,
        mentions_shared_count=0,
        related_neighbor_count=1,
        related_shared_count=0,
        conflict_flag=conflict,
    )


def test_band_thresholds_defaults() -> None:
    thresholds = BandThresholds()
    assert thresholds.high_cosine == pytest.approx(0.90)
    assert thresholds.high_context == pytest.approx(0.50)
    assert thresholds.medium_cosine == pytest.approx(0.80)
    assert thresholds.conflict_floor == pytest.approx(0.10)


def test_band_thresholds_rejects_non_monotonic_high_and_medium() -> None:
    with pytest.raises(ValueError, match="high_cosine"):
        BandThresholds(high_cosine=0.80, medium_cosine=0.80)

    with pytest.raises(ValueError, match="high_cosine"):
        BandThresholds(high_cosine=0.70, medium_cosine=0.80)


def test_assign_band_exact_short_circuits() -> None:
    """An S0 match always wins, even with terrible embedding/context scores."""
    thresholds = BandThresholds()
    terrible_s3 = _s3(mentions=0.0, related=0.0, desc=0.0, conflict=True)
    band = assign_band(s1_cosine=0.0, s3=terrible_s3, s0_match_field="alias", thresholds=thresholds)
    assert band == ConfidenceBand.EXACT


def test_assign_band_exact_id_match() -> None:
    thresholds = BandThresholds()
    band = assign_band(
        s1_cosine=0.95,
        s3=_s3(0.6, 0.6, 0.6),
        s0_match_field="id",
        thresholds=thresholds,
    )
    assert band == ConfidenceBand.EXACT


@pytest.mark.parametrize(
    ("cosine", "composite", "conflict", "expected_band"),
    [
        # boundary at exactly 0.90 must land in high
        (0.90, 0.50, False, ConfidenceBand.HIGH),
        # boundary at exactly 0.80 must land in medium
        (0.80, 0.50, False, ConfidenceBand.MEDIUM),
        # above high threshold but low context -> medium (not high)
        (0.95, 0.40, False, ConfidenceBand.MEDIUM),
        # above high threshold with conflict -> medium
        (0.95, 0.50, True, ConfidenceBand.MEDIUM),
        # between medium and high without high context -> medium
        (0.85, 0.60, False, ConfidenceBand.MEDIUM),
        # below medium -> low
        (0.79, 0.60, False, ConfidenceBand.LOW),
        # low cosine, high context still low
        (0.50, 0.90, False, ConfidenceBand.LOW),
        # exact medium boundary with conflict -> medium still (cosine >= 0.80)
        (0.80, 0.05, True, ConfidenceBand.MEDIUM),
        # conflict below medium -> low
        (0.75, 0.05, True, ConfidenceBand.LOW),
    ],
)
def test_assign_band_matrix(
    cosine: float,
    composite: float,
    conflict: bool,
    expected_band: ConfidenceBand,
) -> None:
    thresholds = BandThresholds()
    s3 = _s3(mentions=composite, related=composite, desc=composite, conflict=conflict)
    band = assign_band(s1_cosine=cosine, s3=s3, s0_match_field="none", thresholds=thresholds)
    assert band == expected_band


def test_assign_band_uses_custom_thresholds() -> None:
    thresholds = BandThresholds(high_cosine=0.85, high_context=0.40, medium_cosine=0.70)
    s3 = _s3(mentions=0.45, related=0.45, desc=0.45)
    band = assign_band(s1_cosine=0.85, s3=s3, s0_match_field="none", thresholds=thresholds)
    assert band == ConfidenceBand.HIGH
