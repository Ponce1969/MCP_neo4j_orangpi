"""GraphDatabasePort implementation using the async Neo4j driver.

All write operations use ``MERGE`` and parameters for idempotent,
 injection-safe persistence.
"""

from __future__ import annotations

from typing import Any

from neo4j import AsyncGraphDatabase

from book_graph_rag.config import Settings
from book_graph_rag.domain.models import (
    Book,
    Chapter,
    Entity,
    KnowledgeGraphChunk,
    Relationship,
    Section,
)
from book_graph_rag.ports.graph_db_port import GraphDatabasePort


class Neo4jCommandAdapter(GraphDatabasePort):
    """Async Neo4j implementation of ``GraphDatabasePort`` for write commands."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # Deserialize the SecretStr once at construction time. The password is
        # passed to the driver and never logged or printed by this adapter.
        self._driver: Any = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
        )

    async def close(self) -> None:
        """Close the underlying Neo4j driver."""
        await self._driver.close()

    async def upsert_book(self, book: Book) -> None:
        """Idempotently persist the book root node (MERGE by id)."""
        async with self._driver.session() as session:
            await session.run(
                """
                MERGE (b:Book {id: $id})
                SET b.title = $title,
                    b.author = $author,
                    b.pdf_path = $pdf_path,
                    b.page_count = $page_count
                """,
                {
                    "id": book.id,
                    "title": book.title,
                    "author": book.author,
                    "pdf_path": book.pdf_path,
                    "page_count": book.page_count,
                },
            )

    async def upsert_entities(self, entities: list[Entity]) -> None:
        """Idempotently persist entities (MERGE by id)."""
        async with self._driver.session() as session:
            await session.run(
                """
                UNWIND $entities AS e
                MERGE (n:Entity {id: e.id})
                SET n.name = e.name,
                    n.type = e.type,
                    n.description = e.description,
                    n.source_page = e.source_page
                """,
                {"entities": [entity.model_dump() for entity in entities]},
            )

    async def upsert_relationships(self, relationships: list[Relationship]) -> None:
        """Idempotently persist relationships (MERGE by endpoints + type)."""
        async with self._driver.session() as session:
            await session.run(
                """
                UNWIND $rels AS r
                MATCH (src:Entity {id: r.source_entity_id}),
                      (dst:Entity {id: r.target_entity_id})
                MERGE (src)-[rel:RELATED {type: r.type}]->(dst)
                SET rel.description = r.description,
                    rel.source_page = r.source_page
                """,
                {"rels": [rel.model_dump() for rel in relationships]},
            )

    async def upsert_editorial_structure(
        self, chapter: Chapter, sections: list[Section], chunk: KnowledgeGraphChunk
    ) -> None:
        """Persist the book's editorial hierarchy (chapter → section → chunk).

        Idempotent via ``MERGE``. If ``chunk.book`` is ``None`` (fallback for
        PDFs without TOC), the chunk node is created without a book link.
        """
        async with self._driver.session() as session:
            if chunk.book is not None:
                await session.run(
                    """
                    MERGE (b:Book {id: $book_id})
                    MERGE (ch:Chapter {number: $chapter_number, title: $chapter_title})
                    SET ch.page_start = $chapter_page_start
                    MERGE (b)-[:CONTAINS]->(ch)
                    """,
                    {
                        "book_id": chunk.book.id,
                        "chapter_number": chapter.number,
                        "chapter_title": chapter.title,
                        "chapter_page_start": chapter.page_start,
                    },
                )

            for section in sections:
                if section.parent_section_title is None:
                    await session.run(
                        """
                        MATCH (ch:Chapter {number: $chapter_number, title: $chapter_title})
                        MERGE (sec:Section {title: $section_title, chapter_number: $chapter_number})
                        SET sec.level = $section_level,
                            sec.page_start = $section_page_start
                        MERGE (ch)-[:HAS_SECTION]->(sec)
                        """,
                        {
                            "chapter_number": chapter.number,
                            "chapter_title": chapter.title,
                            "section_title": section.title,
                            "section_level": section.level,
                            "section_page_start": section.page_start,
                        },
                    )
                else:
                    await session.run(
                        """
                        MATCH (parent:Section {
                            title: $parent_section_title,
                            chapter_number: $chapter_number
                        })
                        MERGE (sec:Section {title: $section_title, chapter_number: $chapter_number})
                        SET sec.level = $section_level,
                            sec.page_start = $section_page_start
                        MERGE (parent)-[:HAS_SUBSECTION]->(sec)
                        """,
                        {
                            "chapter_number": chapter.number,
                            "section_title": section.title,
                            "section_level": section.level,
                            "section_page_start": section.page_start,
                            "parent_section_title": section.parent_section_title,
                        },
                    )

            book_id = chunk.book.id if chunk.book is not None else None
            await session.run(
                """
                MERGE (k:Chunk {chunk_index: $chunk_index, book_id: $book_id})
                SET k.text = $text,
                    k.page_start = $page_start,
                    k.page_end = $page_end
                """,
                {
                    "chunk_index": chunk.chunk_index,
                    "book_id": book_id,
                    "text": chunk.text,
                    "page_start": chunk.page_ref.start,
                    "page_end": chunk.page_ref.end,
                },
            )

            if sections:
                await session.run(
                    """
                    MATCH (sec:Section {title: $section_title, chapter_number: $chapter_number}),
                          (k:Chunk {chunk_index: $chunk_index, book_id: $book_id})
                    MERGE (sec)-[:HAS_CHUNK]->(k)
                    """,
                    {
                        "section_title": sections[-1].title,
                        "chapter_number": chapter.number,
                        "chunk_index": chunk.chunk_index,
                        "book_id": book_id,
                    },
                )
            elif chunk.book is not None:
                await session.run(
                    """
                    MATCH (ch:Chapter {number: $chapter_number, title: $chapter_title}),
                          (k:Chunk {chunk_index: $chunk_index, book_id: $book_id})
                    MERGE (ch)-[:HAS_CHUNK]->(k)
                    """,
                    {
                        "chapter_number": chapter.number,
                        "chapter_title": chapter.title,
                        "chunk_index": chunk.chunk_index,
                        "book_id": book_id,
                    },
                )
