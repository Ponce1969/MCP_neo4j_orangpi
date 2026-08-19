from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import SecretStr

from book_graph_rag.config import Settings
from book_graph_rag.domain.audit_models import AuditTarget, OverallState
from book_graph_rag.infrastructure.neo4j_audit_adapter import QUERY_PLAN, Neo4jAuditAdapter


class Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        async def rows() -> AsyncIterator[dict[str, Any]]:
            for row in self.rows:
                yield row
        return rows()


class Transaction:
    def __init__(self, session: "Session") -> None:
        self.session = session
    async def run(self, query: str, params: dict[str, Any]) -> Result:
        self.session.queries.append((query, params))
        if "dbms.components" in query:
            return Result([{"version": "5.23", "edition": "community"}])
        if "UNWIND ['BOOK'" in query.upper():
            return Result([{"label": "Entity", "total": 2}])
        if "collect(n)" in query and ":Entity" in query:
            return Result([{"total": 2, "samples": [{
                "name": " Cafe\u0301\u00a0 NAME ", "kind": " PERSON ", "subject_ids": ["e2", "e1"]
            }]}])
        if "WITH coalesce(a.id" in query:
            return Result([{"total": 1, "samples": [{
                "source": " A ", "kind": " RELATED ", "target": " B ",
                "subject_ids": ["A", "B"], "native_edge_count": 2
            }]}])
        if "UNWIND ['CONTAINS'" in query.upper():
            return Result([{"rel_type": "RELATED", "total": 3}])
        return Result([{"total": 0, "samples": []}])


class Session:
    def __init__(self) -> None:
        self.queries: list[tuple[str, dict[str, Any]]] = []
        self.read_transactions = 0
    async def execute_read(self, callback: Any, *args: Any) -> Any:
        self.read_transactions += 1
        return await callback(Transaction(self), *args)
    async def __aenter__(self) -> "Session":
        return self
    async def __aexit__(self, *args: object) -> None:
        pass


class Driver:
    def __init__(self, session: Session) -> None:
        self.session_value = session
        self.databases: list[str] = []
    def session(self, **kwargs: Any) -> Session:
        self.databases.append(kwargs["database"])
        return self.session_value


def _target() -> AuditTarget:
    return AuditTarget(selector="bookgraph-neo4j", database="neo4j", scheme="bolt", host="db", uri="bolt://db:7687")


def _settings() -> Settings:
    return Settings(
        neo4j_uri="bolt://db:7687", neo4j_user="neo4j",
        neo4j_password=SecretStr("secret"), neo4j_database="neo4j"
    )


def _patch_driver(monkeypatch: Any, driver: Driver) -> None:
    monkeypatch.setattr(
        "book_graph_rag.infrastructure.neo4j_audit_adapter.AsyncGraphDatabase.driver",
        lambda *args, **kwargs: driver,
    )


@pytest.mark.asyncio
async def test_adapter_uses_one_configured_read_session_and_static_queries(
    monkeypatch: Any,
) -> None:
    session = Session()
    driver = Driver(session)
    _patch_driver(monkeypatch, driver)
    snapshot = await Neo4jAuditAdapter(_settings()).collect_snapshot(_target(), 3)
    assert snapshot.inventory["Entity"].value == 2
    assert driver.databases == ["neo4j"]
    assert session.read_transactions == len(QUERY_PLAN)
    assert all(params["sample_limit"] == 3 for _, params in session.queries)
    assert all(
        not any(token in query.upper() for token in ("MERGE", "CREATE", "DELETE", "DROP", "SET "))
        for query, _ in session.queries
    )
    assert snapshot.runtime.neo4j_version == "5.23"
    duplicates = {f.rule_id: f for f in snapshot.findings if f.rule_id.startswith("DUPLICATE_")}
    assert duplicates["DUPLICATE_ENTITY_LOGICAL"].samples[0].group_id == "entity-duplicate:edf9e3072308d1cb4ce9ffdab689bfaf3e826550037b11c1a9978b5d160a4c13"  # noqa: E501
    assert duplicates["DUPLICATE_RELATIONSHIP_LOGICAL"].samples[0].group_id == "relationship-duplicate:5337757b07e3c2fd7255f4b7c4123718faf0a59d6e8836541d6afd8e00021da1"  # noqa: E501
    assert duplicates["DUPLICATE_RELATIONSHIP_LOGICAL"].samples[0].native_edge_count == 2


def test_query_plan_is_named_and_does_not_accept_raw_cypher() -> None:
    names = [name for name, _ in QUERY_PLAN]
    assert names[0] == "runtime_metadata"
    assert {name for name, _ in QUERY_PLAN} >= {
        "inventory_nodes", "inventory_relationships", "hierarchy_chunk_parent_required",
        "endpoints_related", "provenance_relationship", "duplicates_entity", "pages_chunk",
    }
    queries = dict(QUERY_PLAN)
    assert all("$sample_limit" in query for query in queries.values())
    assert "NOT EXISTS" in queries["hierarchy_chunk_parent_required"]
    assert "chunk_index" in queries["provenance_relationship"]
    assert "r.type" in queries["duplicates_relationship"]
    assert all(
        query.index("ORDER BY") < query.index("collect(")
        for name, query in queries.items()
        if name not in {"runtime_metadata", "inventory_nodes", "inventory_relationships"}
    )


@pytest.mark.asyncio
async def test_negative_sample_limit_is_rejected_before_session_use(monkeypatch: Any) -> None:
    driver = Driver(Session())
    _patch_driver(monkeypatch, driver)
    with pytest.raises(ValueError, match="sample_limit"):
        await Neo4jAuditAdapter(_settings()).collect_snapshot(_target(), -1)
    assert driver.databases == []
    assert driver.session_value.read_transactions == 0


@pytest.mark.asyncio
async def test_transport_failure_is_not_reported_as_zero(monkeypatch: Any) -> None:
    class FailingSession(Session):
        async def execute_read(self, callback: Any, *args: Any) -> Any:
            raise ConnectionError("unreachable")
    driver = Driver(FailingSession())
    _patch_driver(monkeypatch, driver)
    snapshot = await Neo4jAuditAdapter(_settings()).collect_snapshot(_target(), 2)
    assert snapshot.failure_state == OverallState.UNREACHABLE
    assert snapshot.inventory == {}
