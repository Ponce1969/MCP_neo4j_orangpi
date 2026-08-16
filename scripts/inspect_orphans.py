"""List isolated (degree-0) ``:Entity`` nodes for quick inspection.

Isolated entities have no ``:RELATED`` edges, so Leiden ignores them and they are
only covered by the level-0 global summary. If they are extraction noise (chapter
markers, generic terms) leaving them is fine; if they are valuable book entities
that ended up orphaned, the real fix is upstream in the relation-extraction prompt.

Usage:
    uv run python scripts/inspect_orphans.py
    uv run --extra community python scripts/inspect_orphans.py   # same, with extras
"""

from __future__ import annotations

import asyncio

from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.community_adapter import Neo4jCommunityAdapter


async def _main() -> None:
    settings = Settings.model_validate({})
    adapter = Neo4jCommunityAdapter(settings)
    try:
        orphans = await adapter.get_isolated_entities(limit=20)
    finally:
        await adapter.close()

    if not orphans:
        print("No isolated entities found.")
        return
    print(f"{len(orphans)} isolated entities (degree 0):")
    for orphan in orphans:
        name = orphan.get("name")
        entity_type = orphan.get("type")
        print(f"  - {name!r}  [{entity_type}]")


if __name__ == "__main__":
    asyncio.run(_main())
