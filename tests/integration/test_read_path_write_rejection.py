"""Read-path write-vector rejection tests (T-C.4).

Prove the R1 read-only authority invariant against a real testcontainers Neo4j:
a write issued through the adapter's managed read transaction is rejected by the
server and mapped to a typed domain error, and the graph snapshot stays
byte-for-byte unchanged afterwards.
"""

from __future__ import annotations

from typing import Any

import pytest

from book_graph_rag.config import Settings
from book_graph_rag.domain.mcp_security import UnsupportedQueryError
from book_graph_rag.infrastructure.neo4j_query_adapter import Neo4jQueryAdapter

pytestmark = pytest.mark.neo4j_integration


async def _seed_graph(driver: Any) -> None:
    """Seed two entities linked by a relationship for the write-vector tests."""
    async with driver.session() as session:
        await session.run(
            """
            MERGE (a:Entity {id: 'wv:entity-a'})
            SET a.name = 'Write Vector A', a.type = 'concept', a.source_page = 1
            MERGE (b:Entity {id: 'wv:entity-b'})
            SET b.name = 'Write Vector B', b.type = 'concept', b.source_page = 2
            """,
        )
        await session.run(
            """
            MATCH (a:Entity {id: 'wv:entity-a'})
            MATCH (b:Entity {id: 'wv:entity-b'})
            MERGE (a)-[r:RELATED]->(b)
            SET r.type = 'requires', r.source_page = 1, r.chunk_index = 0
            """,
        )


def _snapshot_key_value(value: Any) -> str:
    """Normalize a possibly-None scalar into a sortable string."""
    return "" if value is None else str(value)


async def _graph_snapshot(driver: Any) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    """Return a deterministic snapshot of every node and relationship."""
    async with driver.session() as session:
        nodes_result = await session.run(
            "MATCH (n) "
            "RETURN labels(n) AS labels, n.id AS id, n.name AS name, n.type AS type, "
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
                    _snapshot_key_value(record["source_page"]),
                )
            )

        rels_result = await session.run(
            "MATCH ()-[r]->() "
            "RETURN type(r) AS t, r.type AS prop, r.source_page AS sp, r.chunk_index AS ci"
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


async def test_write_vectors_are_rejected_and_graph_unchanged(
    neo4j_settings: Settings,
    neo4j_driver: Any,
) -> None:
    """CREATE/SET/DELETE through the read path raise UnsupportedQueryError and
    leave the graph snapshot unchanged."""
    await _seed_graph(neo4j_driver)

    write_vectors = [
        "CREATE (n:Entity {id: 'wv:injected'})",
        "MATCH (n:Entity {id: 'wv:entity-a'}) SET n.name = 'mutated'",
        "MATCH (n:Entity {id: 'wv:entity-a'}) DETACH DELETE n",
    ]

    adapter = Neo4jQueryAdapter(neo4j_settings)
    try:
        before = await _graph_snapshot(neo4j_driver)
        assert before[0], "seed graph must contain nodes"

        for cypher in write_vectors:
            with pytest.raises(UnsupportedQueryError, match="Write rejected"):
                await adapter.execute_read(cypher)

        after = await _graph_snapshot(neo4j_driver)
        assert before == after
    finally:
        await adapter.close()
