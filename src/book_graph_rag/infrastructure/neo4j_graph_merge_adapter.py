"""Neo4j implementation of GraphMergePort.

All writes are executed inside a single transaction so merges and rollbacks are
atomic. The adapter captures the pre-merge state (aliases + edge endpoints) as an
``InverseMappingSnapshot`` that the rollback use case can replay later.
"""

from __future__ import annotations

from typing import Any

from book_graph_rag.domain.merge_ledger_models import (
    EdgeInverseMap,
    FoldedAlias,
    MergeLedgerEntry,
)
from book_graph_rag.domain.resolution_errors import ResolutionError
from book_graph_rag.ports.graph_merge_port import (
    GraphMergePort,
    InverseMappingSnapshot,
)

# Capture aliases for every candidate *before* mutation.
_CAPTURE_ALIASES = """
MATCH (dup:Entity) WHERE dup.id IN $ids
RETURN dup.id AS id, dup.aliases AS aliases
"""

# Capture edge endpoints for every candidate *before* mutation.
_CAPTURE_EDGES = """
MATCH (c:Chunk)-[m:MENTIONS]->(dup:Entity) WHERE dup.id IN $ids
RETURN dup.id AS id,
       'MENTIONS' AS edge_kind,
       CASE WHEN c.id IS NOT NULL
            THEN c.id
            ELSE c.source_id + ':chunk-' + toString(c.chunk_index)
       END AS other_id,
       properties(m) AS props
UNION
MATCH (dup:Entity)-[r:RELATED]-(other:Entity) WHERE dup.id IN $ids
RETURN dup.id AS id,
       'RELATED' AS edge_kind,
       other.id AS other_id,
       properties(r) AS props
"""

# Mark duplicates as soft-deleted (R6.4).
_MARK_MERGED_INTO = """
MATCH (dup:Entity) WHERE dup.id IN $dup_ids
SET dup.merged_into = $canonical_id,
    dup.merged_at = datetime()
"""

# Move (:Chunk)-[:MENTIONS]->(duplicate) to (:Chunk)-[:MENTIONS]->(canonical).
_REPOINT_MENTIONS_BATCH = """
UNWIND $mentions AS inv
MATCH (c:Chunk)
WHERE (c.id = inv.original_other_endpoint_id)
   OR (c.source_id + ':chunk-' + toString(c.chunk_index) = inv.original_other_endpoint_id)
MATCH (c)-[m:MENTIONS]->(dup:Entity {id: inv.duplicate_entity_id})
MATCH (canon:Entity {id: $canonical_id})
MERGE (c)-[m2:MENTIONS]->(canon)
ON CREATE SET m2 += inv.edge_properties
ON MATCH SET m2 += inv.edge_properties
DELETE m
"""

# Move duplicate->other RELATED edges to canonical->other.
_REPOINT_RELATED_OUT_BATCH = """
UNWIND $related AS inv
MATCH (dup:Entity {id: inv.duplicate_entity_id})-[r:RELATED]->(other:Entity {
    id: inv.original_other_endpoint_id
})
WHERE r.type = inv.edge_properties.type
MATCH (canon:Entity {id: $canonical_id})
MERGE (canon)-[r2:RELATED {type: r.type}]->(other)
ON CREATE SET r2 += properties(r)
ON MATCH SET r2 += properties(r)
DELETE r
"""

# Move other->duplicate RELATED edges to other->canonical.
_REPOINT_RELATED_IN_BATCH = """
UNWIND $related AS inv
MATCH (other:Entity {id: inv.original_other_endpoint_id})-[r:RELATED]->(dup:Entity {
    id: inv.duplicate_entity_id
})
WHERE r.type = inv.edge_properties.type
MATCH (canon:Entity {id: $canonical_id})
MERGE (other)-[r2:RELATED {type: r.type}]->(canon)
ON CREATE SET r2 += properties(r)
ON MATCH SET r2 += properties(r)
DELETE r
"""

# Append folded aliases onto the canonical entity without duplicates.
_FOLD_ALIASES = """
UNWIND $aliases AS a
MATCH (canon:Entity {id: $canonical_id})
WITH canon, a.alias_value AS alias_value
WHERE NOT alias_value IN coalesce(canon.aliases, [])
SET canon.aliases = coalesce(canon.aliases, []) + alias_value
"""

