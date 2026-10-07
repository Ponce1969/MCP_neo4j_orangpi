"""``load_entity_graph`` must read the live graph, not the soft-deleted ghosts.

A merged entity keeps its ``merged_into`` marker; the audit, command, quarantine-review and
namespace-profile read paths exclude those nodes with
``(n.merged_into IS NULL OR n.merged_into = '')``. The community adapter did not, so
Leiden clustered ghosts plus the edges pointing at them -- exactly the dangling edges the
audit already reports as ``ENDPOINT_*_MERGED_INVALID``. ``scripts-ops/run_communities_scoped.py``
re-queries the active ids and filters ``merged_into`` in Python; that half of its workaround is what
this change makes redundant (its ``STARTS WITH $prefix`` namespace scoping stays necessary, because
the adapter reads the whole graph).

Still unfiltered elsewhere (recorded in ``odd/backlog.md``, Block C, and deliberately out of this
change): the query adapter's ``count_entities``/``list_entities``/``traverse_relationships``/
``find_path``, the neighborhood scorer's ``related_neighbors``, and the legacy
``scripts/resolve_entities.py`` loader.

These are testcontainers integration tests and never touch production.
"""

from __future__ import annotations

from typing import Any

from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.community_adapter import Neo4jCommunityAdapter

_LIVE = "knowledge:alpha:live-concept"
_OTHER = "knowledge:alpha:other-concept"
_GHOST = "knowledge:alpha:ghost-concept"
_ORPHAN = "knowledge:alpha:orphan-concept"
_GHOST_ORPHAN = "knowledge:alpha:ghost-orphan"


async def _seed_live_and_ghost(driver: Any) -> None:
    """Seed two live entities, one merged ghost and a RELATED edge into the ghost."""
    async with driver.session() as session:
        await session.run(
            """
            MERGE (live:Entity {id: $live_id})
            SET live.name = 'Live Concept', live.type = 'concept', live.source_page = 1,
                live.description = 'A live concept'
            MERGE (other:Entity {id: $other_id})
            SET other.name = 'Other Concept', other.type = 'concept', other.source_page = 2,
                other.description = 'Another live concept'
            MERGE (ghost:Entity {id: $ghost_id})
            SET ghost.name = 'Ghost Concept', ghost.type = 'concept', ghost.source_page = 3,
                ghost.description = 'A merged-away concept',
                ghost.merged_into = $live_id, ghost.merged_at = '2026-10-02T00:00:00Z'
            MERGE (live)-[r:RELATED]->(other)
                SET r.type = 'requires', r.description = '', r.source_page = 1, r.chunk_index = 0
            MERGE (live)-[dangling:RELATED]->(ghost)
                SET dangling.type = 'requires', dangling.description = '', dangling.source_page = 1,
                    dangling.chunk_index = 1
            """,
            live_id=_LIVE,
            other_id=_OTHER,
            ghost_id=_GHOST,
        )


async def test_load_entity_graph_excludes_merged_entities(
    neo4j_settings: Settings, neo4j_driver: Any
) -> None:
    """A merged entity and the edges pointing at it never enter the cluster graph."""
    await _seed_live_and_ghost(neo4j_driver)

    adapter = Neo4jCommunityAdapter(neo4j_settings)
    try:
        entities, relationships = await adapter.load_entity_graph()
    finally:
        await adapter.close()

    ids = {entity.id for entity in entities}
    assert _LIVE in ids
    assert _OTHER in ids
    assert _GHOST not in ids

    pairs = {(rel.source_entity_id, rel.target_entity_id) for rel in relationships}
    assert (_LIVE, _OTHER) in pairs
    assert (_LIVE, _GHOST) not in pairs
    assert all(_GHOST not in pair for pair in pairs)


async def test_get_isolated_entities_excludes_merged_entities(
    neo4j_settings: Settings, neo4j_driver: Any
) -> None:
    """An orphan report must not present a merged-away node as an isolated entity."""
    async with neo4j_driver.session() as session:
        await session.run(
            """
            MERGE (orphan:Entity {id: $orphan_id})
            SET orphan.name = 'Orphan Concept', orphan.type = 'concept', orphan.source_page = 1,
                orphan.description = 'A live isolated concept'
            MERGE (ghost:Entity {id: $ghost_id})
            SET ghost.name = 'Ghost Orphan', ghost.type = 'concept', ghost.source_page = 2,
                ghost.description = 'A merged-away concept', ghost.merged_into = $orphan_id
            """,
            orphan_id=_ORPHAN,
            ghost_id=_GHOST_ORPHAN,
        )

    adapter = Neo4jCommunityAdapter(neo4j_settings)
    try:
        isolated = await adapter.get_isolated_entities()
    finally:
        await adapter.close()

    names = {record["name"] for record in isolated}
    assert "Orphan Concept" in names
    assert "Ghost Orphan" not in names
