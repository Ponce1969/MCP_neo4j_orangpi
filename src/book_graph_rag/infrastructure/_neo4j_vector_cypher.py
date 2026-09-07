"""Private Cypher templates for the Neo4j vector candidate retrieval adapter.

These statements are kept in a separate module so the adapter logic remains
readable and the Cypher can be unit-tested in isolation if needed.
"""

from __future__ import annotations

_ENSURE_VECTOR_INDEX = """
CREATE VECTOR INDEX $index_name IF NOT EXISTS
FOR (n:Entity) ON (n.embedding)
OPTIONS {
  indexConfig: {
    `vector.dimensions`: $dim,
    `vector.similarity_function`: $similarity
  }
}
"""

_SET_ENTITY_EMBEDDING = """
MATCH (n:Entity {id: $id})
SET n.embedding = $vec
"""

_VECTOR_QUERY = """
MATCH (anchor:Entity {id: $anchor_id})
CALL db.index.vector.queryNodes($index_name, $raw_top_k, anchor.embedding)
YIELD node AS cand, score
WHERE score >= $min_sim
  AND cand.type = $anchor_type
  AND cand.id <> anchor.id
  AND (cand.merged_into IS NULL OR cand.merged_into = '')
RETURN cand.id AS candidate_id,
       score AS cosine_similarity,
       cand.type AS candidate_type
ORDER BY score DESC
LIMIT $top_k
"""
