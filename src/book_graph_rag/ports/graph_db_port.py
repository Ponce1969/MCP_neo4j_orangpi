"""Port for graph database persistence."""

from __future__ import annotations

import abc

from book_graph_rag.domain.models import (
    Book,
    Chapter,
    Entity,
    KnowledgeGraphChunk,
    Relationship,
    Section,
)


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
    async def upsert_editorial_structure(
        self, chapter: Chapter, sections: list[Section], chunk: KnowledgeGraphChunk
    ) -> None:
        """Persist the book's hierarchical editorial structure (chapter →
        section → chunk) with their page references. Idempotent via MERGE.
        """
        ...
