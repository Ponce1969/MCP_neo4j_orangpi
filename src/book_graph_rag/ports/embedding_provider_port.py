"""Domain port for embedding providers.

The port hides the concrete sentence-transformers backend from the domain and
application layers. Implementations live in ``infrastructure/``.
"""

from __future__ import annotations

import abc

from pydantic import BaseModel, ConfigDict


class EmbeddingVector(BaseModel):
    """One embedding vector as an immutable tuple of floats."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    values: tuple[float, ...]
    model_id: str


class EmbeddingBatch(BaseModel):
    """Result of embedding an ordered batch of texts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    vectors: list[EmbeddingVector]


class EmbeddingRequest(BaseModel):
    """Pure-domain request to embed one or more texts with a specific model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    texts: tuple[str, ...]
    model_id: str


class EmbeddingProviderPort(abc.ABC):
    """Contract for embedding text into dense vectors.

    Implementations are responsible for model caching, I/O threading, and any
    adapter-specific configuration; the domain only sees the models above.
    """

    @abc.abstractmethod
    async def embed(self, request: EmbeddingRequest) -> EmbeddingBatch:
        """Embed ``request.texts`` and return one vector per text in order."""

    @abc.abstractmethod
    def model_dim(self, model_id: str) -> int:
        """Return the embedding dimension reported by ``model_id``."""
