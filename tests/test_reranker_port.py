"""Contract tests for ``RerankerPort`` (Phase 1 of the reranker design).

The port is the seam that lets evaluation and production re-order an
over-fetched candidate pool with a deterministic scorer. These tests pin the
contract: abstract, one method, keyword-only arguments, symmetric types.
"""

from __future__ import annotations

import pytest

from book_graph_rag.domain.evaluation_models import RetrievalContext
from book_graph_rag.ports.reranker_port import RerankerPort


class _IdentityReranker(RerankerPort):
    """Minimal conforming stub: returns the candidates unchanged, truncated."""

    async def rerank(
        self,
        *,
        query: str,
        candidates: tuple[RetrievalContext, ...],
        top_k: int,
    ) -> tuple[RetrievalContext, ...]:
        return candidates[:top_k]


def test_reranker_port_abstract_cannot_instantiate() -> None:
    with pytest.raises(TypeError):
        RerankerPort()  # type: ignore[abstract]


def test_subclass_missing_rerank_cannot_instantiate() -> None:
    class _MissingRerank(RerankerPort):
        pass

    with pytest.raises(TypeError):
        _MissingRerank()  # type: ignore[abstract]


async def test_conforming_stub_round_trips_candidates() -> None:
    candidates = (
        RetrievalContext(chunk_id="knowledge:book:1", text="first"),
        RetrievalContext(chunk_id=None, text="second"),
        RetrievalContext(chunk_id="knowledge:book:3", text="third"),
    )
    result = await _IdentityReranker().rerank(query="q", candidates=candidates, top_k=2)
    assert result == candidates[:2]
    assert all(isinstance(ctx, RetrievalContext) for ctx in result)
