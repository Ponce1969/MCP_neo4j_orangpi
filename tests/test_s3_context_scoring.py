"""Tests for S3 context scoring (Slice D).

S3 validates candidate pairs with relationship and description overlap signals.
All helpers are pure; no model, no I/O.
"""

from __future__ import annotations

import pytest

from book_graph_rag.domain.s3_context_scoring import (
    description_overlap,
    mentions_jaccard,
    related_jaccard,
    s3_context_score,
)


def test_mentions_jaccard_empty_sets() -> None:
    """Empty mention sets have zero overlap by definition."""
    jaccard, shared, union = mentions_jaccard(set(), set())
    assert jaccard == pytest.approx(0.0)
    assert shared == 0
    assert union == 0


def test_mentions_jaccard_identical_sets() -> None:
    sources = {"src:a", "src:b", "src:c"}
    jaccard, shared, union = mentions_jaccard(sources, sources)
    assert jaccard == pytest.approx(1.0)
    assert shared == 3
    assert union == 3


def test_mentions_jaccard_disjoint_sets() -> None:
    a = {"src:a", "src:b"}
    b = {"src:c", "src:d"}
    jaccard, shared, union = mentions_jaccard(a, b)
    assert jaccard == pytest.approx(0.0)
    assert shared == 0
    assert union == 4


def test_mentions_jaccard_partial_overlap() -> None:
    a = {"src:a", "src:b", "src:c"}
    b = {"src:b", "src:c", "src:d"}
    jaccard, shared, union = mentions_jaccard(a, b)
    assert jaccard == pytest.approx(2 / 4)
    assert shared == 2
    assert union == 4


def test_mentions_jaccard_is_symmetric() -> None:
    a = {"src:a", "src:b", "src:c"}
    b = {"src:b", "src:d"}
    left = mentions_jaccard(a, b)
    right = mentions_jaccard(b, a)
    assert left == right


def test_related_jaccard_same_contract_as_mentions() -> None:
    """The related-neighbor Jaccard helper uses the same pure set math."""
    a = {"ent:1", "ent:2"}
    b = {"ent:2", "ent:3"}
    jaccard, shared, union = related_jaccard(a, b)
    assert jaccard == pytest.approx(1 / 3)
    assert shared == 1
    assert union == 3


def test_description_overlap_empty_descriptions() -> None:
    assert description_overlap("", "") == pytest.approx(0.0)


def test_description_overlap_identical_descriptions() -> None:
    assert description_overlap("LangGraph agent framework", "LangGraph agent framework") == pytest.approx(1.0)


def test_description_overlap_order_does_not_matter() -> None:
    """Token-set Jaccard ignores word order."""
    a = "LangGraph agent framework"
    b = "framework agent LangGraph"
    assert description_overlap(a, b) == pytest.approx(1.0)


def test_description_overlap_partial_overlap() -> None:
    a = "LangGraph agent framework"
    b = "framework for LangGraph agent"
    # Shared: {langgraph, agent, framework} ∩ {framework, for, langgraph, agent} = 3
    # Union: 4
    assert description_overlap(a, b) == pytest.approx(0.75)


def test_description_overlap_disjoint() -> None:
    assert description_overlap("foo bar", "baz qux") == pytest.approx(0.0)


def test_description_overlap_case_and_punctuation_normalized() -> None:
    a = "LangGraph: agent framework."
    b = "langgraph agent, framework"
    assert description_overlap(a, b) == pytest.approx(1.0)


def test_s3_context_score_composite_is_mean() -> None:
    s3 = s3_context_score(
        mentions_j=0.5,
        related_j=0.5,
        desc_o=0.5,
        cosine=0.85,
    )
    assert s3.mentions_jaccard == pytest.approx(0.5)
    assert s3.related_jaccard == pytest.approx(0.5)
    assert s3.description_overlap == pytest.approx(0.5)
    assert (s3.mentions_jaccard + s3.related_jaccard + s3.description_overlap) / 3 == pytest.approx(0.5)


@pytest.mark.parametrize(
    ("mentions_j", "related_j", "desc_o", "cosine", "expected_conflict"),
    [
        (0.0, 0.0, 0.0, 0.85, True),   # composite 0.0 < 0.1, cosine >= 0.80
        (0.05, 0.05, 0.05, 0.80, True),  # composite 0.05 < 0.1, cosine >= 0.80
        (0.0, 0.0, 0.0, 0.79, False),  # composite < 0.1 but cosine < 0.80
        (0.2, 0.2, 0.2, 0.85, False),  # composite >= 0.1
        (0.05, 0.05, 0.05, 0.00, False),  # cosine below threshold
    ],
)
def test_s3_conflict_flag_logic(
    mentions_j: float,
    related_j: float,
    desc_o: float,
    cosine: float,
    expected_conflict: bool,
) -> None:
    s3 = s3_context_score(mentions_j, related_j, desc_o, cosine)
    assert s3.conflict_flag is expected_conflict


def test_s3_context_score_populates_counts() -> None:
    s3 = s3_context_score(
        mentions_j=0.5,
        related_j=0.5,
        desc_o=0.5,
        cosine=0.85,
        mentions_source_count=10,
        mentions_shared_count=5,
        related_neighbor_count=8,
        related_shared_count=4,
    )
    assert s3.mentions_source_count == 10
    assert s3.mentions_shared_count == 5
    assert s3.related_neighbor_count == 8
    assert s3.related_shared_count == 4
