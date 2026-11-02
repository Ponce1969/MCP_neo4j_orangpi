"""Allowlisted, read-only Neo4j adapter for extraction predictions (fidelity instrument).

Static Cypher is intentionally one statement per approved operation, so the approved
text stays readable in one piece (same trade the coverage adapter makes). The adapter
reads what the pipeline attached to the labeled chunks and nothing else: no writes, no
merge resolution (protocol §4.3 collapses in the METRIC, never here), no ``type``
filter, and merged entities stay IN the result set so the metric can follow them.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from neo4j import AsyncGraphDatabase

from book_graph_rag.config import Settings
from book_graph_rag.domain.extraction_fidelity import Prediction
from book_graph_rag.domain.extraction_labels import LabeledChunk
from book_graph_rag.domain.models import EntityType
from book_graph_rag.ports.extraction_prediction_port import (
    ExtractionPredictionMeasurement,
    ExtractionPredictionPort,
)

#: Mentioned-entities query — every entity the pipeline attached to one of the
#: SAMPLED chunks. The filter is pushed down to BOTH keys ($book_ids AND
#: $chunk_indices): a book-wide fetch (Huyen: thousands of MENTIONS rows to keep
#: 6 chunks) would put the whole measurement at the mercy of any result
#: truncation, and a silently partial result reads as "the extractor found
#: nothing" — a wrong number that looks like a finding. Cross-book
#: over-approximation is expected and harmless: chunk indices are not unique
#: across books, so the query may return extra rows, and the adapter's ``wanted``
#: map still decides what belongs to the sample. Deliberately NO ``merged_into``
#: filter (merged entities are predictions the metric must follow and collapse)
#: and NO ``type`` filter (the closed type set is a labeling rule, not a graph
#: filter). Grouping by ``(book_id, chunk_index)`` happens in the adapter, keyed
#: exactly like the label file's ``chunk_id``.
MENTIONED_ENTITIES_QUERY = """
MATCH (c:Chunk)-[:MENTIONS]->(e:Entity)
WHERE c.book_id IN $book_ids AND c.chunk_index IN $chunk_indices
RETURN toString(c.book_id) AS book_id,
       c.chunk_index AS chunk_index,
       e.id AS id,
       e.name AS name,
       e.canonical_name AS canonical_name,
       coalesce(e.aliases, []) AS aliases,
       e.type AS type
"""

#: Merge-target query — ``canonical_of`` for exactly the ids that appeared as
#: predictions on the labeled chunks: id → ``merged_into`` target. The metric walks
#: this map (protocol §4.3); the adapter never resolves through it, because
#: resolving here and again in the metric would hide a duplicate that collapsed.
CANONICAL_OF_QUERY = """
MATCH (e:Entity)
WHERE e.id IN $entity_ids
  AND e.merged_into IS NOT NULL
  AND e.merged_into <> ''
RETURN e.id AS id, e.merged_into AS merged_into
"""


def split_chunk_id(chunk_id: str) -> tuple[str, int]:
    """Split ``{book_id}:{chunk_index}`` on the LAST colon.

    The book id itself contains colons (``knowledge:ai-engineering-huyen``), so
    ``rsplit``-style splitting is the only correct cut. A malformed id is rejected
    with a message naming it — never silently skipped, because a skipped row would
    read as "no predictions" instead of "bad input".
    """
    book_id, separator, index_text = chunk_id.rpartition(":")
    if separator == "" or not book_id:
        raise ValueError(
            f"malformed chunk_id {chunk_id!r}: expected '{{book_id}}:{{chunk_index}}' "
            "with a colon before the index"
        )
    try:
        chunk_index = int(index_text)
    except ValueError as exc:
        raise ValueError(
            f"malformed chunk_id {chunk_id!r}: chunk_index {index_text!r} is not an integer"
        ) from exc
    return book_id, chunk_index


class Neo4jExtractionPredictionAdapter(ExtractionPredictionPort):
    """Two static ``MATCH … RETURN`` statements over the R2 read-only credential.

    The credential arrives through the per-consumer ``readonly_settings(settings)``
    view applied at the composition root, exactly like the coverage adapter: this
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
    async def _transaction(tx: Any, query: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        result = await tx.run(query, parameters)
        rows: list[dict[str, Any]] = []
        async for row in result:
            rows.append(row.data() if hasattr(row, "data") else dict(row))
        return rows

    @classmethod
    async def _read(
        cls, session: Any, query: str, parameters: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Run one approved query through a managed read transaction (``execute_read``)."""
        return cast(
            list[dict[str, Any]],
            await session.execute_read(cls._transaction, query, parameters),
        )

    async def measure(
        self, labeled_chunks: Sequence[LabeledChunk]
    ) -> ExtractionPredictionMeasurement:
        """Return predictions per labeled chunk plus their ``merged_into`` targets.

        Every labeled chunk appears in the result — an unmentioned chunk maps to
        ``[]`` ("the extractor found nothing" is a measurement, not missing data).
        A malformed ``chunk_id`` raises before any query reaches the database.
        """
        wanted: dict[tuple[str, int], str] = {}
        for chunk in labeled_chunks:
            parsed_book, parsed_index = split_chunk_id(chunk.chunk_id)
            wanted[(parsed_book, parsed_index)] = chunk.chunk_id
        book_ids = sorted({book_id for book_id, _ in wanted})
        chunk_indices = sorted({chunk_index for _, chunk_index in wanted})

        async with self._driver.session() as session:
            mention_rows = await self._read(
                session,
                MENTIONED_ENTITIES_QUERY,
                {"book_ids": book_ids, "chunk_indices": chunk_indices},
            )
            grouped: dict[str, list[dict[str, Any]]] = {
                chunk_id: [] for chunk_id in wanted.values()
            }
            predicted_ids: set[str] = set()
            for row in mention_rows:
                key = (str(row["book_id"]), int(row["chunk_index"]))
                chunk_id = wanted.get(key)
                if chunk_id is None:
                    continue  # a chunk outside the labeled sample: not our measurement
                grouped[chunk_id].append(row)
                predicted_ids.add(str(row["id"]))
            canonical_rows = await self._read(
                session,
                CANONICAL_OF_QUERY,
                {"entity_ids": sorted(predicted_ids)},
            )

        canonical_of = {
            str(row["id"]): str(row["merged_into"])
            for row in canonical_rows
            if str(row["id"]) in predicted_ids
        }
        predictions_by_chunk = {
            chunk_id: [
                Prediction(
                    id=str(row["id"]),
                    name=str(row["name"]),
                    canonical_name=(str(row["canonical_name"]) if row["canonical_name"] else None),
                    aliases=[str(alias) for alias in (row.get("aliases") or [])],
                    type=cast(EntityType, str(row["type"])),
                    merged_into=canonical_of.get(str(row["id"])),
                )
                for row in rows
            ]
            for chunk_id, rows in grouped.items()
        }
        return ExtractionPredictionMeasurement(
            predictions_by_chunk=predictions_by_chunk,
            canonical_of=canonical_of,
        )
