"""Port for deterministic re-scoring of already-retrieved candidates.

Design context (decision document, Phase 0 audit): the retrieval gate metric is
set-based (``matched / len(chunk_contexts)`` over chunk-bearing contexts only),
so re-ordering alone cannot move it —
this port exists to re-score an **over-fetched** candidate pool and truncate it
back to ``top_k``. It is a pure, read-only seam: implementations never touch
the graph, the network beyond model inference, or any cache.
"""

from __future__ import annotations

import abc

from book_graph_rag.domain.evaluation_models import RetrievalContext


class RerankerPort(abc.ABC):
    """Re-order and truncate already-retrieved candidates for a query.

    Contract (implementations MUST honor every invariant):

    * **Conservation**: the result is a sub-multiset of ``candidates`` — no
      fabricated items, no mutated identity (``chunk_id`` and ``text`` pass
      through untouched).
    * **Bounded**: ``len(result) <= top_k``; implementations return exactly
      ``min(top_k, len(candidates))`` items — never fewer to game denominators.
    * **Determinism**: a fixed ``(query, candidates, top_k)`` triple always
      yields the same result for a fixed model.
    * **Stable ties**: equal scores are ordered by (``chunk_id`` ascending with
      ``None`` last, original index ascending).
    * **Purity**: no graph access, no writes, no cache management, no scope
      resolution — candidates arrive already scope-filtered.
    """

    @abc.abstractmethod
    async def rerank(
        self,
        *,
        query: str,
        candidates: tuple[RetrievalContext, ...],
        top_k: int,
    ) -> tuple[RetrievalContext, ...]:
        """Return at most ``top_k`` candidates, most relevant first."""
