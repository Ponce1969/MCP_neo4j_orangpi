"""Tests for Neo4jExtractionPredictionAdapter — predictions for labeled chunks.

The adapter is the read half of the extraction-fidelity instrument (protocol §4):
given the labeled chunks, it returns the entities the pipeline attached to each of
them, plus the ``merged_into`` map for merged entities that appear in those
predictions. It deliberately does NOT resolve merges (§4.3 resolves and collapses in
the METRIC; doing it here too would hide the difference) and does NOT filter merged
entities out (the metric needs them to follow and collapse them). A fake session
stands in for Neo4j; no live graph is required.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from pydantic import SecretStr

from book_graph_rag.config import Settings
from book_graph_rag.domain.extraction_labels import LabeledChunk
from book_graph_rag.infrastructure.neo4j_extraction_prediction_adapter import (
    CANONICAL_OF_QUERY,
    MENTIONED_ENTITIES_QUERY,
    Neo4jExtractionPredictionAdapter,
)

_BOOK = "knowledge:ai-engineering-huyen"


class Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        async def rows() -> AsyncIterator[dict[str, Any]]:
            for row in self.rows:
                yield row

        return rows()


class Transaction:
    def __init__(self, session: Session) -> None:
        self.session = session

    async def run(self, query: str, params: dict[str, Any] | None = None) -> Result:
        self.session.queries.append((query, params or {}))
        if query == MENTIONED_ENTITIES_QUERY:
            return Result(self.session.mention_rows)
        if query == CANONICAL_OF_QUERY:
            requested = set((params or {}).get("entity_ids", []))
            return Result([row for row in self.session.merge_rows if row["id"] in requested])
        raise AssertionError(f"unexpected query: {query}")


class Session:
    def __init__(
        self,
        mention_rows: list[dict[str, Any]] | None = None,
        merge_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.queries: list[tuple[str, dict[str, Any]]] = []
        self.read_transactions = 0
        self.mention_rows = mention_rows or []
        self.merge_rows = merge_rows or []

    async def execute_read(self, callback: Any, *args: Any) -> Any:
        self.read_transactions += 1
        return await callback(Transaction(self), *args)

    async def __aenter__(self) -> Session:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass


class Driver:
    def __init__(self, session: Session) -> None:
        self.session_value = session

    def session(self, **kwargs: Any) -> Session:
        return self.session_value


def _settings() -> Settings:
    return Settings(
        neo4j_uri="bolt://db:7687",
        neo4j_user="reader",
        neo4j_password=SecretStr("secret"),
        neo4j_database="neo4j",
    )


def _patch_driver(monkeypatch: Any, driver: Driver) -> None:
    monkeypatch.setattr(
        "book_graph_rag.infrastructure.neo4j_extraction_prediction_adapter."
        "AsyncGraphDatabase.driver",
        lambda *args, **kwargs: driver,
    )


def _labeled_chunk(chunk_index: int) -> LabeledChunk:
    """A protocol-valid label row (empty entities is legitimate, §5)."""
    return LabeledChunk(
        chunk_id=f"{_BOOK}:{chunk_index}",
        book_id=_BOOK,
        chunk_index=chunk_index,
        entities=[],
        notes="",
        completed=True,
    )


def _mention_row(chunk_index: int, entity_id: str, name: str, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "book_id": _BOOK,
        "chunk_index": chunk_index,
        "id": entity_id,
        "name": name,
        "canonical_name": None,
        "aliases": [],
        "type": "concept",
    }
    row.update(extra)
    return row


async def test_adapter_groups_mentions_by_chunk_and_keeps_unmentioned_chunks_empty(
    monkeypatch: Any,
) -> None:
    """Both approved queries run via execute_read; rows group by (book, chunk_index).

    A labeled chunk with NO mentions must appear with an empty list: an unmentioned
    chunk means "the extractor found nothing", which is a measurement, not missing
    data. Entities of chunks outside the labeled set are ignored.
    """
    session = Session(
        mention_rows=[
            _mention_row(16, f"{_BOOK}:snis-concept", "Super-NaturalInstructions"),
            _mention_row(16, f"{_BOOK}:mteb-concept", "MTEB"),
            _mention_row(240, f"{_BOOK}:imagebind-tool", "ImageBind", type="tool"),
        ]
    )
    _patch_driver(monkeypatch, Driver(session))
    adapter = Neo4jExtractionPredictionAdapter(_settings())

    measurement = await adapter.measure([_labeled_chunk(16), _labeled_chunk(370)])

    assert session.read_transactions == 2
    assert [query for query, _ in session.queries] == [
        MENTIONED_ENTITIES_QUERY,
        CANONICAL_OF_QUERY,
    ]
    mention_query, mention_params = session.queries[0]
    # The mention source is the approved one, filtered by book — the book id itself
    # contains a colon, so this asserts the last-colon split parsed it correctly.
    assert "MATCH (c:Chunk)-[:MENTIONS]->(e:Entity)" in mention_query
    assert "c.book_id IN $book_ids" in mention_query
    assert "c.chunk_index IN $chunk_indices" in mention_query
    assert "merged_into IS NULL" not in mention_query  # merged entities stay IN
    assert mention_params == {"book_ids": [_BOOK], "chunk_indices": [16, 370]}

    # Grouped by chunk; the labeled chunk without mentions is present with [].
    assert set(measurement.predictions_by_chunk) == {f"{_BOOK}:16", f"{_BOOK}:370"}
    assert [p.name for p in measurement.predictions_by_chunk[f"{_BOOK}:16"]] == [
        "Super-NaturalInstructions",
        "MTEB",
    ]
    mteb = measurement.predictions_by_chunk[f"{_BOOK}:16"][1]
    assert mteb.type == "concept"
    assert measurement.predictions_by_chunk[f"{_BOOK}:370"] == []
    # Chunk 240 was mentioned in the graph but is not a labeled chunk: not returned.
    assert f"{_BOOK}:240" not in measurement.predictions_by_chunk
    assert measurement.canonical_of == {}


async def test_mention_query_filters_by_chunk_indices_not_only_by_book(
    monkeypatch: Any,
) -> None:
    """The chunk filter lives IN Cypher: $book_ids AND $chunk_indices together.

    A book-wide fetch (Huyen: thousands of MENTIONS rows to keep 6 chunks) would
    make any result truncation look like "the extractor found nothing" — a wrong
    number that reads like a finding. The query must bound itself to the sample.
    """
    session = Session(
        mention_rows=[_mention_row(16, f"{_BOOK}:snis-concept", "Super-NaturalInstructions")]
    )
    _patch_driver(monkeypatch, Driver(session))
    adapter = Neo4jExtractionPredictionAdapter(_settings())

    await adapter.measure([_labeled_chunk(16), _labeled_chunk(370)])

    mention_query, mention_params = session.queries[0]
    assert "c.book_id IN $book_ids" in mention_query
    assert "c.chunk_index IN $chunk_indices" in mention_query
    assert mention_params == {"book_ids": [_BOOK], "chunk_indices": [16, 370]}


async def test_adapter_ignores_rows_for_chunks_outside_the_sample(monkeypatch: Any) -> None:
    """The wanted map still decides membership, even if the query over-returns.

    Over-approximation across books is expected (chunk 16 of another book may come
    back); the client-side sample filter must survive the Cypher-side push-down.
    """
    other_book = "knowledge:other-book"
    session = Session(
        mention_rows=[
            _mention_row(16, f"{_BOOK}:snis-concept", "Super-NaturalInstructions"),
            _mention_row(999, f"{_BOOK}:unsampled-concept", "Unsampled"),
            dict(
                _mention_row(16, f"{other_book}:foreign-concept", "Foreign"),
                book_id=other_book,
            ),
        ]
    )
    _patch_driver(monkeypatch, Driver(session))
    adapter = Neo4jExtractionPredictionAdapter(_settings())

    measurement = await adapter.measure([_labeled_chunk(16)])

    assert set(measurement.predictions_by_chunk) == {f"{_BOOK}:16"}
    assert [p.id for p in measurement.predictions_by_chunk[f"{_BOOK}:16"]] == [
        f"{_BOOK}:snis-concept"
    ]
    assert measurement.canonical_of == {}


async def test_adapter_returns_merged_entities_with_their_target_and_maps_only_them(
    monkeypatch: Any,
) -> None:
    """A merged entity travels as a prediction WITH its ``merged_into`` (§4.3).

    ``canonical_of`` is filled only for merged ids present in the predictions: an id
    merged in the graph but never predicted on a labeled chunk has no business in a
    map the metric consults while collapsing predictions.
    """
    canonical_target = f"{_BOOK}:react-pattern"
    merged_id = f"{_BOOK}:react-alias-concept"
    session = Session(
        mention_rows=[
            _mention_row(16, f"{_BOOK}:mcp-mcp", "MCP", type="mcp"),
            _mention_row(16, merged_id, "Reasoning and Acting", type="pattern"),
        ],
        merge_rows=[
            {"id": merged_id, "merged_into": canonical_target},
            {"id": f"{_BOOK}:never-predicted-tool", "merged_into": f"{_BOOK}:other-tool"},
        ],
    )
    _patch_driver(monkeypatch, Driver(session))
    adapter = Neo4jExtractionPredictionAdapter(_settings())

    measurement = await adapter.measure([_labeled_chunk(16)])

    predictions = measurement.predictions_by_chunk[f"{_BOOK}:16"]
    assert [p.id for p in predictions] == [f"{_BOOK}:mcp-mcp", merged_id]
    assert predictions[0].merged_into is None
    assert predictions[1].merged_into == canonical_target
    # The merge query asked for EXACTLY the predicted ids, and the never-predicted
    # merged id from the fixture did not leak into the map.
    merge_query, merge_params = session.queries[1]
    assert merge_query == CANONICAL_OF_QUERY
    assert merge_params == {"entity_ids": sorted([f"{_BOOK}:mcp-mcp", merged_id])}
    assert measurement.canonical_of == {merged_id: canonical_target}


class _NonIntegerIndexChunk:
    chunk_id = f"{_BOOK}:not-a-number"


class _NoColonChunk:
    chunk_id = "no-colon-here"


async def test_adapter_rejects_a_malformed_chunk_id_naming_it_before_querying(
    monkeypatch: Any,
) -> None:
    """A chunk_id whose index is not an integer is rejected with a message naming it."""
    session = Session()
    _patch_driver(monkeypatch, Driver(session))
    adapter = Neo4jExtractionPredictionAdapter(_settings())

    with pytest.raises(ValueError, match="not-a-number") as excinfo:
        await adapter.measure([cast(LabeledChunk, _NonIntegerIndexChunk())])

    assert f"{_BOOK}:not-a-number" in str(excinfo.value)
    assert session.queries == []  # rejected before any query reached the database


async def test_adapter_rejects_a_chunk_id_without_a_colon(monkeypatch: Any) -> None:
    """An id with no book/index separator cannot be parsed and is named, not skipped."""
    session = Session()
    _patch_driver(monkeypatch, Driver(session))
    adapter = Neo4jExtractionPredictionAdapter(_settings())

    with pytest.raises(ValueError, match="no-colon-here") as excinfo:
        await adapter.measure([_labeled_chunk(16), cast(LabeledChunk, _NoColonChunk())])

    assert "no-colon-here" in str(excinfo.value)
    assert session.queries == []
