"""Regression: Section/Chapter MERGE keys must carry book_id.

Two books with identical section titles in the same chapter number must
produce distinct Section nodes (one per book). Before the fix, the MERGE key
was ``(title, chapter_number)`` only, so a generic title like "Summary" in
book B reused the Section node of book A and overwrote its ``page_start``,
breaking the global ``PAGE_SECTION_INVALID_START`` audit rule.
"""

from __future__ import annotations

from typing import Any

import pytest

from book_graph_rag.config import Settings
from book_graph_rag.domain.checkpoint_models import VersionDimensions
from book_graph_rag.domain.models import (
    Book,
    Chapter,
    KnowledgeGraphChunk,
    PageRef,
    Section,
)
from book_graph_rag.infrastructure.neo4j_command_adapter import Neo4jCommandAdapter

VERSIONS = VersionDimensions(
    source_version="secns001",
    pipeline_version="1.0.0",
    model_version="openai:gpt-4o-mini:2026-09-01",
    schema_version="1.0.0",
)


def _chunk(
    book: Book,
    chapter: Chapter,
    section: Section,
    chunk_index: int,
    text: str,
) -> KnowledgeGraphChunk:
    return KnowledgeGraphChunk(
        text=text,
        chunk_index=chunk_index,
        book=book,
        chapter=chapter,
        section=section,
        section_ancestors=(),
        page_ref=PageRef(start=section.page_start, end=section.page_start + 1),
        entities=[],
        relationships=[],
    )


@pytest.mark.neo4j_integration
async def test_sections_are_isolated_per_book(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """Same section title+chapter across two books never shares a node."""
    books = (
        Book(
            id="test:secns-book-a",
            title="Book A",
            author="Test",
            pdf_path="/tmp/a.pdf",
            page_count=100,
        ),
        Book(
            id="test:secns-book-b",
            title="Book B",
            author="Test",
            pdf_path="/tmp/b.pdf",
            page_count=5,
        ),
    )
    # Same chapter number and same generic section title in both books, but
    # different page_start so an accidental shared node would be detectable.
    chapters = (
        Chapter(number=4, title="Chapter 4 A", page_start=1),
        Chapter(number=4, title="Chapter 4 B", page_start=1),
    )
    sections = (
        Section(
            chapter_number=4,
            level=2,
            title="Summary",
            page_start=3,
            parent_section_title=None,
        ),
        Section(
            chapter_number=4,
            level=2,
            title="Summary",
            page_start=40,
            parent_section_title=None,
        ),
    )

    adapter = Neo4jCommandAdapter(neo4j_settings)
    try:
        for i, (book, chapter, section) in enumerate(zip(books, chapters, sections, strict=True)):
            await adapter.commit_chunk_atomic(
                _chunk(book, chapter, section, chunk_index=i, text=f"chunk {i}"),
                entity_ids=[],
                versions=VERSIONS,
                attempt=1,
            )
    finally:
        await adapter.close()

    async with neo4j_driver.session() as session:
        # Exactly two Section nodes with this title+chapter exist, one per book.
        rec = await (
            await session.run(
                "MATCH (s:Section {title: 'Summary', chapter_number: 4}) "
                "RETURN count(s) AS c"
            )
        ).single()
        assert rec is not None
        assert rec["c"] == 2, f"expected 2 isolated sections, got {rec['c']}"

        # Each section carries its book_id so the audit can scope it.
        rec = await (
            await session.run(
                "MATCH (s:Section {title: 'Summary', chapter_number: 4}) "
                "WHERE s.book_id IS NULL RETURN count(s) AS c"
            )
        ).single()
        assert rec is not None
        assert rec["c"] == 0, "sections must expose book_id"

        # Book A's Summary keeps its own page_start (3), not Book B's (40).
        rec = await (
            await session.run(
                "MATCH (s:Section {title: 'Summary', chapter_number: 4, book_id: $bid}) "
                "RETURN s.page_start AS ps",
                {"bid": "test:secns-book-a"},
            )
        ).single()
        assert rec is not None
        assert rec["ps"] == 3, "Book A section page_start was clobbered by Book B"

        # Each Book reaches exactly one Summary section through its own chapter.
        for bid in ("test:secns-book-a", "test:secns-book-b"):
            rec = await (
                await session.run(
                    "MATCH (b:Book {id: $bid})-[:CONTAINS]->(:Chapter)-[:HAS_SECTION]->(s:Section) "
                    "WHERE s.title = 'Summary' RETURN count(s) AS c",
                    {"bid": bid},
                )
            ).single()
            assert rec is not None
            assert rec["c"] == 1, f"book {bid} must see exactly one Summary"