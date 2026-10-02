# Tech debt: edges pointing at soft-deleted entities (`essential-graphrag`)

- **Status**: open — prioritized right after the Book 4 close-out
- **Opened**: 2026-10-01
- **Measured scope**: `knowledge:essential-graphrag` only; the same class can exist in any namespace
  where merges ran before the adapter fix
- **Engram**: obs 1449 (`defect/edge-integrity-and-audit-gaps`) + obs 1450 (Book 4 close-out state)
- **Related**: `odd/tasks/book4-postindex-closure.md` (T8), commit `7664b19` (the historical adapter fix),
  commit `85167c7` (`scripts-ops/cleanup_dangling_edges.py`)

## Problem

Measured on the OrangePi on 2026-10-01 (read-only, live production graph):

| Class | Count | Meaning |
|-------|-------|---------|
| MENTIONS to a soft-deleted entity | **90** | `(:Chunk)-[:MENTIONS]->(:Entity)` where the entity has `merged_into` |
| RELATED touching a soft-deleted entity | **196** | `(:Entity)-[r:RELATED]-(:Entity)` with at least one endpoint carrying `merged_into` |
| Edges with **both** endpoints merged | 0 | `a.merged_into IS NOT NULL AND b.merged_into IS NOT NULL` |

All 286 edges belong to `knowledge:essential-graphrag`. The Book 4 namespace is at 0 of both after the
2026-10-01 cleanup; 196 is exactly the remainder that `scripts-ops/cleanup_dangling_edges.py` reported
when it cleaned the Book 4 case with explicit approval. The other two namespaces are clean.

Reproduce:

```cypher
MATCH (c:Chunk)-[:MENTIONS]->(e:Entity) WHERE e.merged_into IS NOT NULL
RETURN split(e.id, ':')[0] + ':' + split(e.id, ':')[1] AS ns, count(*) ORDER BY ns DESC;

MATCH (a:Entity)-[r:RELATED]->(b:Entity)
WHERE a.merged_into IS NOT NULL OR b.merged_into IS NOT NULL
RETURN split(coalesce(a.id, b.id), ':')[0] + ':' + split(coalesce(a.id, b.id), ':')[1] AS ns,
       count(r) ORDER BY ns DESC;
```

## Root cause

Left behind by the September 2026 resolution of that namespace, **before** `7664b19` fixed the
adapter's chunk-key resolution. With the broken key, `capture_inverse_mapping` produced a null
other-endpoint, `_build_inverse_mapping` dropped the edge, and the re-point batches never ran: the
duplicate was soft-deleted while its edges stayed where they were. Two mechanisms:

1. **Unre-pointed edges of a soft-deleted duplicate** — the 90 MENTIONS.
2. **Intra-group RELATED edges** — `_REPOINT_RELATED_OUT_BATCH` / `_REPOINT_RELATED_IN_BATCH` use
   `MERGE (canon)-[r2:RELATED {type}]->(other)` without excluding `other` = the canonical or a sibling
   duplicate, so the edge survives pointing at a soft-deleted node. In Book 4 this happened exactly
   once (`flop-s-concept <- contrasts_with <- operaciones-de-punto-flotante-por-segundo-concept`),
   which is why the rate is measurable as "1 per group with intra-group RELATED edges".

## Risk

- **Invisible to the gates**: `RULE_CATALOG` has no rule for "endpoint is merged" — `ENDPOINT_RELATED_INVALID`
  and its siblings only check the node label and null ids. Scoped and global audits therefore report
  `passed 0/0/0` on a graph carrying 286 junk edges, and the readiness gate `expose-mcp` (audit-backed)
  inherits the blind spot.
- **Traversal pollution**: `traverse_relationships`, `search_rag` and community summarization can walk
  into soft-deleted nodes. Code paths that filter `merged_into` and paths that do not return different
  counts for the same question, so a duplicated concept can surface or a path can disappear.
- **Resolution and rollback**: a later merge in that namespace captures an inverse mapping that already
  contains those edges, so a rollback can restore them onto the wrong endpoint.
- **Growth**: without the adapter fix, every future merge in a namespace with intra-group RELATED edges
  adds one more (measured rate: 1 per group).

## Plan (backup -> dry-run -> approval -> apply -> audit)

1. **TDD on the adapter** (`infrastructure/neo4j_graph_merge_adapter.py`): a fake-driver unit test that
   proves an intra-group RELATED edge does not survive as an edge to a soft-deleted node; then fix the
   two re-point batches to exclude same-group members and collapse to the canonical instead of creating
   the self-loop.
2. **Read-only inventory script** (`scripts-ops/`): by namespace and relationship type, count the edges,
   the distinct canonical targets, and how many collapse under MERGE dedupe when re-pointed.
3. **Re-point, not delete**: move each dangling endpoint to `merged_into` with the same
   `MERGE ... ON CREATE / ON MATCH SET += props` semantics the adapter uses, so properties survive and
   duplicates collapse. Deletion is correct only for edges whose two endpoints collapse onto the same
   node. Snapshot the affected edges into an evidence bundle first (the inverse-map shape the ledger
   already uses) to keep the operation reversible.
4. **Gate the mutation** with the house protocol (AGENTS.md §7.2): fresh backup, dry-run, human approval
   file, apply, then scoped + global audit. Do not touch the ledger entries or the `merged_into` values.
5. **Add the missing audit rule** (for example `ENDPOINT_MERGED_INVALID`) to `RULE_CATALOG` with tests,
   so the class is visible and cannot regress silently. If the new rule changes the global audit totals,
   regenerate the affected baselines deliberately.
6. **Verify** as stated in the acceptance criteria below.

## Acceptance criteria

- `MATCH (c:Chunk)-[:MENTIONS]->(e:Entity) WHERE e.merged_into IS NOT NULL RETURN count(*)` = **0**.
- `MATCH (a:Entity)-[r:RELATED]->(b:Entity) WHERE a.merged_into IS NOT NULL OR b.merged_into IS NOT NULL RETURN count(r)` = **0**.
- The new adapter regression test fails before the fix and passes after it.
- The new audit rule exists, is tested, and reports 0 on the production graph.
- Merge ledger untouched (958 entries) and `merged_into` counts unchanged (Book 4: 409).

## Non-goals

- Re-running entity resolution for the affected namespaces.
- Deleting merged nodes, ledger entries, or community summaries.
- Changing any node property beyond the re-pointed edge endpoints.

## Related follow-ups (registered, deliberately out of this debt)

- **Self-loops**: 5 were deleted on 2026-10-01 with explicit approval, but the extractor recreates them
  and the audit still has no self-loop rule.
- **Case-sensitive duplicates**: the audit groups by exact `name`, so `alucinación` and `Alucinación`
  are never reported as duplicates; the case-insensitive/semantic pass belongs to the semantic
  resolution phase.
