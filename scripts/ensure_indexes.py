"""Create read-side Neo4j indexes idempotently.

Run once against the target Neo4j instance (e.g. the Orange Pi) after the
Fase 06 Query Layer is deployed. All CREATE INDEX statements use
``IF NOT EXISTS``, so the script is safe to run repeatedly.
"""

from __future__ import annotations

import asyncio
import sys

from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.neo4j_query_adapter import Neo4jQueryAdapter


async def main() -> int:
    """Create all read-side indexes and exit cleanly."""
    settings = Settings()  # type: ignore[call-arg]
    adapter = Neo4jQueryAdapter(settings)
    try:
        await adapter.ensure_indexes()
    finally:
        await adapter.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
