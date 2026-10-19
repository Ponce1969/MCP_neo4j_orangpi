"""The extraction coverage queries exclude merged entities on a REAL graph (front d).

The unit pins prove the quotient arithmetic against fakes; the exclusion itself is
enforced in the approved Cypher (``merged_into IS NULL OR merged_into = ''`` on both
halves of the quotient and in the per-book breakdown), and this file is the only place
that proves the Cypher does what the unit fakes assume: a merged entity that IS
mentioned must not inflate the numerator, a merged entity that is NOT mentioned must
not inflate the denominator, and a book whose chunks mention nothing must still
report its chunk count.

Testcontainers integration tests; they never touch production.
"""

from __future__ import annotations

from typing import Any

from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.neo4j_extraction_coverage_adapter import (
    Neo4jExtractionCoverageAdapter,
)
from book_graph_rag.ports.extraction_coverage_port import BookCoverage

_ALPHA_LIVE_MENTIONED = "knowledge:alpha:live-mentioned"
_ALPHA_LIVE_MENTIONED_TOO = "knowledge:alpha:live-mentioned-too"
_ALPHA_LIVE_UNMENTIONED = "knowledge:alpha:live-unmentioned"
_ALPHA_GHOST_MENTIONED = "knowledge:alpha:ghost-mentioned"
_ALPHA_GHOST_UNMENTIONED = "knowledge:alpha:ghost-unmentioned"


async def _seed_coverage_fixture(driver: Any) -> None:
    """A tiny corpus carrying every boundary the quotient must respect.

    Active population: 3 entities (2 mentioned, 1 not). Merged population: 2 entities
    (1 mentioned by a chunk, 1 unmentioned) — either one entering the quotient would
    change the result. Book ``knowledge:beta`` has one chunk that mentions nothing.
    """
    async with driver.session() as session:
        await session.run(
            """
            MERGE (c0:Chunk {book_id: 'knowledge:alpha', chunk_index: 0})
            MERGE (c1:Chunk {book_id: 'knowledge:alpha', chunk_index: 1})
            MERGE (c2:Chunk {book_id: 'knowledge:beta', chunk_index: 0})
            MERGE (a1:Entity {id: $a1})
            SET a1.name = 'Live Mentioned', a1.type = 'concept'
            MERGE (a2:Entity {id: $a2})
            SET a2.name = 'Live Mentioned Too', a2.type = 'concept'
            MERGE (a3:Entity {id: $a3})
            SET a3.name = 'Live Unmentioned', a3.type = 'concept'
            MERGE (m1:Entity {id: $m1})
            SET m1.name = 'Ghost Mentioned', m1.type = 'concept', m1.merged_into = $a1
            MERGE (m2:Entity {id: $m2})
            SET m2.name = 'Ghost Unmentioned', m2.type = 'concept', m2.merged_into = $a1
            MERGE (c0)-[:MENTIONS]->(a1)
            MERGE (c0)-[:MENTIONS]->(m1)
            MERGE (c1)-[:MENTIONS]->(a2)
            """,
            a1=_ALPHA_LIVE_MENTIONED,
            a2=_ALPHA_LIVE_MENTIONED_TOO,
            a3=_ALPHA_LIVE_UNMENTIONED,
            m1=_ALPHA_GHOST_MENTIONED,
            m2=_ALPHA_GHOST_UNMENTIONED,
        )


async def test_quotient_excludes_merged_entities_on_a_real_graph(
    neo4j_settings: Settings, neo4j_driver: Any
) -> None:
    """2/3, not 3/5: neither merged entity may enter either half of the quotient."""
    await _seed_coverage_fixture(neo4j_driver)
    adapter = Neo4jExtractionCoverageAdapter(neo4j_settings)

    try:
        measurement = await adapter.measure()
    finally:
        await adapter.close()

    assert measurement.active_entities == 3  # the two ghosts are discarded from the base
    assert measurement.mentioned_active_entities == 2  # the mentioned ghost adds nothing
    assert measurement.books == (
        BookCoverage(book_id="knowledge:alpha", chunk_count=2, mentioned_entities=2),
        # The empty book still reports its chunk count: its chunk mentions nothing active.
        BookCoverage(book_id="knowledge:beta", chunk_count=1, mentioned_entities=0),
    )
