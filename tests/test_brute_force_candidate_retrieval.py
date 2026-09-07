"""Tests for BruteForceCandidateRetrieval in-memory cosine adapter."""

from __future__ import annotations

import pytest

from book_graph_rag.infrastructure.brute_force_candidate_retrieval import (
    BruteForceCandidateRetrieval,
    cosine_similarity,
)
from book_graph_rag.ports.candidate_retrieval_port import CandidateRetrievalRequest
from book_graph_rag.ports.embedding_provider_port import EmbeddingVector


def _vec(values: tuple[float, ...]) -> EmbeddingVector:
    return EmbeddingVector(values=values, model_id="fake")


async def test_retrieve_filters_by_type_and_returns_hits() -> None:
    """Only same-type candidates above the similarity floor are returned."""
    adapter = BruteForceCandidateRetrieval()
    await adapter.upsert_entity_embedding("anchor", _vec((1.0, 0.0, 0.0)))
    adapter.upsert_entity_metadata("anchor", "agent", "book:ch1")

    await adapter.upsert_entity_embedding("same-type-a", _vec((0.9, 0.1, 0.0)))
    adapter.upsert_entity_metadata("same-type-a", "agent", "book:ch1")
    await adapter.upsert_entity_embedding("same-type-b", _vec((0.8, 0.2, 0.0)))
    adapter.upsert_entity_metadata("same-type-b", "agent", "book:ch1")
    await adapter.upsert_entity_embedding("same-type-low", _vec((0.1, 0.9, 0.0)))
    adapter.upsert_entity_metadata("same-type-low", "agent", "book:ch1")
    await adapter.upsert_entity_embedding("different-type", _vec((0.95, 0.05, 0.0)))
    adapter.upsert_entity_metadata("different-type", "concept", "book:ch1")

    request = CandidateRetrievalRequest(
        anchor_id="anchor",
        anchor_text="Anchor",
        anchor_type="agent",
        anchor_namespace="book:ch1",
        top_k=10,
        min_similarity=0.5,
    )
    hits = await adapter.retrieve(request)

    ids = {h.candidate_id for h in hits}
    assert ids == {"same-type-a", "same-type-b"}
    assert all(h.candidate_type == "agent" for h in hits)


async def test_retrieve_respects_top_k() -> None:
    """The result is truncated to ``top_k`` after sorting by cosine."""
    adapter = BruteForceCandidateRetrieval()
    await adapter.upsert_entity_embedding("anchor", _vec((1.0, 0.0)))
    adapter.upsert_entity_metadata("anchor", "agent", "ns")

    for i in range(5):
        cid = f"cand-{i}"
        await adapter.upsert_entity_embedding(cid, _vec((0.99 - i * 0.05, 0.01 + i * 0.05)))
        adapter.upsert_entity_metadata(cid, "agent", "ns")

    request = CandidateRetrievalRequest(
        anchor_id="anchor",
        anchor_text="Anchor",
        anchor_type="agent",
        anchor_namespace="ns",
        top_k=2,
        min_similarity=0.0,
    )
    hits = await adapter.retrieve(request)

    assert len(hits) == 2
    assert hits[0].cosine_similarity > hits[1].cosine_similarity
    assert hits[0].candidate_id == "cand-0"


async def test_identical_vectors_yield_cosine_one() -> None:
    """Cosine of identical normalized vectors is exactly 1.0."""
    adapter = BruteForceCandidateRetrieval()
    await adapter.upsert_entity_embedding("anchor", _vec((1.0, 0.0)))
    adapter.upsert_entity_metadata("anchor", "agent", "ns")
    await adapter.upsert_entity_embedding("duplicate", _vec((1.0, 0.0)))
    adapter.upsert_entity_metadata("duplicate", "agent", "ns")

    request = CandidateRetrievalRequest(
        anchor_id="anchor",
        anchor_text="Anchor",
        anchor_type="agent",
        anchor_namespace="ns",
        top_k=5,
        min_similarity=0.0,
    )
    hits = await adapter.retrieve(request)

    assert len(hits) == 1
    assert hits[0].candidate_id == "duplicate"
    assert hits[0].cosine_similarity == pytest.approx(1.0)


async def test_retrieve_excludes_anchor_itself() -> None:
    """The anchor entity never appears in its own candidate list."""
    adapter = BruteForceCandidateRetrieval()
    await adapter.upsert_entity_embedding("anchor", _vec((1.0, 0.0)))
    adapter.upsert_entity_metadata("anchor", "agent", "ns")

    request = CandidateRetrievalRequest(
        anchor_id="anchor",
        anchor_text="Anchor",
        anchor_type="agent",
        anchor_namespace="ns",
        top_k=5,
        min_similarity=0.0,
    )
    hits = await adapter.retrieve(request)

    assert hits == []


async def test_retrieve_returns_empty_when_anchor_missing() -> None:
    """If the anchor vector was never upserted, retrieval returns no hits."""
    adapter = BruteForceCandidateRetrieval()

    request = CandidateRetrievalRequest(
        anchor_id="missing",
        anchor_text="Missing",
        anchor_type="agent",
        anchor_namespace="ns",
        top_k=5,
        min_similarity=0.0,
    )
    hits = await adapter.retrieve(request)

    assert hits == []


async def test_ensure_index_is_no_op() -> None:
    """The eval adapter's ensure_index is a no-op and does not raise."""
    adapter = BruteForceCandidateRetrieval()
    await adapter.ensure_index()


def test_cosine_similarity_of_orthogonal_vectors() -> None:
    """Orthogonal vectors have zero cosine similarity."""
    assert cosine_similarity((1.0, 0.0), (0.0, 1.0)) == pytest.approx(0.0)


def test_cosine_similarity_requires_same_dimension() -> None:
    """Mismatched vector dimensions raise a clear error."""
    with pytest.raises(ValueError, match="dimension"):
        cosine_similarity((1.0, 0.0), (1.0, 0.0, 0.0))
