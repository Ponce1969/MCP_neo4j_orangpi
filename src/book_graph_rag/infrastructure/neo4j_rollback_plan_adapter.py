"""Neo4j implementation of RollbackPlanPort (T8c). Read-only by construction."""

from __future__ import annotations

from typing import Any

from book_graph_rag.domain.rollback_plan_models import EdgeCensus, RelatedEdgeProbe
from book_graph_rag.ports.rollback_plan_port import RollbackPlanPort

# Both endpoints must exist; inline WHERE inside the pattern comprehension
# filters by relationship type. A missing endpoint yields no row, which the
# adapter reports as (False, False) -> planner verdict "unknown" (legacy path).
_PROBE_RELATED = """
MATCH (a:Entity {id: $a_id}), (b:Entity {id: $b_id})
RETURN size([(a)-[r:RELATED]->(b) WHERE r.type = $edge_type | r]) > 0 AS a_to_b,
       size([(b)-[r:RELATED]->(a) WHERE r.type = $edge_type | r]) > 0 AS b_to_a
"""

# MENTIONS counts (Chunk -> Entity) per affected entity.
_CENSUS_MENTIONS_BY_ENTITY = """
MATCH (n:Entity) WHERE n.id IN $ids
OPTIONAL MATCH (c:Chunk)-[:MENTIONS]->(n)
RETURN n.id AS id, count(c) AS mentions
"""

# RELATED counts (both directions) per affected entity.
_CENSUS_RELATED_BY_ENTITY = """
MATCH (n:Entity) WHERE n.id IN $ids
OPTIONAL MATCH (n)-[r:RELATED]-(:Entity)
RETURN n.id AS id, count(r) AS related
"""

# Db-wide totals. Aggregation-only queries return a single row even when the
# match set is empty, so count() is always 0 rather than "no result".
_TOTAL_MENTIONS = "MATCH (m:MENTIONS) RETURN count(m) AS c"
_TOTAL_RELATED = "MATCH (r:RELATED) RETURN count(r) AS c"
_TOTAL_MERGED_INTO = """
MATCH (e:Entity) WHERE e.merged_into IS NOT NULL RETURN count(e) AS c
"""


class Neo4jRollbackPlanAdapter(RollbackPlanPort):
    """Read-only Neo4j probes for the T8c rollback planner."""

    def __init__(self, driver: Any) -> None:
        self._driver = driver

    async def probe_related_edges(
        self,
        *,
        a_id: str,
        b_id: str,
        edge_type: str,
    ) -> RelatedEdgeProbe:
        """Probe both RELATED directions between two entities (see the port)."""
        async with self._driver.session() as session:
            result = await session.run(
                _PROBE_RELATED,
                {"a_id": a_id, "b_id": b_id, "edge_type": edge_type},
            )
            record = await result.single()
            if record is None:
                # One (or both) endpoints do not exist: nothing can be restored
                # from a live edge, so the planner must fall back and report.
                return RelatedEdgeProbe(a_to_b=False, b_to_a=False)
            return RelatedEdgeProbe(
                a_to_b=bool(record["a_to_b"]),
                b_to_a=bool(record["b_to_a"]),
            )

    async def read_edge_census(self, entity_ids: list[str]) -> EdgeCensus:
        """Read per-entity edge counts plus db-wide totals (read-only)."""
        mentions_by_entity: dict[str, int] = {}
        related_by_entity: dict[str, int] = {}
        async with self._driver.session() as session:
            if entity_ids:
                mentions_result = await session.run(
                    _CENSUS_MENTIONS_BY_ENTITY,
                    {"ids": entity_ids},
                )
                async for record in mentions_result:
                    mentions_by_entity[str(record["id"])] = int(record["mentions"])
                related_result = await session.run(
                    _CENSUS_RELATED_BY_ENTITY,
                    {"ids": entity_ids},
                )
                async for record in related_result:
                    related_by_entity[str(record["id"])] = int(record["related"])

            total_mentions = await (await session.run(_TOTAL_MENTIONS)).single()
            total_related = await (await session.run(_TOTAL_RELATED)).single()
            merged_into = await (await session.run(_TOTAL_MERGED_INTO)).single()
            assert total_mentions is not None
            assert total_related is not None
            assert merged_into is not None
            return EdgeCensus(
                mentions_by_entity=mentions_by_entity,
                related_by_entity=related_by_entity,
                total_mentions=int(total_mentions["c"]),
                total_related=int(total_related["c"]),
                merged_into_count=int(merged_into["c"]),
            )

    async def close(self) -> None:
        """Close the underlying Neo4j driver."""
        await self._driver.close()