# Remove aliases that were folded onto the canonical entity by this merge.
_ROLLBACK_REMOVE_ALIASES = """
MATCH (canon:Entity {id: $canonical_id})
WITH canon, [x IN coalesce(canon.aliases, []) WHERE NOT x IN $alias_values] AS cleaned
SET canon.aliases = cleaned
"""

# Delete edges that were re-pointed onto the canonical entity.
_ROLLBACK_REMOVE_CANON_MENTIONS = """
UNWIND $mentions AS inv
MATCH (c:Chunk)-[m:MENTIONS]->(canon:Entity {id: $canonical_id})
WHERE (c.id = inv.original_other_endpoint_id)
   OR (c.source_id + ':chunk-' + toString(c.chunk_index) = inv.original_other_endpoint_id)
DELETE m
"""

_ROLLBACK_REMOVE_CANON_RELATED = """
UNWIND $related AS inv
MATCH (canon:Entity {id: $canonical_id})-[r:RELATED]-(other:Entity {
    id: inv.original_other_endpoint_id
})
WHERE r.type = inv.edge_properties.type
DELETE r
"""

# Restore the original edges onto the duplicate entities.
_ROLLBACK_RESTORE_MENTIONS = """
UNWIND $mentions AS inv
MATCH (c:Chunk)
WHERE (c.id = inv.original_other_endpoint_id)
   OR (c.source_id + ':chunk-' + toString(c.chunk_index) = inv.original_other_endpoint_id)
MATCH (dup:Entity {id: inv.duplicate_entity_id})
MERGE (c)-[m:MENTIONS]->(dup)
ON CREATE SET m += inv.edge_properties
"""

_ROLLBACK_RESTORE_RELATED_OUT = """
UNWIND $related AS inv
MATCH (dup:Entity {id: inv.duplicate_entity_id}),
      (other:Entity {id: inv.original_other_endpoint_id})
MERGE (dup)-[r:RELATED {type: inv.edge_properties.type}]->(other)
ON CREATE SET r += inv.edge_properties
"""

_ROLLBACK_RESTORE_RELATED_IN = """
UNWIND $related AS inv
MATCH (dup:Entity {id: inv.duplicate_entity_id}),
      (other:Entity {id: inv.original_other_endpoint_id})
MERGE (other)-[r:RELATED {type: inv.edge_properties.type}]->(dup)
ON CREATE SET r += inv.edge_properties
"""

# Clear the soft-delete markers from duplicates.
_ROLLBACK_REMOVE_MERGED = """
MATCH (dup:Entity) WHERE dup.id IN $dup_ids
REMOVE dup.merged_into, dup.merged_at
"""


async def _build_inverse_mapping(
    aliases_result: Any, edges_result: Any
) -> InverseMappingSnapshot:
    """Build an ``InverseMappingSnapshot`` from Neo4j alias + edge results."""
    aliases_before: dict[str, tuple[str, ...]] = {}
    async for record in aliases_result:
        entity_id: str = record["id"]
        aliases = record["aliases"] or []
        aliases_before[entity_id] = tuple(aliases)

    edge_inverse_map: list[EdgeInverseMap] = []
    async for record in edges_result:
        other_id = record["other_id"]
        if other_id is None:
            continue
        edge_inverse_map.append(
            EdgeInverseMap(
                edge_kind=record["edge_kind"],
                duplicate_entity_id=record["id"],
                original_other_endpoint_id=other_id,
                edge_properties=dict(record["props"] or {}),
            )
        )

    return InverseMappingSnapshot(
        aliases_before=aliases_before,
        edge_inverse_map=edge_inverse_map,
    )


