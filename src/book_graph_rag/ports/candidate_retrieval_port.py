"""Domain port for candidate entity retrieval.

This port abstracts whether retrieval happens via in-memory brute-force cosine
(evaluation) or a Neo4j native vector index (production). Both paths return the
same ``CandidateHit`` shape.
"""

from __future__ import annotations

import abc

from pydantic import BaseModel, ConfigDict, Field

from book_graph_rag.domain.models import EntityType
from book_graph_rag.ports.embedding_provider_port import EmbeddingVector


class CandidateHit(BaseModel):
    """One candidate entity retrieved for an anchor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str
    cosine_similarity: float = Field(ge=0.0, le=1.0)
    candidate_type: EntityType
    candidate_namespace: str


class CandidateRetrievalRequest(BaseModel):
    """Pure-domain request to retrieve candidate entities for an anchor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    anchor_id: str
    anchor_text: str
    anchor_type: EntityType
    anchor_namespace: str
    top_k: int = Field(ge=1, le=100)
    min_similarity: float = Field(ge=0.0, le=1.0)


class CandidateRetrievalPort(abc.ABC):
    """Contract for retrieving similar candidate entities for a given anchor."""

    @abc.abstractmethod
    async def retrieve(self, request: CandidateRetrievalRequest) -> list[CandidateHit]:
        """Return up to ``top_k`` candidates above ``min_similarity``."""

    @abc.abstractmethod
    async def upsert_entity_embedding(
        self, entity_id: str, vector: EmbeddingVector
    ) -> None:
        """Store or update the embedding vector for ``entity_id``."""

    @abc.abstractmethod
    async def ensure_index(self) -> None:
        """Ensure the backing vector index exists.

        On the brute-force adapter this is a no-op; on the Neo4j adapter it
        creates the native vector index.
        """
