"""Read-only Neo4j retrieval adapter for layer-5 evaluation (Slice B, D3)."""

from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import BaseModel

from book_graph_rag.application.global_query_use_case import GlobalQueryUseCase
from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.community_adapter import Neo4jCommunityAdapter
from book_graph_rag.infrastructure.llm_adapter import LLMAdapter
from book_graph_rag.infrastructure.neo4j_query_adapter import Neo4jQueryAdapter
from book_graph_rag.ports.graph_retrieval_port import GraphRetrievalPort


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
    ) -> None:
        self._settings = settings
        self._query_adapter = query_adapter or Neo4jQueryAdapter(settings)
        self._community_adapter = community_adapter or Neo4jCommunityAdapter(settings)
        self._llm_adapter = llm_adapter or LLMAdapter(settings)
        self._global_uc = GlobalQueryUseCase(
            read_port=self._community_adapter,
            llm_port=self._llm_adapter,
            max_concurrency=settings.summary_max_concurrency,
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
    ) -> tuple[str, ...]:
        """Return ordered contexts for ``question``."""
        if qtype == "global":
            summaries = await self._community_adapter.get_summaries_by_level(detail_level)
            return tuple(s.summary for s in summaries)

        chunks, entities = await asyncio.gather(
            self._query_adapter.search_chunks(question, limit=10),
            self._query_adapter.find_entity(question, None),
            return_exceptions=True,
        )
        contexts: list[str] = []
        if isinstance(chunks, list):
            contexts.extend(
                c.get("text", "") for c in chunks
                if isinstance(c, dict) and c.get("text")
            )
        if isinstance(entities, list):
            contexts.extend(
                f"{e.entity.name}: {e.entity.description}"
                for e in entities
                if hasattr(e, "entity") and e.entity.description
            )
        return tuple(contexts)

    async def compose_answer(
        self,
        *,
        question: str,
        contexts: tuple[str, ...],
    ) -> str:
        """Compose an NL answer from retrieved contexts."""
        joined = "\n---\n".join(contexts[:15])
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
