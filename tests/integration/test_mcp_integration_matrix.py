"""End-to-end integration matrix (T-I.1): MCP wiring, neighborhood, dynamic path.

Proves, against real testcontainers Neo4j:

1. The ``McpServerAdapter`` wired to ``Neo4jQueryAdapter`` + a catalog-backed
   scope resolver executes scoped structured reads in isolation and leaves the
   graph snapshot byte-for-byte unchanged (read-only authority).
2. ``Neo4jNeighborhoodQueryAdapter`` resolves mention sources and related
   neighbours for a single entity.
3. The dynamic-query path's structural validator requires a scope proof and the
   EXPLAIN gate runs against real Neo4j; the disabled-by-default ``query_cypher``
   tool returns a typed policy error without contacting the graph or the LLM.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from book_graph_rag.config import Settings
from book_graph_rag.domain.mcp_security import (
    ScopeContext,
    StructuralPolicyViolationError,
)
from book_graph_rag.infrastructure.catalog_loader import CatalogLoader
from book_graph_rag.infrastructure.catalog_scope_resolver import CatalogScopeResolver
from book_graph_rag.infrastructure.mcp.mcp_server_adapter import McpServerAdapter
from book_graph_rag.infrastructure.neo4j_neighborhood_query_adapter import (
    Neo4jNeighborhoodQueryAdapter,
)
from book_graph_rag.infrastructure.neo4j_query_adapter import Neo4jQueryAdapter
from book_graph_rag.infrastructure.structural_cypher_policy import StructuralCypherPolicy
from book_graph_rag.ports.query_logger_port import QueryLoggerPort
from book_graph_rag.ports.text2cypher_port import Text2CypherPort, Text2CypherResult

pytestmark = pytest.mark.neo4j_integration

_NS_A = "matrix:ns-a"
_NS_B = "matrix:ns-b"

_CATALOG = """\
version: 1
corpora:
  matrix:
    label: "Matrix corpus"
    sources:
      ns-a:
        label: "Namespace A"
        file: "a.pdf"
        status: active
      ns-b:
        label: "Namespace B"
        file: "b.pdf"
        status: active
