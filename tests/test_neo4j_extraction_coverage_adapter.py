"""Tests for Neo4jExtractionCoverageAdapter (front d, S4 pin 4).

The pin: the adapter passes the two approved queries (S1's quotient definition and the
per-book breakdown) through ``execute_read`` on the read-only credential, and parses
their rows into the measurement the use case divides. A fake session stands in for
Neo4j; no live graph is required.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.neo4j_extraction_coverage_adapter import (
    COVERAGE_PER_BOOK_QUERY,
    COVERAGE_QUOTIENT_QUERY,
    Neo4jExtractionCoverageAdapter,
)
from book_graph_rag.ports.extraction_coverage_port import (
    BookCoverage,
    ExtractionCoverageMeasurement,
)


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
        if "active_entities" in query:
            return Result([self.session.quotient_row])
        return Result(self.session.book_rows)


class Session:
    def __init__(
        self,
        quotient_row: dict[str, Any] | None = None,
        book_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.queries: list[tuple[str, dict[str, Any]]] = []
        self.read_transactions = 0
        self.quotient_row = quotient_row or {
            "active_entities": 8,
            "mentioned_active_entities": 8,
        }
        self.book_rows = (
            book_rows
            if book_rows is not None
            else [
                {
                    "book_id": "knowledge:test-book",
                    "chunk_count": 4,
                    "mentioned_entities": 8,
                }
            ]
        )

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
        "book_graph_rag.infrastructure.neo4j_extraction_coverage_adapter.AsyncGraphDatabase.driver",
        lambda *args, **kwargs: driver,
    )


async def test_adapter_passes_the_approved_queries_and_parses_the_result(
    monkeypatch: Any,
) -> None:
    """Both approved queries run via execute_read; their rows become the measurement."""
    session = Session()
    _patch_driver(monkeypatch, Driver(session))
    adapter = Neo4jExtractionCoverageAdapter(_settings())

    measurement = await adapter.measure()

    # execute_read (managed read transaction) for BOTH queries — no ad-hoc writes.
    assert session.read_transactions == 2
    assert [query for query, _ in session.queries] == [
        COVERAGE_QUOTIENT_QUERY,
        COVERAGE_PER_BOOK_QUERY,
    ]

    # The quotient query carries S1's approved definition, verbatim in algebra:
    # active denominator (merged entities explicitly discarded) and the COUNT(DISTINCT e)
    # numerator of active entities with an incoming (:Chunk)-[:MENTIONS] relationship.
    quotient_query = session.queries[0][0]
    assert "merged_into IS NULL OR e.merged_into = ''" in quotient_query
    assert "count(DISTINCT e) AS active_entities" in quotient_query
    assert "(:Chunk)-[:MENTIONS]->(e)" in quotient_query
    assert "count(DISTINCT CASE WHEN mentioned THEN e END) AS mentioned_active_entities" in (
        quotient_query
    )

    # The per-book query groups by book/namespace and excludes merged entities too.
    per_book_query = session.queries[1][0]
    assert "coalesce(toString(c.book_id), '') AS book_id" in per_book_query
    assert "count(DISTINCT chunk) AS chunk_count" in per_book_query
    assert "count(DISTINCT e) AS mentioned_entities" in per_book_query
    assert "merged_into IS NULL OR e.merged_into = ''" in per_book_query

    # Parsed result.
    assert measurement == ExtractionCoverageMeasurement(
        active_entities=8,
        mentioned_active_entities=8,
        books=(BookCoverage(book_id="knowledge:test-book", chunk_count=4, mentioned_entities=8),),
    )


async def test_adapter_refuses_a_measurement_that_could_leave_the_unit_interval(
    monkeypatch: Any,
) -> None:
    """A numerator above the denominator is rejected at the model boundary (S1)."""
    session = Session(
        quotient_row={"active_entities": 8, "mentioned_active_entities": 9},
        book_rows=[],
    )
    _patch_driver(monkeypatch, Driver(session))
    adapter = Neo4jExtractionCoverageAdapter(_settings())

    with pytest.raises(ValidationError):
        await adapter.measure()


async def test_adapter_fails_loudly_on_an_empty_quotient_result(monkeypatch: Any) -> None:
    """No quotient row means the database answered nothing; a zero must not be invented."""

    class _EmptyTransaction(Transaction):
        async def run(self, query: str, params: dict[str, Any] | None = None) -> Result:
            self.session.queries.append((query, params or {}))
            return Result([])

    class _EmptySession(Session):
        async def execute_read(self, callback: Any, *args: Any) -> Any:
            self.read_transactions += 1
            return await callback(_EmptyTransaction(self), *args)

    _patch_driver(monkeypatch, Driver(_EmptySession()))
    adapter = Neo4jExtractionCoverageAdapter(_settings())

    with pytest.raises(RuntimeError):
        await adapter.measure()
