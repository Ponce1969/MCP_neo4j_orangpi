"""Allowlisted, read-only Neo4j adapter for the extraction coverage instrument (front d).

Static Cypher is intentionally one statement per approved operation, so the approved
text stays readable in one piece (same trade the audit adapter makes).
"""

from __future__ import annotations

from typing import Any, cast

from neo4j import AsyncGraphDatabase

from book_graph_rag.config import Settings
from book_graph_rag.ports.extraction_coverage_port import (
    BookCoverage,
    ExtractionCoverageMeasurement,
    ExtractionCoveragePort,
)

#: Quotient query — the operator's approved definition (S1, approved 2026-10-19),
#: verbatim in algebra: the denominator is the **active** population (merged entities
#: explicitly discarded so they cannot inflate the base) and the numerator is
#: ``COUNT(DISTINCT e)`` of active entities holding at least one incoming
#: ``(:Chunk)-[:MENTIONS]->(e)`` relationship. The audit inventory is deliberately NOT
#: consulted: it includes merged entities while both halves of this quotient exclude
#: them, and that mismatch is the trap this instrument exists to avoid.
COVERAGE_QUOTIENT_QUERY = """
MATCH (e:Entity)
WHERE (e.merged_into IS NULL OR e.merged_into = '')
WITH e, EXISTS { MATCH (:Chunk)-[:MENTIONS]->(e) } AS mentioned
RETURN count(DISTINCT e) AS active_entities,
       count(DISTINCT CASE WHEN mentioned THEN e END) AS mentioned_active_entities
"""

#: Per-book/namespace breakdown (S1: AC-PROV-01 detail). Merged entities are excluded
#: here too, so the per-book sums can sit below the audit inventory's entity total for
#: exactly that reason; the instrument states the denominator explicitly instead of
#: leaving that subtraction to the reader (S7).
COVERAGE_PER_BOOK_QUERY = """
MATCH (c:Chunk)
WHERE c.book_id IS NOT NULL AND toString(c.book_id) <> ''
WITH coalesce(toString(c.book_id), '') AS book_id, c AS chunk
OPTIONAL MATCH (chunk)-[:MENTIONS]->(e:Entity)
WHERE (e.merged_into IS NULL OR e.merged_into = '')
RETURN book_id,
       count(DISTINCT chunk) AS chunk_count,
       count(DISTINCT e) AS mentioned_entities
ORDER BY book_id
"""


class Neo4jExtractionCoverageAdapter(ExtractionCoveragePort):
    """Two static ``MATCH … RETURN`` statements over the R2 read-only credential.

    The credential arrives through the per-consumer ``readonly_settings(settings)``
    view applied at the composition root, exactly like the other read adapters: this
    adapter never maps credentials itself, and every query it can issue is a read.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._driver: Any = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password.get_secret_value()),
        )

    async def close(self) -> None:
        """Close the underlying Neo4j driver."""
        await self._driver.close()

    @staticmethod
    async def _transaction(tx: Any, query: str) -> list[dict[str, Any]]:
        result = await tx.run(query)
        rows: list[dict[str, Any]] = []
        async for row in result:
            rows.append(row.data() if hasattr(row, "data") else dict(row))
        return rows

    @classmethod
    async def _read(cls, session: Any, query: str) -> list[dict[str, Any]]:
        """Run one approved query through a managed read transaction (``execute_read``)."""
        return cast(list[dict[str, Any]], await session.execute_read(cls._transaction, query))

    async def measure(self) -> ExtractionCoverageMeasurement:
        """Return the approved quotient and its per-book breakdown, parsed and validated."""
        async with self._driver.session() as session:
            quotient_rows = await self._read(session, COVERAGE_QUOTIENT_QUERY)
            book_rows = await self._read(session, COVERAGE_PER_BOOK_QUERY)
        if not quotient_rows:
            raise RuntimeError("extraction coverage quotient query returned no row")
        quotient = quotient_rows[0]
        books = tuple(
            BookCoverage(
                book_id=str(row["book_id"]),
                chunk_count=int(row["chunk_count"]),
                mentioned_entities=int(row["mentioned_entities"]),
            )
            for row in book_rows
        )
        # The model validator rejects mentioned > active, so a parsed measurement
        # provably keeps the quotient inside [0, 1] (S1).
        return ExtractionCoverageMeasurement(
            active_entities=int(quotient["active_entities"]),
            mentioned_active_entities=int(quotient["mentioned_active_entities"]),
            books=books,
        )
