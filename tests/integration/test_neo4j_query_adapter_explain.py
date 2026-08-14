"""Container-gated EXPLAIN validation for PR4 Cypher queries (REQ-NFR-03)."""

from __future__ import annotations

import os

import pytest

from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.neo4j_query_adapter import Neo4jQueryAdapter

pytestmark = pytest.mark.neo4j_integration


def _live_settings() -> Settings | None:
    """Build Settings from environment, or None if not configured."""
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD")
    if not password:
        return None
    try:
        return Settings.model_validate(
            {"neo4j_uri": uri, "neo4j_user": user, "neo4j_password": password}
        )
    except Exception:  # pragma: no cover
        return None


async def _is_reachable(adapter: Neo4jQueryAdapter) -> bool:
    """Return True if the adapter can run a trivial read against Neo4j."""
    try:
        await adapter.execute_read("RETURN 1 AS n")
        return True
    except Exception:  # pragma: no cover
        return False


@pytest.fixture
async def live_adapter() -> Neo4jQueryAdapter:
    """Real Neo4j adapter; skips when no live container is available."""
    settings = _live_settings()
    if settings is None:
        pytest.skip(
            "NEO4J_PASSWORD not set; no live Neo4j container configured"
        )
    adapter = Neo4jQueryAdapter(settings)
    if not await _is_reachable(adapter):
        await adapter.close()
        pytest.skip("Live Neo4j container is not reachable")
    try:
        yield adapter
    finally:
        await adapter.close()


async def test_explain_pr4_tiered_find_entity_queries(
    live_adapter: Neo4jQueryAdapter,
) -> None:
    """EXPLAIN validates all four find_entity tiers plus batch/index DDL."""
    # Tier 4 and the index DDL require the fulltext index to exist first.
    await live_adapter.ensure_indexes()

    # Tier 1 — exact match.
    await live_adapter.explain(
        """
        MATCH (n:Entity {name: $name})
        WHERE $entity_type IS NULL OR n.type = $entity_type
        OPTIONAL MATCH (c:Chunk)-[:MENTIONS]->(n)
        RETURN n, 1.0 AS score, c.chunk_index AS chunk_index,
               c.book_id AS book_id
        LIMIT $limit
        """,
        {
            "name": "Model Context Protocol",
            "entity_type": "concept",
            "limit": 10,
        },
    )

    # Tier 2 — case-insensitive.
    await live_adapter.explain(
        """
        MATCH (n:Entity)
        WHERE toLower(n.name) = toLower($name)
          AND ($entity_type IS NULL OR n.type = $entity_type)
        OPTIONAL MATCH (c:Chunk)-[:MENTIONS]->(n)
        RETURN n, 0.8 AS score, c.chunk_index AS chunk_index,
               c.book_id AS book_id
        LIMIT $limit
        """,
        {"name": "mcp", "entity_type": "concept", "limit": 10},
    )

    # Tier 3 — partial/CONTAINS.
    await live_adapter.explain(
        """
        MATCH (n:Entity)
        WHERE n.name CONTAINS $name
          AND ($entity_type IS NULL OR n.type = $entity_type)
        OPTIONAL MATCH (c:Chunk)-[:MENTIONS]->(n)
        RETURN n, 0.6 AS score, c.chunk_index AS chunk_index,
               c.book_id AS book_id
        ORDER BY size(n.name) ASC
        LIMIT $limit
        """,
        {"name": "Context", "entity_type": "concept", "limit": 10},
    )

    # Tier 4 — fulltext over names, canonical names and aliases.
    await live_adapter.explain(
        """
        CALL db.index.fulltext.queryNodes("entity_name_aliases_index", $name)
        YIELD node AS n, score AS ft_score
        WHERE $entity_type IS NULL OR n.type = $entity_type
        OPTIONAL MATCH (c:Chunk)-[:MENTIONS]->(n)
        RETURN
            n,
            ft_score * 0.4 AS score,
            c.chunk_index AS chunk_index,
            c.book_id AS book_id
        ORDER BY score DESC
        LIMIT $limit
        """,
        {"name": "MCP", "entity_type": "concept", "limit": 10},
    )

    # find_entities_batch source extraction query.
    await live_adapter.explain(
        """
        UNWIND $ids AS id
        MATCH (n:Entity {id: id})
        OPTIONAL MATCH (c:Chunk)-[:MENTIONS]->(n)
        RETURN n, c.chunk_index AS chunk_index, c.book_id AS book_id
        """,
        {"ids": ["model-context-protocol-concept"]},
    )

    # Fulltext index DDL.
    await live_adapter.explain(
        "CREATE FULLTEXT INDEX entity_name_aliases_index "
        "IF NOT EXISTS FOR (n:Entity) "
        "ON EACH [n.name, n.canonical_name, n.aliases]"
    )
