"""Production ``CandidateRetrievalPort`` backed by a Neo4j 5.23 vector index.

This adapter uses ``db.index.vector.queryNodes`` with cosine similarity. The
anchor embedding is read from the existing ``:Entity(embedding)`` property, so
callers must upsert entity embeddings before retrieval.
"""

from __future__ import annotations

from typing import Any, cast

from book_graph_rag.config import Settings
from book_graph_rag.domain.models import EntityType
from book_graph_rag.domain.s0_normalization import _namespace_from_id
from book_graph_rag.infrastructure._neo4j_vector_cypher import (
    _ENSURE_VECTOR_INDEX,
    _SET_ENTITY_EMBEDDING,
    _VECTOR_QUERY,
)
from book_graph_rag.ports.candidate_retrieval_port import (
    CandidateHit,
    CandidateRetrievalPort,
    CandidateRetrievalRequest,
)
from book_graph_rag.ports.embedding_provider_port import EmbeddingVector


class Neo4jVectorCandidateRetrieval(CandidateRetrievalPort):
    """Prod retrieval over a Neo4j native vector index on ``:Entity(embedding)``."""

    def __init__(self, driver: Any, settings: Settings) -> None:
        self._driver = driver
        self._settings = settings

    async def ensure_index(self) -> None:
        """Create the vector index idempotently if it does not exist."""
        async with self._driver.session() as session:
            await session.run(
                _ENSURE_VECTOR_INDEX,
                index_name=self._settings.vector_index_name,
                dim=self._settings.embedding_dim,
                similarity="cosine",
            )

    async def upsert_entity_embedding(
        self, entity_id: str, vector: EmbeddingVector
    ) -> None:
        """Write ``vector`` to the ``embedding`` property of ``:Entity {id}``."""
        async with self._driver.session() as session:
            await session.run(
                _SET_ENTITY_EMBEDDING,
                id=entity_id,
                vec=list(vector.values),
            )

    async def retrieve(self, request: CandidateRetrievalRequest) -> list[CandidateHit]:
        """Return top-k same-type active candidates above ``min_similarity``.

        The vector index query is executed with a larger candidate pool so that
        post-query filters (same type, active-only, exclude anchor) do not leave
        us with fewer results than ``top_k``.
        """
        raw_top_k = max(request.top_k + 50, request.top_k * 2)
        async with self._driver.session() as session:
            result = await session.run(
                _VECTOR_QUERY,
                index_name=self._settings.vector_index_name,
                anchor_id=request.anchor_id,
                raw_top_k=raw_top_k,
                top_k=request.top_k,
                min_sim=request.min_similarity,
                anchor_type=request.anchor_type,
            )
            hits: list[CandidateHit] = []
            async for record in result:
                candidate_id = record["candidate_id"]
                hits.append(
                    CandidateHit(
                        candidate_id=candidate_id,
                        cosine_similarity=float(record["cosine_similarity"]),
                        candidate_type=cast(EntityType, record["candidate_type"]),
                        candidate_namespace=_namespace_from_id(candidate_id),
                    )
                )
            return hits
