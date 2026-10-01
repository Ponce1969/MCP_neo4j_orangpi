"""Backfill candidate query must report distinct chunks, not mentions.

``fetch_backfill_candidates`` feeds both the dry-run report and the evidence
bundle of an approval-gated operation.  Returning one row per ``:MENTIONS``
edge inflates every count: the Libro 1 dry-run reported 16.200 "candidates" for
1.515 real chunks (16.200 mentions), and ``--apply`` would have re-written each
chunk once per mention.
"""

from __future__ import annotations

from typing import Any

import pytest

from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.neo4j_command_adapter import Neo4jCommandAdapter


def _make_settings() -> Settings:
    return Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
            "graph_llm_base_url": "https://graph.example.test/v1",
            "graph_llm_model_name": "graph-model",
            "query_llm_base_url": "https://query.example.test/v1",
            "query_llm_model_name": "query-model",
        }
    )


class _FakeRecord:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]


class _FakeResult:
    """Async-iterable result: the candidate query streams one row per mention."""

    def __init__(self, records: list[_FakeRecord]) -> None:
        self._records = records

    async def __aiter__(self) -> Any:
        for record in self._records:
            yield record


class _FakeSession:
    def __init__(self, records: list[_FakeRecord]) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self._records = records

    async def run(self, query: str, parameters: dict[str, Any] | None = None) -> _FakeResult:
        self.calls.append((query, parameters))
        return _FakeResult(self._records)

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass


class _FakeDriver:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def session(self) -> _FakeSession:
        return self._session

    async def close(self) -> None:
        pass


class _FakeGraphDatabase:
    def __init__(self, session: _FakeSession) -> None:
        self.driver_instance = _FakeDriver(session)

    def driver(self, *args: Any, **kwargs: Any) -> _FakeDriver:
        return self.driver_instance


async def test_backfill_candidates_collapse_duplicate_mentions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-BACKFILL-01: a chunk with N mentions is reported once."""
    session = _FakeSession(
        [
            _FakeRecord({"chunk_index": 3}),
            _FakeRecord({"chunk_index": 3}),
            _FakeRecord({"chunk_index": 3}),
            _FakeRecord({"chunk_index": 7}),
            _FakeRecord({"chunk_index": 7}),
        ]
    )
    fake_db = _FakeGraphDatabase(session)
    monkeypatch.setattr(
        "book_graph_rag.infrastructure.neo4j_command_adapter.AsyncGraphDatabase",
        fake_db,
    )
    adapter = Neo4jCommandAdapter(_make_settings())

    indices = await adapter.fetch_backfill_candidates("knowledge:some-book")

    assert indices == [3, 7]


async def test_backfill_candidates_query_deduplicates_in_cypher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The duplicate collapse is pushed down so the driver does not stream N rows."""
    session = _FakeSession([])
    fake_db = _FakeGraphDatabase(session)
    monkeypatch.setattr(
        "book_graph_rag.infrastructure.neo4j_command_adapter.AsyncGraphDatabase",
        fake_db,
    )
    adapter = Neo4jCommandAdapter(_make_settings())

    await adapter.fetch_backfill_candidates("knowledge:some-book")

    query, params = session.calls[-1]
    assert "RETURN DISTINCT k.chunk_index" in query
    assert params == {"source_id": "knowledge:some-book"}
