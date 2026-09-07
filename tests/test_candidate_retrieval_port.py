"""Tests for the CandidateRetrievalPort domain contract."""

from __future__ import annotations

import pytest

from book_graph_rag.ports.candidate_retrieval_port import (
    CandidateHit,
    CandidateRetrievalPort,
    CandidateRetrievalRequest,
)


def test_candidate_hit_is_frozen_value() -> None:
    """A hit is an immutable retrieval result."""
    hit = CandidateHit(
        candidate_id="book:ch1:agent-beta",
        cosine_similarity=0.95,
        candidate_type="agent",
        candidate_namespace="book:ch1",
    )

    assert hit.candidate_id == "book:ch1:agent-beta"
    assert hit.cosine_similarity == pytest.approx(0.95)
    assert hit.candidate_type == "agent"
    assert hit.candidate_namespace == "book:ch1"


def test_candidate_retrieval_request_validates_min_similarity_range() -> None:
    """min_similarity is a cosine score and must live in [0, 1]."""
    with pytest.raises(ValueError, match="min_similarity"):
        CandidateRetrievalRequest(
            anchor_id="a",
            anchor_text="A",
            anchor_type="agent",
            anchor_namespace="ns",
            top_k=5,
            min_similarity=1.5,
        )


def test_candidate_retrieval_port_is_abstract() -> None:
    """The port cannot be instantiated directly."""
    with pytest.raises(TypeError, match="abstract"):
        CandidateRetrievalPort()  # type: ignore[abstract]
