"""Port for graph database persistence."""

from __future__ import annotations

import abc
from dataclasses import dataclass

from book_graph_rag.domain.models import (
    Book,
    Chapter,
    Entity,
    KnowledgeGraphChunk,
    Relationship,
    Section,
)


@dataclass(frozen=True)
class CountTolerancePolicy:
    """Tolerance rules for post-run index verification."""

    chunk_tolerance_pct: float = 1.0
    chunk_tolerance_abs: int = 1
    entity_must_not_decrease: bool = True


class GraphDatabasePort(abc.ABC):
    """Contract for persistence into a graph DB (Neo4j, Memgraph, etc.)."""

    @abc.abstractmethod
    async def upsert_book(self, book: Book) -> None:
        """Idempotently persist the book root node (MERGE by id). Called ONCE
        at the start of an indexing run, before upsert_editorial_structure.
        """
        ...

    @abc.abstractmethod
    async def upsert_entities(self, entities: list[Entity]) -> None:
        """Idempotently persist entities (MERGE by id)."""
        ...

    @abc.abstractmethod
    async def upsert_relationships(self, relationships: list[Relationship]) -> None:
        """Idempotently persist relationships (MERGE by id endpoints)."""
        ...

    @abc.abstractmethod
    async def upsert_mentions(
        self, chunk_index: int, book_id: str | None, entity_ids: list[str]
    ) -> None:
        """Idempotently persist (:Chunk)-[:MENTIONS]->(:Entity) edges.

        Anchored on the Chunk node keyed by ``(chunk_index, book_id)`` and the
        Entity node keyed by ``id``. ``book_id`` may be ``None`` for PDFs
        without a TOC.
        """
        ...

    @abc.abstractmethod
    async def upsert_editorial_structure(
        self, chapter: Chapter, sections: list[Section], chunk: KnowledgeGraphChunk
    ) -> None:
        """Persist the book's hierarchical editorial structure (chapter →
        section → chunk) with their page references. Idempotent via MERGE.
        """
        ...

    @abc.abstractmethod
    async def clear_index(self) -> None:
        """Delete every index-created node and edge.

        Removes the following edge types: ``MENTIONS``, ``RELATED``,
        ``HAS_SUMMARY``, ``CONTAINS``, ``HAS_SECTION``, ``HAS_SUBSECTION``,
        ``HAS_CHUNK``.

        Removes the following node labels: ``Chunk``, ``Entity``,
        ``CommunitySummary``, ``Section``, ``Chapter``, ``Book``.

        ``:User`` and ``:Config`` nodes (and any edges incident to them) are
        left untouched because they are never referenced by the indexer.
        """
        ...

    @abc.abstractmethod
    async def count_chunks(self) -> int:
        """Return the number of ``:Chunk`` nodes in the graph."""
        ...

    @abc.abstractmethod
    async def count_entities(self) -> int:
        """Return the number of ``:Entity`` nodes in the graph."""
        ...

    @abc.abstractmethod
    async def count_mentions(self) -> int:
        """Return the number of ``(:Chunk)-[:MENTIONS]->(:Entity)`` edges."""
        ...
