"""Unit tests for the Neo4j skill registry adapter (skill-quality-gating, Unit 2.2).

The adapter is the only piece that knows about the graph, and it must stay read-only:
these tests assert the query never writes, that a node maps to a ``Skill``, and that an
incomplete or invalid node is dropped instead of reaching the gate (REQ-SK-01).
"""

from __future__ import annotations

from typing import Any

from book_graph_rag.infrastructure.neo4j_skill_registry_adapter import (
    Neo4jSkillRegistryAdapter,
)

_FULL_NODE: dict[str, Any] = {
    "id": "skill:react-lookup:1.0.0",
    "name": "React pattern lookup",
    "version": "1.0.0",
    "status": "active",
    "tool_names": ["find_entity", "traverse_relationships"],
    "safety": 0.9,
    "executability": 0.8,
    "completeness": 0.7,
    "maintainability": 0.6,
    "cost_awareness": 0.5,
    "weight_safety": 2.0,
    "weight_executability": 2.0,
    "weight_completeness": 1.0,
    "weight_maintainability": 1.0,
    "weight_cost_awareness": 1.0,
}

_WRITE_KEYWORDS = ("CREATE", "MERGE", "SET ", "DELETE", "REMOVE", "DROP", "CALL {")


class _FakeResult:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records

    def __aiter__(self) -> Any:
        async def _iterate() -> Any:
            for record in self._records:
                yield record

        return _iterate()


class _FakeSession:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records
        self.queries: list[str] = []

    async def run(self, query: str, parameters: dict[str, Any] | None = None) -> _FakeResult:
        self.queries.append(query)
        return _FakeResult(self._records)

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class _FakeDriver:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def session(self, **_: Any) -> _FakeSession:
        return self._session


def _adapter(records: list[dict[str, Any]]) -> tuple[Neo4jSkillRegistryAdapter, _FakeSession]:
    session = _FakeSession(records)
    return Neo4jSkillRegistryAdapter(_FakeDriver(session)), session


async def test_load_active_maps_a_node_to_a_skill() -> None:
    """A complete node becomes a ``Skill`` whose score is the weighted mean."""
    adapter, _ = _adapter([dict(_FULL_NODE)])

    skills = await adapter.load_active()

    assert len(skills) == 1
    skill = skills[0]
    assert skill.id == "skill:react-lookup:1.0.0"
    assert skill.tool_names == ("find_entity", "traverse_relationships")
    assert skill.status == "active"
    # (2*0.9 + 2*0.8 + 0.7 + 0.6 + 0.5) / 7
    assert skill.quality_score == 5.2 / 7


async def test_load_active_query_is_read_only_and_scoped_to_active() -> None:
    """The registry read must never write, and must ask for active skills only."""
    adapter, session = _adapter([])

    await adapter.load_active()

    assert len(session.queries) == 1
    query = session.queries[0]
    assert "MATCH (s:Skill)" in query
    assert "status" in query
    assert "active" in query
    for keyword in _WRITE_KEYWORDS:
        assert keyword not in query, f"the registry read must not write: {keyword}"


async def test_node_missing_a_score_is_dropped_fail_closed() -> None:
    """REQ-SK-01: a node missing any score is ineligible and never returned."""
    incomplete = dict(_FULL_NODE, id="skill:incomplete:1.0.0", safety=None)
    adapter, _ = _adapter([incomplete, dict(_FULL_NODE)])

    skills = await adapter.load_active()

    assert [skill.id for skill in skills] == ["skill:react-lookup:1.0.0"]


async def test_node_with_an_invalid_score_is_dropped() -> None:
    """A score outside [0, 1] cannot be trusted, so the node is dropped too."""
    out_of_range = dict(_FULL_NODE, id="skill:bad-range:1.0.0", safety=1.5)
    adapter, _ = _adapter([out_of_range])

    assert await adapter.load_active() == ()


async def test_no_active_skills_yields_an_empty_tuple() -> None:
    """An empty registry is a valid, fail-closed snapshot."""
    adapter, _ = _adapter([])

    assert await adapter.load_active() == ()
