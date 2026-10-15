"""Read-only Neo4j retrieval adapter for layer-5 evaluation (Slice B, D3)."""

from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import BaseModel

from book_graph_rag.application.global_query_use_case import GlobalQueryUseCase
from book_graph_rag.config import Settings
from book_graph_rag.domain.evaluation_models import GlobalRetrievalAnswer, RetrievalContext
from book_graph_rag.infrastructure.community_adapter import Neo4jCommunityAdapter
from book_graph_rag.infrastructure.llm_adapter import LLMAdapter
from book_graph_rag.infrastructure.neo4j_query_adapter import Neo4jQueryAdapter
from book_graph_rag.ports.graph_retrieval_port import GraphRetrievalPort
from book_graph_rag.ports.reranker_port import RerankerPort


class _ProxyAnswer(BaseModel):
    """Lightweight model so instructor can return a raw completion as a string."""

    answer: str


class Neo4jRetrievalAdapter(GraphRetrievalPort):
    """Read-only graph retrieval for layer-5 claim validation.

    This adapter composes the existing read-only Neo4j adapters and the query LLM.
    It issues no write operations (verified by integration test snapshot).
    """

    def __init__(
        self,
        settings: Settings,
        *,
        llm_adapter: LLMAdapter | None = None,
        query_adapter: Neo4jQueryAdapter | None = None,
        community_adapter: Neo4jCommunityAdapter | None = None,
        reranker: RerankerPort | None = None,
    ) -> None:
        self._settings = settings
        self._query_adapter = query_adapter or Neo4jQueryAdapter(settings)
        self._community_adapter = community_adapter or Neo4jCommunityAdapter(settings)
        self._llm_adapter = llm_adapter or LLMAdapter(settings)
        # Optional: without a reranker the read path keeps its legacy shape
        # (fetch exactly retrieval_top_k chunks, no re-ordering).
        self._reranker = reranker
        self._global_uc = GlobalQueryUseCase(
            read_port=self._community_adapter,
            llm_port=self._llm_adapter,
            max_concurrency=settings.summary_max_concurrency,
            top_n=settings.ask_global_top_n,
            score_batch_size=settings.ask_global_score_batch_size,
        )

    async def close(self) -> None:
        """Close underlying Neo4j drivers."""
        await self._community_adapter.close()
        await self._query_adapter.close()

    async def fetch_contexts(
        self,
        *,
        question: str,
        qtype: Literal["global", "local"],
        detail_level: int,
    ) -> tuple[RetrievalContext, ...]:
        """Return ordered contexts for ``question``.

        Local questions fetch ``retrieval_overfetch_n`` fulltext candidates when a
        reranker is wired, re-order them and keep ``retrieval_top_k``; entities are
        appended after the ranked chunks, unchanged. Without a reranker the fetch is
        ``retrieval_top_k`` — exactly the pre-reranker behaviour.

        Global questions return the community summaries the production path would rank
        first for this question (``GlobalQueryUseCase.select``): the same ranking
        ``ask_global`` uses, so the layer-5 evidence is comparable to production instead of
        being every summary at the level in database order.
        """
        if qtype == "global":
            ranking = await self._global_uc.select(question, detail_level)
            return tuple(
                RetrievalContext(chunk_id=None, text=summary.summary) for summary, _score in ranking
            )

        top_k = self._settings.retrieval_top_k
        fetch_limit = self._settings.retrieval_overfetch_n if self._reranker is not None else top_k
        chunks, entities = await asyncio.gather(
            self._query_adapter.search_chunks(question, limit=fetch_limit),
            self._query_adapter.find_entity(question, None),
            return_exceptions=True,
        )
        chunk_contexts: list[RetrievalContext] = []
        if isinstance(chunks, list):
            chunk_contexts = [
                RetrievalContext(chunk_id=c.get("chunk_id"), text=c.get("text", ""))
                for c in chunks
                if isinstance(c, dict) and c.get("text")
            ]
        if self._reranker is not None and chunk_contexts:
            # The reranker only re-orders and truncates the pool it was handed: the
            # extra recall comes from fetch_limit, never from the reranker itself.
            chunk_contexts = list(
                await self._reranker.rerank(
                    query=question,
                    candidates=tuple(chunk_contexts),
                    top_k=top_k,
                )
            )
        contexts: list[RetrievalContext] = list(chunk_contexts)
        if isinstance(entities, list):
            contexts.extend(
                RetrievalContext(
                    chunk_id=None,
                    text=f"{e.entity.name}: {e.entity.description}",
                )
                for e in entities
                if hasattr(e, "entity") and e.entity.description
            )
        return tuple(contexts)

    async def answer_global(
        self,
        *,
        question: str,
        detail_level: int,
    ) -> GlobalRetrievalAnswer:
        """Return the answer production would give, with the summaries behind it.

        Delegates to the same use case ``ask_global`` runs, so the evaluation measures that
        path instead of a parallel one. ``contexts`` and ``citation_ids`` are built from the
        same list in the same order, so they cannot drift apart.
        """
        result = await self._global_uc.ask(question, detail_level)
        entries = result["contexts"]
        return GlobalRetrievalAnswer(
            answer=str(result["answer"]),
            contexts=tuple(
                RetrievalContext(chunk_id=None, text=str(entry["text"])) for entry in entries
            ),
            citation_ids=tuple(str(entry["id"]) for entry in entries),
        )

    async def compose_answer(
        self,
        *,
        question: str,
        contexts: tuple[RetrievalContext, ...],
    ) -> str:
        """Compose an NL answer from retrieved contexts.

        Local questions only: a global question is answered by ``answer_global``, which uses
        the production composer. This prompt sees the reranked chunk pool.
        """
        joined = "\n---\n".join(ctx.text for ctx in contexts[:15])
        prompt = (
            f"Question: {question}\n\n"
            f"Relevant contexts:\n{joined}\n\n"
            "Answer the question concisely based ONLY on the contexts above. "
            "If the answer cannot be found, say so."
        )
        response = await self._llm_adapter._query_client.chat.completions.create(  # noqa: SLF001
            model=self._settings.query_llm_model_name,
            response_model=_ProxyAnswer,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.answer
