"""Tests for Neo4jCommunityAdapter (PR2 Phase 2)."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from book_graph_rag.config import Settings
from book_graph_rag.domain.models import (
    CommunitySummary,
    Entity,
    Relationship,
)
from book_graph_rag.infrastructure.community_adapter import Neo4jCommunityAdapter


def _expected_id(level: int, entity_ids: list[str]) -> str:
    """Mirror the stable-id algorithm from the domain model."""
    key = f"{level}:{','.join(sorted(entity_ids))}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


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


class _FakeRelationship:
    """Mimics a Neo4j Relationship object with ``start_node``/``end_node``."""

    def __init__(
        self,
        start_node: _FakeRecord,
        end_node: _FakeRecord,
        rel_type: str,
        description: str = "",
        source_page: int | None = None,
    ) -> None:
        self.start_node = start_node
        self.end_node = end_node
        self.type = "RELATED"
        self._data: dict[str, Any] = {
            "type": rel_type,
            "description": description,
            "source_page": source_page,
        }

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)


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
        query_records: dict[str, list[_FakeRecord]] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._records = records or []
        self._query_records = query_records or {}
        self._raise = raise_exc
        self.queries: list[tuple[str, dict[str, Any]]] = []

    async def run(
        self, query: str, parameters: dict[str, Any] | None = None
    ) -> _FakeResult:
        self.queries.append((query, parameters or {}))
        if self._raise is not None:
            raise self._raise
        for marker, records in self._query_records.items():
            if marker in query:
                return _FakeResult(records)
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
        self._session: _FakeSession | None = None

    def driver(self, *args: Any, **kwargs: Any) -> _FakeDriver:
        self.driver_calls.append((args, kwargs))
        if self._driver is None:
            self._session = _FakeSession()
            self._driver = _FakeDriver(self._session)
        return self._driver

    @property
    def session(self) -> _FakeSession:
        if self._session is None:
            self._session = _FakeSession()
        return self._session


@pytest.fixture
def fake_graph_database() -> _FakeGraphDatabase:
    return _FakeGraphDatabase()


@pytest.fixture
def adapter(
    fake_graph_database: _FakeGraphDatabase,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> Neo4jCommunityAdapter:
    monkeypatch.chdir(tmp_path)
    for var in ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        "book_graph_rag.infrastructure.community_adapter.AsyncGraphDatabase",
        fake_graph_database,
    )
    settings = Settings.model_validate(
        {
            "neo4j_uri": "bolt://localhost:7687",
            "neo4j_user": "neo4j",
            "neo4j_password": "secret",
        }
    )
    return Neo4jCommunityAdapter(settings)


def test_adapter_requires_settings() -> None:
    """Neo4jCommunityAdapter requires Settings to construct."""
    with pytest.raises(TypeError):
        Neo4jCommunityAdapter()  # type: ignore[call-arg]


def test_adapter_creates_driver_from_settings(
    adapter: Neo4jCommunityAdapter,
    fake_graph_database: _FakeGraphDatabase,
) -> None:
    """Constructor creates the async driver with the configured credentials."""
    assert len(fake_graph_database.driver_calls) == 1
    args, kwargs = fake_graph_database.driver_calls[0]
    assert args[0] == "bolt://localhost:7687"
    assert kwargs["auth"] == ("neo4j", "secret")


async def test_load_entity_graph_returns_entities_and_relationships(
    adapter: Neo4jCommunityAdapter,
) -> None:
    """The adapter maps :Entity and :RELATED records to domain objects."""
    entity_record = _FakeRecord(
        {
            "id": "e1",
            "name": "Agent",
            "type": "agent",
            "description": "An agent entity",
            "source_page": 5,
        }
    )
    rel_node_a = _FakeRecord(
        {"id": "e1", "name": "Agent", "type": "agent", "description": "", "source_page": None}
    )
    rel_node_b = _FakeRecord(
        {"id": "e2", "name": "Pattern", "type": "pattern", "description": "", "source_page": None}
    )
    _ = _FakeRelationship(rel_node_a, rel_node_b, "requires")

    session = _FakeSession(
        query_records={
            "MATCH (e:Entity)": [entity_record],
            "MATCH (src:Entity)-[r:RELATED]->(dst:Entity)": [
                _FakeRecord(
                    {
                        "type": "requires",
                        "description": "",
                        "source_page": None,
                        "source_entity_id": "e1",
                        "target_entity_id": "e2",
                    }
                )
            ],
        }
    )
    adapter._driver = _FakeDriver(session)

    entities, relationships = await adapter.load_entity_graph()

    assert len(entities) == 1
    assert entities[0] == Entity(
        id="e1",
        name="Agent",
        type="agent",
        description="An agent entity",
        source_page=5,
    )
    assert len(relationships) == 1
    assert relationships[0] == Relationship(
        source_entity_id="e1",
        target_entity_id="e2",
        type="requires",
        description="",
        source_page=None,
    )

    queries = [q for q, _ in session.queries]
    assert any("MATCH (e:Entity)" in q for q in queries)
    assert any("MATCH (src:Entity)-[r:RELATED]->(dst:Entity)" in q for q in queries)


async def test_get_summaries_by_level_maps_community_nodes(
    adapter: Neo4jCommunityAdapter,
) -> None:
    """The adapter maps :CommunitySummary nodes to CommunitySummary models."""
    summary_id = _expected_id(1, ["e1", "e2"])
    parent_id = _expected_id(0, ["e1", "e2"])
    record = _FakeRecord(
        {
            "id": summary_id,
            "level": 1,
            "summary": "A community summary",
            "entity_ids": ["e1", "e2"],
            "parent_id": parent_id,
        }
    )
    session = _FakeSession(records=[record])
    adapter._driver = _FakeDriver(session)

    result = await adapter.get_summaries_by_level(1)

    assert len(result) == 1
    assert result[0].level == 1
    assert result[0].summary == "A community summary"
    assert result[0].entity_ids == ["e1", "e2"]
    assert result[0].parent_id == parent_id

    query, params = session.queries[0]
    assert "MATCH (c:CommunitySummary {level: $level})" in query
    assert params["level"] == 1


async def test_get_summaries_by_level_zero_has_no_parent(
    adapter: Neo4jCommunityAdapter,
) -> None:
    """Level 0 summaries are mapped with parent_id=None."""
    summary_id = _expected_id(0, ["e1", "e2"])
    record = _FakeRecord(
        {
            "id": summary_id,
            "level": 0,
            "summary": "Root community",
            "entity_ids": ["e1", "e2"],
            "parent_id": None,
        }
    )
    session = _FakeSession(records=[record])
    adapter._driver = _FakeDriver(session)

    result = await adapter.get_summaries_by_level(0)

    assert len(result) == 1
    assert result[0].level == 0
    assert result[0].parent_id is None


async def test_count_summaries_returns_total(
    adapter: Neo4jCommunityAdapter,
) -> None:
    """count_summaries issues the expected count query."""
    session = _FakeSession(records=[_FakeRecord({"count": 42})])
    adapter._driver = _FakeDriver(session)

    result = await adapter.count_summaries()

    assert result == 42
    query, _ = session.queries[0]
    assert "MATCH (c:CommunitySummary)" in query
    assert "count(c)" in query


async def test_upsert_summaries_emits_merge_per_id(
    adapter: Neo4jCommunityAdapter,
) -> None:
    """Persisting summaries uses MERGE keyed by the stable id."""
    summary = CommunitySummary(level=0, summary="Root.", entity_ids=["e1"])
    session = _FakeSession()
    adapter._driver = _FakeDriver(session)

    await adapter.upsert_summaries([summary])

    query, params = session.queries[0]
    assert "MERGE (c:CommunitySummary {id: s.id})" in query
    assert "UNWIND $summaries AS s" in query
    assert params["summaries"][0]["id"] == summary.id
    assert params["summaries"][0]["level"] == 0


async def test_upsert_summaries_emits_merge_for_multiple_summaries(
    adapter: Neo4jCommunityAdapter,
) -> None:
    """Multiple summaries are unwound and merged in a single query."""
    summary_a = CommunitySummary(level=0, summary="Root.", entity_ids=["e1"])
    summary_b = CommunitySummary(
        level=1,
        summary="Child.",
        entity_ids=["e1", "e2"],
        parent_id=summary_a.id,
    )
    session = _FakeSession()
    adapter._driver = _FakeDriver(session)

    await adapter.upsert_summaries([summary_a, summary_b])

    query, params = session.queries[0]
    assert "UNWIND $summaries AS s" in query
    assert len(params["summaries"]) == 2
    assert params["summaries"][0]["id"] == summary_a.id
    assert params["summaries"][1]["parent_id"] == summary_a.id


async def test_clear_summaries_emits_detach_delete(
    adapter: Neo4jCommunityAdapter,
) -> None:
    """Clearing summaries removes only :CommunitySummary nodes."""
    session = _FakeSession()
    adapter._driver = _FakeDriver(session)

    await adapter.clear_summaries()

    query, _ = session.queries[0]
    assert "MATCH (c:CommunitySummary)" in query
    assert "DETACH DELETE c" in query


async def test_clear_summaries_does_not_touch_base_graph(
    adapter: Neo4jCommunityAdapter,
) -> None:
    """The clear query never references :Entity or :RELATED labels."""
    session = _FakeSession()
    adapter._driver = _FakeDriver(session)

    await adapter.clear_summaries()

    query, _ = session.queries[0]
    assert ":Entity" not in query
    assert ":RELATED" not in query
