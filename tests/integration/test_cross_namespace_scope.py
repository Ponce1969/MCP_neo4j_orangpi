"""Adversarial cross-namespace scope isolation tests (T-B.3).

These tests prove two spec R3 invariants against a real testcontainers Neo4j:

1. A structured query scoped to one namespace must never return rows belonging
   to another namespace, even when both namespaces contain entities with the
   same name/type and chunks with the same text.
2. Running the scoped structured reads leaves the graph snapshot byte-for-byte
   unchanged (read-only authority).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.catalog_loader import CatalogLoader
from book_graph_rag.infrastructure.catalog_scope_resolver import CatalogScopeResolver
from book_graph_rag.infrastructure.neo4j_query_adapter import Neo4jQueryAdapter

pytestmark = pytest.mark.neo4j_integration

_NS_A = "adversarial:ns-a"
_NS_B = "adversarial:ns-b"

_CATALOG = """\
version: 1
corpora:
  adversarial:
    label: "Adversarial corpus"
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


def _resolver(tmp_path: Path) -> CatalogScopeResolver:
    """Build a catalog-backed scope resolver with both adversarial sources active."""
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text(_CATALOG, encoding="utf-8")
    return CatalogScopeResolver(CatalogLoader(catalog_path))


async def _seed_adversarial_graph(driver: Any) -> None:
    """Seed two namespaces with colliding entity names/types and chunk text."""
    async with driver.session() as session:
        # Entities: ns-a has a "Shared Concept" and an "Only In A"; ns-b has its
        # own "Shared Concept" with the same name and type (adversarial collision).
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
        # Chunks: identical text in both namespaces.
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
        # ns-a's chunk mentions ns-a's entities.
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
    """Normalize a possibly-None scalar into a sortable string."""
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


async def test_scoped_entity_queries_do_not_leak_cross_namespace(
    neo4j_settings: Settings,
    neo4j_driver: Any,
    tmp_path: Path,
) -> None:
    """Entity reads scoped to ns-a must never return ns-b entities."""
    await _seed_adversarial_graph(neo4j_driver)
    scope = _resolver(tmp_path).resolve(_NS_A)

    adapter = Neo4jQueryAdapter(neo4j_settings)
    try:
        count = await adapter.count_entities(None, scope=scope)
        assert count == 2

        entities, _ = await adapter.list_entities(0, 100, scope=scope)
        listed_ids = {entity.entity.id for entity in entities}
        assert listed_ids, "scoped list_entities returned no rows"
        assert all(eid.startswith(f"{_NS_A}:") for eid in listed_ids)

        found = await adapter.find_entity("Shared Concept", None, scope=scope)
        found_ids = {entity.entity.id for entity in found}
        assert found_ids == {f"{_NS_A}:shared-concept"}
    finally:
        await adapter.close()


async def test_scoped_chunk_search_does_not_leak_cross_namespace(
    neo4j_settings: Settings,
    neo4j_driver: Any,
    tmp_path: Path,
) -> None:
    """Chunk search scoped to ns-a must never return ns-b chunks."""
    await _seed_adversarial_graph(neo4j_driver)
    scope = _resolver(tmp_path).resolve(_NS_A)

    adapter = Neo4jQueryAdapter(neo4j_settings)
    try:
        await adapter.ensure_indexes()
        chunks = await adapter.search_chunks("shared token", 10, scope=scope)
        returned_book_ids = {chunk["book_id"] for chunk in chunks}
        assert returned_book_ids == {_NS_A}
    finally:
        await adapter.close()


async def test_scoped_reads_leave_graph_unchanged(
    neo4j_settings: Settings,
    neo4j_driver: Any,
    tmp_path: Path,
) -> None:
    """Every scoped structured read is read-only: the snapshot stays identical."""
    await _seed_adversarial_graph(neo4j_driver)
    scope = _resolver(tmp_path).resolve(_NS_A)

    adapter = Neo4jQueryAdapter(neo4j_settings)
    try:
        await adapter.ensure_indexes()
        before = await _graph_snapshot(neo4j_driver)

        await adapter.count_entities(None, scope=scope)
        await adapter.list_entities(0, 100, scope=scope)
        await adapter.find_entity("Shared Concept", None, scope=scope)
        await adapter.search_chunks("shared token", 10, scope=scope)

        after = await _graph_snapshot(neo4j_driver)
        assert before == after
    finally:
        await adapter.close()
