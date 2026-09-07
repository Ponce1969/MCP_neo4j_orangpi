"""Local sentence-transformers implementation of ``EmbeddingProviderPort``.

The heavy ``sentence_transformers`` import is deferred until a model actually
needs to be loaded, keeping module-import time fast and unit tests without the
dependency runnable. Production callers use the default model loader; tests can
inject a fake ``_encode_sync`` and ``_model_loader`` for deterministic vectors.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from book_graph_rag.config import Settings
from book_graph_rag.ports.embedding_provider_port import (
    EmbeddingBatch,
    EmbeddingProviderPort,
    EmbeddingRequest,
    EmbeddingVector,
)

logger = logging.getLogger(__name__)


class SentenceTransformerAdapter(EmbeddingProviderPort):
    """Embeds texts using a local ``sentence-transformers`` model.

    - Loads from ``settings.embedding_local_path / model_id`` when present,
      otherwise falls back to the Hugging Face hub identifier.
    - Caches loaded models per process.
    - Runs the synchronous ``encode`` call off the asyncio event loop.
    - ``model_dim`` prefers ``get_embedding_dimension()`` and falls back to
      ``get_sentence_embedding_dimension()`` for compatibility.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        encode_sync: Any | None = None,
        model_loader: Any | None = None,
    ) -> None:
        self._settings = settings
        self._models: dict[str, Any] = {}
        self._encode_sync = (
            encode_sync if encode_sync is not None else self._default_encode_sync
        )
        self._model_loader = (
            model_loader if model_loader is not None else self._default_model_loader
        )

    async def embed(self, request: EmbeddingRequest) -> EmbeddingBatch:
        """Embed ``request.texts`` using the requested model."""
        model = self._load_model(request.model_id)
        vectors = await asyncio.to_thread(self._encode_sync, model, request.texts)
        return EmbeddingBatch(
            model_id=request.model_id,
            vectors=[
                EmbeddingVector(values=tuple(v), model_id=request.model_id) for v in vectors
            ],
        )

    def model_dim(self, model_id: str) -> int:
        """Return the embedding dimension reported by ``model_id``."""
        model = self._load_model(model_id)
        dim_fn: Any = getattr(model, "get_embedding_dimension", None)
        if dim_fn is None:
            dim_fn = model.get_sentence_embedding_dimension
        return int(dim_fn())

    def _load_model(self, model_id: str) -> Any:
        """Return a cached model, loading it on first use."""
        if model_id in self._models:
            return self._models[model_id]
        model = self._model_loader(model_id)
        self._models[model_id] = model
        return model

    def _default_model_loader(self, model_id: str) -> Any:
        """Load a SentenceTransformer model from local path or HF hub."""
        from sentence_transformers import SentenceTransformer

        local_path = self._settings.embedding_local_path
        if local_path:
            path = Path(local_path) / model_id
            if path.exists():
                logger.debug("Loading model %s from local path %s", model_id, path)
                return SentenceTransformer(str(path))
        logger.debug("Loading model %s from Hugging Face hub", model_id)
        return SentenceTransformer(model_id)

    @staticmethod
    def _default_encode_sync(model: Any, texts: tuple[str, ...]) -> list[tuple[float, ...]]:
        """Default synchronous encode: numpy array → tuple rows."""
        arr = model.encode(list(texts), convert_to_numpy=True, normalize_embeddings=True)
        return [tuple(float(x) for x in row) for row in arr]
