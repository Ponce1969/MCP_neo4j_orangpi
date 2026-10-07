"""Soft-deleted entities must not reach the S3 scorer or the legacy merge planner.

Block C in ``odd/backlog.md``: the community adapter was fixed first; these two reads came next,
because they feed a pipeline or a score.

- ``Neo4jNeighborhoodQueryAdapter.related_neighbors`` feeds the S3 neighborhood jaccard in
  ``ResolveEntitiesUseCase``, so a ghost neighbour could tilt a resolution score.
- ``scripts/resolve_entities.py::load_entities`` feeds ``build_merge_plan`` / ``apply_merges``
  in the legacy pipeline, so it could plan a merge onto an already merged ghost.

Both now apply the predicate every other read path uses,
``(n.merged_into IS NULL OR n.merged_into = '')``. ``count_entities`` / ``list_entities`` /
``traverse_relationships`` / ``find_path`` stay unfixed on purpose: they are visible through the
MCP tools and move documented baselines, so they need their own decision (Block C).

These are testcontainers integration tests and never touch production.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from book_graph_rag.infrastructure.neo4j_neighborhood_query_adapter import (
    Neo4jNeighborhoodQueryAdapter,
)

_LIVE = "knowledge:alpha:live-concept"
_OTHER = "knowledge:alpha:other-concept"
_GHOST = "knowledge:alpha:ghost-concept"
_SCRIPT_PATH = Path("scripts/resolve_entities.py")


def _load_resolve_entities_module() -> Any:
    """Load ``scripts/resolve_entities.py`` as a module for testing."""
    spec = importlib.util.spec_from_file_location("resolve_entities", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["resolve_entities"] = module
    spec.loader.exec_module(module)
    return module


async def _seed_live_and_ghost(driver: Any) -> None:
    """One live entity related to a live peer and to a merged ghost."""
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


async def test_related_neighbors_excludes_merged_entities(neo4j_driver: Any) -> None:
    """A ghost neighbour must not reach the S3 neighborhood jaccard."""
    await _seed_live_and_ghost(neo4j_driver)

    adapter = Neo4jNeighborhoodQueryAdapter(neo4j_driver)
    neighbors = await adapter.related_neighbors(_LIVE)

    assert _OTHER in neighbors
    assert _GHOST not in neighbors


async def test_legacy_load_entities_excludes_merged_entities(neo4j_driver: Any) -> None:
    """The legacy merge planner must not plan a merge onto an already merged ghost."""
    await _seed_live_and_ghost(neo4j_driver)

    module = _load_resolve_entities_module()
    entities = await module.load_entities(neo4j_driver)

    ids = {entity.id for entity in entities}
    assert _LIVE in ids
    assert _OTHER in ids
    assert _GHOST not in ids
