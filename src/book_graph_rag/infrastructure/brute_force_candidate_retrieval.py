"""In-memory cosine retrieval adapter for evaluation harnesses.

This implementation of ``CandidateRetrievalPort`` keeps an embedding cache in
process memory and scores candidates with brute-force cosine similarity. It is
deterministic, dependency-light, and used on Windows dev/test; production uses
``Neo4jVectorCandidateRetrieval`` instead.
"""

from __future__ import annotations

import logging
import math

from book_graph_rag.domain.models import EntityType
from book_graph_rag.ports.candidate_retrieval_port import (
    CandidateHit,
    CandidateRetrievalPort,
    CandidateRetrievalRequest,
)
from book_graph_rag.ports.embedding_provider_port import EmbeddingVector

logger = logging.getLogger(__name__)


def cosine_similarity(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Cosine similarity of two equal-dimension vectors.

    Returns ``0.0`` if either vector has zero norm, avoiding division by zero.
    """
    if len(a) != len(b):
        raise ValueError(f"vectors must have the same dimension: {len(a)} != {len(b)}")

    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    # Clamp to [-1, 1]: floating-point rounding can produce 1.0000000000000002.
    return max(-1.0, min(1.0, dot / (norm_a * norm_b)))


class BruteForceCandidateRetrieval(CandidateRetrievalPort):
    """Eval-side in-memory top-k cosine retrieval over a precomputed cache."""

    def __init__(self) -> None:
        self._vectors: dict[str, EmbeddingVector] = {}
        self._metadata: dict[str, tuple[EntityType, str]] = {}

    async def upsert_entity_embedding(
        self, entity_id: str, vector: EmbeddingVector
    ) -> None:
        """Store or update the embedding vector for ``entity_id``."""
        self._vectors[entity_id] = vector

    def upsert_entity_metadata(
        self, entity_id: str, entity_type: EntityType, namespace: str
    ) -> None:
        """Store type/namespace metadata needed for S2 filtering.

        This is a concrete helper, not part of the abstract port contract,
        because metadata semantics differ across adapters (the Neo4j adapter
        reads them from the graph).
        """
        self._metadata[entity_id] = (entity_type, namespace)

    async def retrieve(self, request: CandidateRetrievalRequest) -> list[CandidateHit]:
        """Return top-k same-type candidates above ``min_similarity``."""
        anchor_vec = self._vectors.get(request.anchor_id)
        if anchor_vec is None:
            return []

        hits: list[CandidateHit] = []
        for candidate_id, vector in self._vectors.items():
            if candidate_id == request.anchor_id:
                continue
            metadata = self._metadata.get(candidate_id)
            if metadata is None:
                continue
            candidate_type, candidate_namespace = metadata
            if candidate_type != request.anchor_type:
                continue
            score = cosine_similarity(anchor_vec.values, vector.values)
            if score < request.min_similarity:
                continue
            hits.append(
                CandidateHit(
                    candidate_id=candidate_id,
                    cosine_similarity=score,
                    candidate_type=candidate_type,
                    candidate_namespace=candidate_namespace,
                )
            )

        hits.sort(key=lambda h: h.cosine_similarity, reverse=True)
        return hits[: request.top_k]

    async def ensure_index(self) -> None:
        """No-op for the in-memory eval adapter."""
        logger.debug("brute-force ensure_index is an eval no-op")
