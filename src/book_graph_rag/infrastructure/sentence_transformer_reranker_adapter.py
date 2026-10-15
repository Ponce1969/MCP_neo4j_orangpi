"""Local cross-encoder implementation of ``RerankerPort``.

The heavy ``sentence_transformers`` import is deferred into the model loader so
module import stays cheap; the model itself is instantiated in ``__init__`` as
the design fixes it, and unit tests inject a fake with the same ``predict``
surface so no Hugging Face download happens off the network.

Scores are raw cross-encoder logits: only their order is used, so no activation
is applied (a monotonic transform cannot change the ranking).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from book_graph_rag.config import Settings
from book_graph_rag.domain.evaluation_models import RetrievalContext
from book_graph_rag.ports.reranker_port import RerankerPort

logger = logging.getLogger(__name__)

#: Fixed so repeated runs score identical batches (determinism over throughput:
#: at ~40 candidates per query the batching effect is negligible).
_PREDICT_BATCH_SIZE = 32


class SentenceTransformerRerankerAdapter(RerankerPort):
    """Re-score (query, candidate text) pairs with a local cross-encoder.

    ``model`` is injectable for tests; production builds a ``CrossEncoder`` for
    ``settings.reranker_model_id`` in the constructor. Inference is synchronous
    and CPU-bound, so it runs through ``asyncio.to_thread`` — the same pattern
    ``sqlite_routing_telemetry`` and ``sentence_transformer_adapter`` already use.
    """

    def __init__(self, settings: Settings, *, model: Any | None = None) -> None:
        self._model_id = settings.reranker_model_id
        self._model: Any = model if model is not None else self._load_model(self._model_id)

    async def rerank(
        self,
        *,
        query: str,
        candidates: tuple[RetrievalContext, ...],
        top_k: int,
    ) -> tuple[RetrievalContext, ...]:
        """Return the ``top_k`` most relevant candidates, best first.

        Ordering key: score descending, then chunk-bearing contexts before
        entities, then the original candidate order. The third component makes
        the order total, so equal scores can never reorder between runs.
        """
        if not candidates or top_k < 1:
            return ()
        pairs = [(query, candidate.text) for candidate in candidates]
        scores = await asyncio.to_thread(self._predict_sync, pairs)
        ranked = sorted(
            range(len(candidates)),
            key=lambda index: (
                -scores[index],
                candidates[index].chunk_id is None,
                index,
            ),
        )
        return tuple(candidates[index] for index in ranked[:top_k])

    def _predict_sync(self, pairs: list[tuple[str, str]]) -> list[float]:
        """Score ``pairs`` with the underlying model (runs in a worker thread)."""
        raw = self._model.predict(pairs, batch_size=_PREDICT_BATCH_SIZE)
        return [float(score) for score in raw]

    @staticmethod
    def _load_model(model_id: str) -> Any:
        """Load the cross-encoder from the Hugging Face hub."""
        from sentence_transformers import CrossEncoder

        logger.info("Loading cross-encoder reranker %s", model_id)
        return CrossEncoder(model_id)


def reranker_for(settings: Settings) -> SentenceTransformerRerankerAdapter | None:
    """The chunk reranker when its switch is on, else ``None``.

    ONE rule for every wiring site (evaluation command and MCP composition root), so
    the two can never drift. Gated on purpose: the retrieval A/B on the committed
    dataset measured every candidate cross-encoder *below* the plain fulltext order
    (0.1250 vs 0.0917 / 0.1083 / 0.1000), so the shipped default is ``False`` and a
    deployment that never enables it never downloads the model at boot.
    """
    if not settings.rerank_enabled:
        return None
    return SentenceTransformerRerankerAdapter(settings)