class Neo4jGraphMergeAdapter(GraphMergePort):
    """Atomic merge application and rollback backed by a Neo4j transaction."""

    def __init__(self, driver: Any) -> None:
        self._driver = driver

    async def capture_inverse_mapping(
        self, candidate_ids: list[str]
    ) -> InverseMappingSnapshot:
        """Read aliases and edge endpoints for ``candidate_ids`` pre-merge."""
        async with self._driver.session() as session:
            aliases_result = await session.run(
                _CAPTURE_ALIASES,
                {"ids": candidate_ids},
            )
            edges_result = await session.run(
                _CAPTURE_EDGES,
                {"ids": candidate_ids},
            )
            return await _build_inverse_mapping(aliases_result, edges_result)

    async def apply_merge(
        self,
        canonical_id: str,
        candidate_ids: list[str],
        aliases_folded: list[FoldedAlias],
        inverse_mapping: InverseMappingSnapshot,
    ) -> None:
        """Atomically mark, re-point edges, and fold aliases in one transaction."""
        mentions = [e for e in inverse_mapping.edge_inverse_map if e.edge_kind == "MENTIONS"]
        related = [e for e in inverse_mapping.edge_inverse_map if e.edge_kind == "RELATED"]

        async with self._driver.session() as session:
            tx = await session.begin_transaction()
            try:
                await tx.run(
                    _MARK_MERGED_INTO,
                    {"canonical_id": canonical_id, "dup_ids": candidate_ids},
                )
                if mentions:
                    await tx.run(
                        _REPOINT_MENTIONS_BATCH,
                        {
                            "canonical_id": canonical_id,
                            "mentions": [e.model_dump(mode="json") for e in mentions],
                        },
                    )
                if related:
                    await tx.run(
                        _REPOINT_RELATED_OUT_BATCH,
                        {
                            "canonical_id": canonical_id,
                            "related": [e.model_dump(mode="json") for e in related],
                        },
                    )
                    await tx.run(
                        _REPOINT_RELATED_IN_BATCH,
                        {
                            "canonical_id": canonical_id,
                            "related": [e.model_dump(mode="json") for e in related],
                        },
                    )
                if aliases_folded:
                    await tx.run(
                        _FOLD_ALIASES,
                        {
                            "canonical_id": canonical_id,
                            "aliases": [a.model_dump(mode="json") for a in aliases_folded],
                        },
                    )
                await tx.commit()
            except Exception as exc:
                await tx.rollback()
                raise ResolutionError(f"apply_merge failed for {canonical_id}: {exc}") from exc

    async def rollback_merge(self, entry: MergeLedgerEntry) -> None:
        """Reverse a previously applied merge transaction."""
        mentions = [e for e in entry.edge_inverse_map if e.edge_kind == "MENTIONS"]
        related = [e for e in entry.edge_inverse_map if e.edge_kind == "RELATED"]
        alias_values = [a.alias_value for a in entry.aliases_folded]

        async with self._driver.session() as session:
            tx = await session.begin_transaction()
            try:
                if alias_values:
                    await tx.run(
                        _ROLLBACK_REMOVE_ALIASES,
                        {
                            "canonical_id": entry.canonical_id,
                            "alias_values": alias_values,
                        },
                    )
                if mentions:
                    await tx.run(
                        _ROLLBACK_REMOVE_CANON_MENTIONS,
                        {
                            "canonical_id": entry.canonical_id,
                            "mentions": [e.model_dump(mode="json") for e in mentions],
                        },
                    )
                if related:
                    await tx.run(
                        _ROLLBACK_REMOVE_CANON_RELATED,
                        {
                            "canonical_id": entry.canonical_id,
                            "related": [e.model_dump(mode="json") for e in related],
                        },
                    )
                if mentions:
                    await tx.run(
                        _ROLLBACK_RESTORE_MENTIONS,
                        {"mentions": [e.model_dump(mode="json") for e in mentions]},
                    )
                if related:
                    await tx.run(
                        _ROLLBACK_RESTORE_RELATED_OUT,
                        {"related": [e.model_dump(mode="json") for e in related]},
                    )
                    await tx.run(
                        _ROLLBACK_RESTORE_RELATED_IN,
                        {"related": [e.model_dump(mode="json") for e in related]},
                    )
                await tx.run(
                    _ROLLBACK_REMOVE_MERGED,
                    {"dup_ids": entry.candidate_ids},
                )
                await tx.commit()
            except Exception as exc:
                await tx.rollback()
                raise ResolutionError(
                    f"rollback_merge failed for seq={entry.seq}: {exc}"
                ) from exc