"""


class _NoopQueryLogger(QueryLoggerPort):
    """Discards every structured log entry (no persistence in tests)."""

    async def log_query(self, entry: Any) -> None:
        pass

    async def close(self) -> None:
        pass


class _ExplodingText2Cypher(Text2CypherPort):
    """Fails the test if the dynamic path is ever invoked."""

    async def generate_and_run(
        self, question: str, *, scope: ScopeContext | None = None
    ) -> Text2CypherResult:
        raise AssertionError("text2cypher must never run for the disabled path")


def _resolver(tmp_path: Path) -> CatalogScopeResolver:
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text(_CATALOG, encoding="utf-8")
    return CatalogScopeResolver(CatalogLoader(catalog_path))


def _mcp_adapter(
    settings: Settings, resolver: CatalogScopeResolver, *, enable_query_cypher: bool = False
) -> McpServerAdapter:
    return McpServerAdapter(
        Neo4jQueryAdapter(settings),
        _NoopQueryLogger(),
        _ExplodingText2Cypher(),
        scope_resolver=resolver,
        enable_query_cypher=enable_query_cypher,
        hmac_key_id="test-v1",
        hmac_key=SecretStr("test-hmac-secret"),
    )


async def _seed_adversarial_graph(driver: Any) -> None:
    """Seed colliding entity names and chunk text across two namespaces."""
    async with driver.session() as session:
        await session.run(
            """
            MERGE (a_shared:Entity {id: $a_shared})
            SET a_shared.name = 'Shared Concept', a_shared.type = 'concept',
                a_shared.source_page = 1
            MERGE (a_only:Entity {id: $a_only})
            SET a_only.name = 'Only In A', a_only.type = 'concept',
                a_only.source_page = 2
            MERGE (b_shared:Entity {id: $b_shared})
            SET b_shared.name = 'Shared Concept', b_shared.type = 'concept',
                b_shared.source_page = 1
            """,
            a_shared=f"{_NS_A}:shared-concept",
            a_only=f"{_NS_A}:only-in-a-concept",
            b_shared=f"{_NS_B}:shared-concept",
        )
        await session.run(
            """
            MERGE (ca:Chunk {id: $ca})
            SET ca.book_id = $ns_a, ca.chunk_index = 0,
                ca.page_start = 1, ca.page_end = 1, ca.text = 'shared token'
            MERGE (cb:Chunk {id: $cb})
            SET cb.book_id = $ns_b, cb.chunk_index = 0,
                cb.page_start = 1, cb.page_end = 1, cb.text = 'shared token'
            """,
            ca=f"{_NS_A}:chunk-0",
            cb=f"{_NS_B}:chunk-0",
            ns_a=_NS_A,
            ns_b=_NS_B,
        )
        await session.run(
            """
            MATCH (ca:Chunk {id: $ca})
            MATCH (a_shared:Entity {id: $a_shared})
            MATCH (a_only:Entity {id: $a_only})
            MERGE (ca)-[m1:MENTIONS]->(a_shared)
                SET m1.source_page = 1, m1.chunk_index = 0
            MERGE (ca)-[m2:MENTIONS]->(a_only)
                SET m2.source_page = 1, m2.chunk_index = 0
            """,
            ca=f"{_NS_A}:chunk-0",
            a_shared=f"{_NS_A}:shared-concept",
            a_only=f"{_NS_A}:only-in-a-concept",
        )


def _snapshot_key_value(value: Any) -> str:
    return "" if value is None else str(value)


async def _graph_snapshot(driver: Any) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    """Return a deterministic snapshot of every node and relationship."""
    async with driver.session() as session:
        nodes_result = await session.run(
            "MATCH (n) "
            "RETURN labels(n) AS labels, n.id AS id, n.name AS name, n.type AS type, "
            "n.book_id AS book_id, n.chunk_index AS chunk_index, n.text AS text, "
            "n.source_page AS source_page"
        )
        nodes: list[tuple[Any, ...]] = []
        async for record in nodes_result:
            nodes.append(
                (
                    tuple(sorted(record["labels"])),
                    _snapshot_key_value(record["id"]),
                    _snapshot_key_value(record["name"]),
                    _snapshot_key_value(record["type"]),
                    _snapshot_key_value(record["book_id"]),
                    _snapshot_key_value(record["chunk_index"]),
                    _snapshot_key_value(record["text"]),
                    _snapshot_key_value(record["source_page"]),
                )
            )

        rels_result = await session.run(
            "MATCH ()-[r]->() "
            "RETURN type(r) AS t, r.type AS prop, r.source_page AS sp, "
            "r.chunk_index AS ci"
        )
        rels: list[tuple[Any, ...]] = []
        async for record in rels_result:
            rels.append(
                (
                    _snapshot_key_value(record["t"]),
                    _snapshot_key_value(record["prop"]),
                    _snapshot_key_value(record["sp"]),
                    _snapshot_key_value(record["ci"]),
                )
            )

    return (sorted(nodes), sorted(rels))


async def test_mcp_adapter_scoped_reads_are_isolated_and_read_only(
    neo4j_settings: Settings,
    neo4j_driver: Any,
    tmp_path: Path,
) -> None:
    """The MCP boundary scopes structured reads to ns-a and never mutates the graph."""
    await _seed_adversarial_graph(neo4j_driver)
    adapter = _mcp_adapter(neo4j_settings, _resolver(tmp_path))
    graph_port = adapter._graph_query_port
    assert isinstance(graph_port, Neo4jQueryAdapter)
    try:
        await graph_port.ensure_indexes()
        before = await _graph_snapshot(neo4j_driver)

        found = await adapter.find_entity("Shared Concept", source_id=_NS_A)
        found_ids = {e["entity"]["id"] for e in found["entities"]}
        assert found_ids == {f"{_NS_A}:shared-concept"}

        count = await adapter.count_entities(source_id=_NS_A)
        assert count == {"count": 2}

        listed = await adapter.list_entities(cursor=0, page_size=100, source_id=_NS_A)
        listed_ids = {e["entity"]["id"] for e in listed["entities"]}
        assert listed_ids, "scoped list_entities returned no rows"
        assert all(eid.startswith(f"{_NS_A}:") for eid in listed_ids)

        chunks = await adapter.search_chunks("shared token", limit=10, source_id=_NS_A)
        returned_book_ids = {chunk["book_id"] for chunk in chunks["chunks"]}
        assert returned_book_ids == {_NS_A}

        after = await _graph_snapshot(neo4j_driver)
        assert before == after, "MCP scoped reads mutated the graph"
    finally:
        await graph_port.close()


async def test_neighborhood_adapter_mentions_and_neighbours(
    neo4j_driver: Any,
) -> None:
    """Neo4jNeighborhoodQueryAdapter returns mention sources and related neighbours."""
    async with neo4j_driver.session() as session:
        await session.run(
            """
            MERGE (a:Entity {id: $a})
            SET a.name = 'Alpha', a.type = 'concept', a.source_page = 1
            MERGE (b:Entity {id: $b})
            SET b.name = 'Beta', b.type = 'concept', b.source_page = 2
            """,
            a=f"{_NS_A}:alpha",
            b=f"{_NS_A}:beta",
        )
        await session.run(
            """
            MERGE (c:Chunk {id: $c})
            SET c.source_id = $ns_a, c.book_id = $ns_a, c.chunk_index = 0,
                c.page_start = 1, c.page_end = 1, c.text = 'alpha mention'
            """,
            c=f"{_NS_A}:chunk-0",
            ns_a=_NS_A,
        )
        await session.run(
            """
            MATCH (c:Chunk {id: $c})
            MATCH (a:Entity {id: $a})
            MERGE (c)-[m:MENTIONS]->(a)
                SET m.source_page = 1, m.chunk_index = 0
            """,
            c=f"{_NS_A}:chunk-0",
            a=f"{_NS_A}:alpha",
        )
        await session.run(
            """
            MATCH (a:Entity {id: $a})
            MATCH (b:Entity {id: $b})
            MERGE (a)-[r:RELATED]->(b)
                SET r.type = 'requires', r.source_page = 1, r.chunk_index = 0
            """,
            a=f"{_NS_A}:alpha",
            b=f"{_NS_A}:beta",
        )

    adapter = Neo4jNeighborhoodQueryAdapter(neo4j_driver)
    try:
        sources = await adapter.mention_sources(f"{_NS_A}:alpha")
        assert sources == {_NS_A}

        neighbours = await adapter.related_neighbors(f"{_NS_A}:alpha")
        assert neighbours == {f"{_NS_A}:beta"}
    finally:
        await adapter.close()


async def test_dynamic_path_structural_scope_and_explain_gate_on_real_neo4j(
    neo4j_settings: Settings,
) -> None:
    """A scope-proof read query passes validation and the EXPLAIN gate on real Neo4j."""
    policy = StructuralCypherPolicy()
    query = "MATCH (n:Entity) WHERE n.id = $entity_id RETURN n.name AS name LIMIT 10"

    result = policy.validate(query, require_scope_proof=True)
    assert result.scope_bound is True
    assert result.explain_required is True
    policy.require_explain(query, explain_applied=True)

    adapter = Neo4jQueryAdapter(neo4j_settings)
    try:
        await adapter.explain(query, {"entity_id": f"{_NS_A}:alpha"})
    finally:
        await adapter.close()

    # A write clause and a missing scope proof both fail closed before EXPLAIN.
    with pytest.raises(StructuralPolicyViolationError):
        policy.validate("MATCH (n:Entity) DETACH DELETE n", require_scope_proof=True)
    with pytest.raises(StructuralPolicyViolationError, match="scope"):
        policy.validate("MATCH (n:Entity) RETURN n LIMIT 10", require_scope_proof=True)


async def test_query_cypher_disabled_returns_typed_error_never_contacts_graph(
    neo4j_settings: Settings,
    tmp_path: Path,
) -> None:
    """The disabled dynamic tool returns a typed policy error without touching the graph."""
    adapter = _mcp_adapter(neo4j_settings, _resolver(tmp_path))
    graph_port = adapter._graph_query_port
    assert isinstance(graph_port, Neo4jQueryAdapter)
    try:
        result = await adapter.query_cypher("what patterns mitigate risk?")

        assert result["error"] == "query_cypher is disabled by default"
        assert result["error_code"] == "policy_violation"
        assert result["cypher"] is None
        assert result["rows"] == []
    finally:
        await graph_port.close()
