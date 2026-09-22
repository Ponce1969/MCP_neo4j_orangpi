"""Row-multiplication regression test for ``search_chunks`` (T-B.4).

``search_chunks`` must return exactly one row per unique ``Chunk``. The two
``OPTIONAL MATCH`` clauses in the query traverse variable-length ancestor paths
(``-[:HAS_SECTION|HAS_SUBSECTION*1..]->``); without an aggregation collapse,
each extra ancestor route multiplies the row count for the same chunk, and the
trailing ``ORDER BY score DESC LIMIT $limit`` then counts duplicated rows
instead of unique chunks.

This test seeds a Section reachable from its Chapter by MORE THAN ONE route and
proves that every returned chunk is unique and none is dropped.
"""

from __future__ import annotations

from typing import Any

import pytest

from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.neo4j_query_adapter import Neo4jQueryAdapter

pytestmark = pytest.mark.neo4j_integration

_BOOK_ID = "rowdup:test"
_CHAPTER_NUMBER = 1
_CHAPTER_TITLE = "Intro"
_SECTION_TITLE = "Core"
_NESTED_TITLE = "Nested"
_CHUNK_COUNT = 3


async def _seed_multiplying_graph(driver: Any) -> None:
    """Seed one Section reachable from its Chapter by two distinct routes.

    The Section ``Core`` is reachable from the Chapter both directly
    (``Chapter -[:HAS_SECTION]-> Core``) and through a nested subsection
    (``Chapter -[:HAS_SECTION]-> Nested -[:HAS_SUBSECTION]-> Core``), so the
    variable-length ancestor traversal yields two routes back to the same
    Chapter for every chunk owned by ``Core``.
    """
    async with driver.session() as session:
        await session.run(
            """
            MERGE (b:Book {id: $book_id})
            MERGE (c:Chapter {number: $chapter_number, title: $chapter_title})
            MERGE (s:Section {chapter_number: $chapter_number, title: $section_title})
            MERGE (nested:Section {chapter_number: $chapter_number, title: $nested_title})
            MERGE (b)-[:CONTAINS]->(c)
            MERGE (c)-[:HAS_SECTION]->(s)
            MERGE (c)-[:HAS_SECTION]->(nested)
            MERGE (nested)-[:HAS_SUBSECTION]->(s)
            """,
            book_id=_BOOK_ID,
            chapter_number=_CHAPTER_NUMBER,
            chapter_title=_CHAPTER_TITLE,
            section_title=_SECTION_TITLE,
            nested_title=_NESTED_TITLE,
        )
        for index in range(_CHUNK_COUNT):
            await session.run(
                """
                MERGE (k:Chunk {id: $chunk_id})
                SET k.book_id = $book_id, k.chunk_index = $index,
                    k.page_start = 1, k.page_end = 1, k.text = $text
                WITH k
                MATCH (s:Section {
                    chapter_number: $chapter_number,
                    title: $section_title
                })
                MERGE (s)-[:HAS_CHUNK]->(k)
                """,
                chunk_id=f"{_BOOK_ID}:chunk-{index}",
                book_id=_BOOK_ID,
                index=index,
                text=f"shared token chunk {index}",
                chapter_number=_CHAPTER_NUMBER,
                section_title=_SECTION_TITLE,
            )


async def test_search_chunks_returns_one_row_per_unique_chunk(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """Every unique chunk must appear exactly once, never multiplied by routes."""
    await _seed_multiplying_graph(neo4j_driver)

    adapter = Neo4jQueryAdapter(neo4j_settings)
    try:
        await adapter.ensure_indexes()
        chunks = await adapter.search_chunks("shared token", 10)

        chunk_ids = [chunk["chunk_id"] for chunk in chunks]
        assert len(chunks) == _CHUNK_COUNT
        assert len(chunk_ids) == _CHUNK_COUNT
        assert len(set(chunk_ids)) == _CHUNK_COUNT, (
            f"search_chunks returned duplicated rows: {chunk_ids}"
        )
    finally:
        await adapter.close()
