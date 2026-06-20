"""Port for LLM-based knowledge graph extraction."""

from __future__ import annotations

import abc

from book_graph_rag.domain.models import KnowledgeGraphChunk


class LLMProviderPort(abc.ABC):
    """Contract for LLM-based graph extraction (e.g. instructor + OpenAI-compat client)."""

    @abc.abstractmethod
    async def extract_graph(self, chunk: KnowledgeGraphChunk) -> KnowledgeGraphChunk:
        """Receive a chunk with text + editorial metadata; return the chunk
        with its ``entities`` and ``relationships`` populated by the LLM.
        """
        ...
