"""Soft-deleted entities must not reach the four MCP-visible reads.

Block C in ``odd/backlog.md``: the community adapter and the two pipeline reads were fixed
first; these four are the last ones, and they are the ones a caller sees through the MCP
tools, so fixing them moves the counts the runbook and the smoke document.

- ``count_entities`` and ``list_entities`` counted and returned ghosts, while the
  ``find_entity`` tiers in the same adapter already excluded them.
- ``traverse_relationships`` and ``find_path`` walked *through* ghosts, so a caller could
  still see an entity that resolution had merged away.

All four now apply the predicate every other read path uses,
``(n.merged_into IS NULL OR n.merged_into = '')``, and the two path reads apply it to
**every** node in the path, not just the endpoints: a ghost in the middle of a chain must
not make the two ends look connected.

These are testcontainers integration tests and never touch production.
"""

from __future__ import annotations

from typing import Any

from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.neo4j_query_adapter import Neo4jQueryAdapter
from book_graph_rag.mcp_server_main import _CATALOG_STATS_CYPHER, _stats_from_rows

_LIVE = "knowledge:alpha:live-concept"
_PEER = "knowledge:alpha:peer-concept"
_FAR = "knowledge:alpha:far-concept"
_GHOST = "knowledge:alpha:ghost-concept"
_LIVE_IDS = {_LIVE, _PEER, _FAR}


async def _seed_graph_with_a_ghost(driver: Any) -> None:
    """Three live entities plus one merged ghost sitting in the middle of a chain.

    ``live -> ghost -> far`` is the interesting shape: the only route from ``live`` to
    ``far`` passes through the ghost, so a read that filters only the endpoints still
    returns a path that should not exist.
    """
    async with driver.session() as session:
        await session.run(
            """
            MERGE (live:Entity {id: $live_id})
            SET live.name = 'Live Concept', live.type = 'concept', live.source_page = 1,
                live.description = 'A live concept'
            MERGE (peer:Entity {id: $peer_id})
            SET peer.name = 'Peer Concept', peer.type = 'concept', peer.source_page = 2,
                peer.description = 'A live peer'
            MERGE (far:Entity {id: $far_id})
            SET far.name = 'Far Concept', far.type = 'concept', far.source_page = 3,
                far.description = 'Only reachable through the ghost'
            MERGE (ghost:Entity {id: $ghost_id})
            SET ghost.name = 'Ghost Concept', ghost.type = 'concept', ghost.source_page = 4,
                ghost.description = 'A merged-away concept',
                ghost.merged_into = $live_id, ghost.merged_at = '2026-10-02T00:00:00Z'
            MERGE (live)-[r1:RELATED]->(peer)
                SET r1.type = 'requires', r1.description = '', r1.source_page = 1,
                    r1.chunk_index = 0
            MERGE (live)-[r2:RELATED]->(ghost)
                SET r2.type = 'requires', r2.description = '', r2.source_page = 1,
                    r2.chunk_index = 1
            MERGE (ghost)-[r3:RELATED]->(far)
                SET r3.type = 'requires', r3.description = '', r3.source_page = 4,
                    r3.chunk_index = 2
            """,
            live_id=_LIVE,
            peer_id=_PEER,
            far_id=_FAR,
            ghost_id=_GHOST,
        )


async def test_count_entities_excludes_merged_entities(
    neo4j_settings: Settings, neo4j_driver: Any
) -> None:
    """The count a caller sees through the MCP must not include ghosts."""
    await _seed_graph_with_a_ghost(neo4j_driver)

    adapter = Neo4jQueryAdapter(neo4j_settings)

    assert await adapter.count_entities(None) == len(_LIVE_IDS)


async def test_list_entities_excludes_merged_entities(
    neo4j_settings: Settings, neo4j_driver: Any
) -> None:
    """``list_entities`` must not hand ghost records to a caller."""
    await _seed_graph_with_a_ghost(neo4j_driver)

    adapter = Neo4jQueryAdapter(neo4j_settings)
    entities, _ = await adapter.list_entities(0, 100)

    ids = {entity.entity.id for entity in entities}
    assert ids == _LIVE_IDS


async def test_traverse_relationships_excludes_merged_neighbours(
    neo4j_settings: Settings, neo4j_driver: Any
) -> None:
    """A live entity's neighbourhood must not contain the ghost it points at."""
    await _seed_graph_with_a_ghost(neo4j_driver)

    adapter = Neo4jQueryAdapter(neo4j_settings)
    entities, _ = await adapter.traverse_relationships(_LIVE, None, 1)

    ids = {entity.entity.id for entity in entities}
    assert _GHOST not in ids
    assert {_LIVE, _PEER} <= ids


async def test_traverse_relationships_from_a_ghost_is_empty(
    neo4j_settings: Settings, neo4j_driver: Any
) -> None:
    """Asking for a ghost's neighbourhood returns nothing, at depth 0 and beyond."""
    await _seed_graph_with_a_ghost(neo4j_driver)

    adapter = Neo4jQueryAdapter(neo4j_settings)

    assert await adapter.traverse_relationships(_GHOST, None, 0) == ([], [])
    assert await adapter.traverse_relationships(_GHOST, None, 1) == ([], [])


async def test_find_path_never_crosses_a_merged_entity(
    neo4j_settings: Settings, neo4j_driver: Any
) -> None:
    """The only route from ``live`` to ``far`` goes through the ghost, so there is none."""
    await _seed_graph_with_a_ghost(neo4j_driver)

    adapter = Neo4jQueryAdapter(neo4j_settings)

    assert await adapter.find_path(_LIVE, _FAR, 3) == []
    assert await adapter.find_path(_GHOST, _PEER, 3) == []


async def test_catalog_resource_stats_exclude_merged_entities(
    neo4j_settings: Settings, neo4j_driver: Any
) -> None:
    """``bookgraph://catalog`` must not report a different count than ``count_entities``.

    The resource runs its own single-round-trip Cypher rather than the ``count_entities``
    tool, so the two can drift: this is the case an independent verifier found after the
    four tools were fixed, where the same graph answered ``5`` through the resource and
    ``3`` through the tool.
    """
    await _seed_graph_with_a_ghost(neo4j_driver)

    adapter = Neo4jQueryAdapter(neo4j_settings)
    stats = _stats_from_rows(await adapter.execute_read(_CATALOG_STATS_CYPHER))

    assert stats["knowledge:alpha"]["entities"] == len(_LIVE_IDS)
