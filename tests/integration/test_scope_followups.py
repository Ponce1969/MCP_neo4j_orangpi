"""T-B.3 follow-up integration tests (traverse namespace binding + cursor pagination).

These tests pin the two defects identified in the T-B.3 review:

1. ``traverse_relationships`` must bind the entity namespace — every node in the
   traversed path must share the scope prefix — not just the relationship types.
2. ``list_entities`` cursor pagination must return every scoped entity exactly
   once across pages. The initial ``id(n) > 0`` cursor skipped internal id 0.

Both are proven against a real testcontainers Neo4j.
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

_NS_A = "followups:ns-a"
_NS_B = "followups:ns-b"

_CATALOG = """\
version: 1
corpora:
  followups:
    label: "Follow-ups corpus"
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
    """Build a catalog-backed scope resolver with both namespaces active."""
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text(_CATALOG, encoding="utf-8")
    return CatalogScopeResolver(CatalogLoader(catalog_path))


async def _seed_traverse_graph(driver: Any) -> None:
    """Seed ns-a entities plus a cross-namespace neighbour reachable from ns-a."""
    async with driver.session() as session:
        for eid, name in (
            (f"{_NS_A}:alpha", "Alpha"),
            (f"{_NS_A}:beta", "Beta"),
            (f"{_NS_B}:gamma", "Gamma"),
        ):
            await session.run(
                "MERGE (e:Entity {id: $id}) "
                "SET e.name = $name, e.type = 'concept', e.source_page = 1",
                id=eid,
                name=name,
            )
        # alpha -> beta stays in-namespace; alpha -> gamma crosses into ns-b.
        await session.run(
            """
            MATCH (a:Entity {id: $a})
            MATCH (b:Entity {id: $b})
            MATCH (g:Entity {id: $g})
            MERGE (a)-[r1:RELATED]->(b)
                SET r1.type = 'requires', r1.source_page = 1, r1.chunk_index = 0
            MERGE (a)-[r2:RELATED]->(g)
                SET r2.type = 'requires', r2.source_page = 1, r2.chunk_index = 0
            """,
            a=f"{_NS_A}:alpha",
            b=f"{_NS_A}:beta",
            g=f"{_NS_B}:gamma",
        )


async def _seed_entities(driver: Any, count: int, ns: str) -> list[str]:
    """Seed ``count`` distinct entities in ``ns`` and return their ids."""
    ids = [f"{ns}:entity-{i}" for i in range(count)]
    async with driver.session() as session:
        for i, eid in enumerate(ids):
            await session.run(
                "MERGE (e:Entity {id: $id}) "
                "SET e.name = $name, e.type = 'concept', e.source_page = 1",
                id=eid,
                name=f"Entity {i}",
            )
    return ids


async def test_traverse_relationships_does_not_leak_cross_namespace(
    neo4j_settings: Settings,
    neo4j_driver: Any,
    tmp_path: Path,
) -> None:
    """Traversal scoped to ns-a must never return the ns-b neighbour."""
    await _seed_traverse_graph(neo4j_driver)
    scope = _resolver(tmp_path).resolve(_NS_A)

    adapter = Neo4jQueryAdapter(neo4j_settings)
    try:
        entities, relationships = await adapter.traverse_relationships(
            f"{_NS_A}:alpha", None, 1, scope=scope
        )
    finally:
        await adapter.close()

    returned_ids = {e.entity.id for e in entities}
    assert f"{_NS_A}:alpha" in returned_ids
    assert f"{_NS_A}:beta" in returned_ids
    # The cross-namespace neighbour must never leak into a scoped traversal.
    assert f"{_NS_B}:gamma" not in returned_ids
    assert all(eid.startswith(f"{_NS_A}:") for eid in returned_ids)

    relationship_targets = {r.target_entity_id for r in relationships}
    assert f"{_NS_A}:beta" in relationship_targets
    assert f"{_NS_B}:gamma" not in relationship_targets


async def test_list_entities_pagination_returns_every_entity_exactly_once(
    neo4j_settings: Settings,
    neo4j_driver: Any,
    tmp_path: Path,
) -> None:
    """Cursor pagination must not skip or duplicate any scoped entity."""
    seeded = await _seed_entities(neo4j_driver, 3, _NS_A)
    scope = _resolver(tmp_path).resolve(_NS_A)

    adapter = Neo4jQueryAdapter(neo4j_settings)
    try:
        page1, cursor1 = await adapter.list_entities(0, 2, scope=scope)
        page2, cursor2 = await adapter.list_entities(cursor1, 2, scope=scope)
        page3, _ = await adapter.list_entities(cursor2, 2, scope=scope)
    finally:
        await adapter.close()

    collected = [e.entity.id for e in page1 + page2 + page3]
    assert len(collected) == 3, "pagination skipped or duplicated entities"
    assert len(set(collected)) == 3, "pagination returned duplicate entities"
    assert set(collected) == set(seeded)
