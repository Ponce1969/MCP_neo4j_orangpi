"""Behavior tests for the read-only namespace profile source adapter."""

from __future__ import annotations

from typing import Any

import pytest
from neo4j import READ_ACCESS

from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.catalog_loader import CatalogLoader
from book_graph_rag.infrastructure.neo4j_namespace_profile_source import (
    Neo4jNamespaceProfileSource,
)

_CATALOG = """\
version: 1
corpora:
  knowledge:
    label: "Knowledge Library"
    sources:
      book-a:
        label: "Book A"
        file: "a.pdf"
        status: active
      book-b:
        label: "Book B"
        file: "b.pdf"
        status: active
"""


class _FakeResult:
    """Async iterable of records backed by a static list."""

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records

    def __aiter__(self) -> _FakeResult:
        return self

    async def __anext__(self) -> dict[str, Any]:
        if not self._records:
            raise StopAsyncIteration
        return self._records.pop(0)


class _FakeSession:
    """Records queries/params and serves canned records by statement family."""

    def __init__(self) -> None:
        self.queries: list[tuple[str, dict[str, Any]]] = []

    async def run(self, query: str, parameters: dict[str, Any] | None = None) -> _FakeResult:
        params = parameters or {}
        self.queries.append((query, params))
        if "Chapter" in query:
            records = [{"title": f"chapter-{params['source_id']}-{i}"} for i in range(2)]
        elif "Section" in query:
            records = [{"title": f"section-{params['source_id']}-{i}"} for i in range(1)]
        elif "Entity" in query:
            records = [{"name": f"entity-{i}", "description": f"desc-{i}"} for i in range(2)]
        elif "CommunitySummary" in query:
            records = [{"text": f"summary-{i}"} for i in range(1)]
        else:
            records = [{"text": f"chunk-{params['source_id']}-{i}"} for i in range(3)]
        return _FakeResult(records)

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass


class _FakeDriver:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session
        self.session_kwargs: list[dict[str, Any]] = []

    def session(self, **config: Any) -> _FakeSession:
        self.session_kwargs.append(config)
        return self._session

    async def close(self) -> None:
        pass


@pytest.fixture
def settings(tmp_path: Any) -> Settings:
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text(_CATALOG, encoding="utf-8")
    return Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
            "catalog_path": str(catalog_path),
        }
    )


async def test_load_source_texts_reads_every_active_namespace(
    settings: Settings,
) -> None:
    session = _FakeSession()
    adapter = Neo4jNamespaceProfileSource(
        settings,
        CatalogLoader(settings.catalog_path),
        driver=_FakeDriver(session),
    )

    sources = await adapter.load_source_texts()

    assert [source.namespace.source_id for source in sources] == [
        "knowledge:book-a",
        "knowledge:book-b",
    ]
    book_a = sources[0]
    assert book_a.chapter_titles[0] == "chapter-knowledge:book-a-0"
    assert book_a.section_titles[0] == "section-knowledge:book-a-0"
    assert book_a.chunk_texts[0] == "chunk-knowledge:book-a-0"
    assert book_a.entity_names[0] == "entity-0"
    assert book_a.entity_descriptions[0] == "desc-0"
    assert book_a.community_summaries[0] == "summary-0"


async def test_ordered_profile_texts_puts_primary_signals_first(
    settings: Settings,
) -> None:
    session = _FakeSession()
    adapter = Neo4jNamespaceProfileSource(
        settings,
        CatalogLoader(settings.catalog_path),
        driver=_FakeDriver(session),
    )

    sources = await adapter.load_source_texts()

    ordered = sources[0].ordered_profile_texts
    assert "chapter-knowledge:book-a-0" in ordered
    assert "summary-0" in ordered
    assert ordered.index("chunk-knowledge:book-a-0") < ordered.index("summary-0")


async def test_queries_are_parameterized_bound_not_concatenated(
    settings: Settings,
) -> None:
    session = _FakeSession()
    adapter = Neo4jNamespaceProfileSource(
        settings,
        CatalogLoader(settings.catalog_path),
        driver=_FakeDriver(session),
    )

    await adapter.load_source_texts()

    for query, _params in session.queries:
        assert "knowledge:book-a" not in query
        assert "knowledge:book-b" not in query
    entity_query_params = [params for query, params in session.queries if "Entity" in query]
    assert {"prefix": "knowledge:book-a:", "cap": 500} in entity_query_params


async def test_session_uses_read_database_with_read_access(
    settings: Settings,
) -> None:
    session = _FakeSession()
    driver = _FakeDriver(session)
    adapter = Neo4jNamespaceProfileSource(
        settings,
        CatalogLoader(settings.catalog_path),
        driver=driver,
    )

    await adapter.load_source_texts()

    assert driver.session_kwargs
    assert driver.session_kwargs[0]["database"] == settings.neo4j_read_database
    assert driver.session_kwargs[0]["default_access_mode"] == READ_ACCESS


async def test_entity_supplements_exclude_merged_entities(
    settings: Settings,
) -> None:
    session = _FakeSession()
    adapter = Neo4jNamespaceProfileSource(
        settings,
        CatalogLoader(settings.catalog_path),
        driver=_FakeDriver(session),
    )

    await adapter.load_source_texts()

    entity_query = next(query for query, _ in session.queries if "Entity" in query)
    assert "merged_into" in entity_query
