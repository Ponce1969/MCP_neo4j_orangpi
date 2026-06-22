"""Tests for Neo4jQueryAdapter (AC-06.2, AC-06.5-AC-06.10, AC-06.17)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from book_graph_rag.config import Settings
from book_graph_rag.domain.models import (
    Entity,
    EntityWithContext,
    QueryTimeoutError,
    Relationship,
)
from book_graph_rag.infrastructure.neo4j_query_adapter import Neo4jQueryAdapter


class _FakeRecord:
    """Dict-like record returned by a fake Neo4j result."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def data(self) -> dict[str, Any]:
        return self._data


class _FakeResult:
    """Async iterable of records."""

    def __init__(self, records: list[_FakeRecord]) -> None:
        self._records = records

    async def __aiter__(self):
        for record in self._records:
            yield record


class _FakeSession:
    """Records Cypher queries and yields configurable records."""

    def __init__(
        self,
        records: list[_FakeRecord] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._records = records or []
        self._raise = raise_exc
        self.queries: list[tuple[str, dict[str, Any]]] = []

    async def run(
        self, query: str, parameters: dict[str, Any] | None = None
    ) -> _FakeResult:
        self.queries.append((query, parameters or {}))
        if self._raise is not None:
            raise self._raise
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
    """Stand-in for ``neo4j.AsyncGraphDatabase`` that records driver construction."""

    def __init__(self) -> None:
        self.driver_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self._driver: _FakeDriver | None = None

    def driver(self, *args: Any, **kwargs: Any) -> _FakeDriver:
        self.driver_calls.append((args, kwargs))
        if self._driver is None:
            self._driver = _FakeDriver(_FakeSession())
        return self._driver


@pytest.fixture
def fake_graph_database() -> _FakeGraphDatabase:
    return _FakeGraphDatabase()


@pytest.fixture
def adapter(
    fake_graph_database: _FakeGraphDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> Neo4jQueryAdapter:
    monkeypatch.setattr(
        "book_graph_rag.infrastructure.neo4j_query_adapter.AsyncGraphDatabase",
        fake_graph_database,
    )
    settings = Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
        }
    )
    return Neo4jQueryAdapter(settings)


# ── T-06.5: constructor + timeout wrapper ────────────────────────────────────


def test_adapter_requires_settings() -> None:
    """Adapter constructor requires Settings."""
    with pytest.raises(TypeError):
        Neo4jQueryAdapter()  # type: ignore[call-arg]


def test_adapter_creates_driver_from_settings(
    adapter: Neo4jQueryAdapter,
    fake_graph_database: _FakeGraphDatabase,
) -> None:
    """Constructor creates the async driver using the same pattern as the command adapter."""
    assert len(fake_graph_database.driver_calls) == 1
    args, kwargs = fake_graph_database.driver_calls[0]
    assert args[0] == "bolt://localhost:7687"
    assert kwargs["auth"] == ("neo4j", "secret")


async def test_run_with_timeout_returns_result(adapter: Neo4jQueryAdapter) -> None:
    """A coroutine that finishes within the timeout returns its value."""

    async def coro() -> str:
        return "ok"

    result = await adapter._run_with_timeout(coro())
    assert result == "ok"


async def test_run_with_timeout_raises_query_timeout(adapter: Neo4jQueryAdapter) -> None:
    """A slow coroutine is converted into a domain QueryTimeoutError."""

    async def slow() -> None:
        await asyncio.sleep(10)

    with pytest.raises(QueryTimeoutError, match="Query exceeded 0.01s timeout"):
        await adapter._run_with_timeout(slow(), timeout=0.01)
