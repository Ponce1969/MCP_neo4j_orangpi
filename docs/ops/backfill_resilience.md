# Backfill Resilience (`graphrag-ragas-resilience`)

This document describes the offline backfill for graphs created before the
`graphrag-ragas-resilience` change. It makes legacy `:Entity` nodes compatible
with the new type-aware ids, alias/canonical fields, and fulltext index.

## What the backfill does

```bash
uv run python scripts/backfill_resilience.py all
```

The script performs three idempotent operations:

1. **Legacy id migration**
   Nodes whose id is exactly the slugified name (without the type suffix) are
   migrated to `slugify(name)-type`. The new node copies all properties, and
   existing `:MENTIONS` and `:RELATED` relationships are re-pointed before the
   old node is deleted. Nodes already using a type-aware id are unchanged. If
   the target id already belongs to a different entity, the backfill skips that
   migration without copying properties, changing relationships, or deleting
   either node. Each collision is listed as `old_id -> new_id` in the command
   output, along with the final collision count. Resolve every collision
   manually before re-running the backfill; each re-run remains idempotent.

2. **Alias / canonical defaults**
   ```cypher
   MATCH (n:Entity)
   SET n.aliases = coalesce(n.aliases, []),
       n.canonical_name = coalesce(n.canonical_name, n.name)
   ```
   - Leaves existing `aliases` and `canonical_name` values untouched.
   - Fills missing `aliases` with an empty list.
   - Fills missing `canonical_name` with the current `n.name`.

3. **Fulltext index**
   ```cypher
   CREATE FULLTEXT INDEX entity_name_aliases_index IF NOT EXISTS
   FOR (n:Entity) ON EACH [n.name, n.canonical_name, n.aliases]
   ```
   - Required by the Tier 4 fallback in `find_entity`.
   - Idempotent via `IF NOT EXISTS`.

## Provenance and rollback

During id migration, existing `:MENTIONS` and `:RELATED` relationships are
re-pointed to the new type-aware entity id before the old node is deleted.
Entities without recoverable chunk provenance still require a full re-index.

`:MENTIONS` edges — `(:Chunk)-[:MENTIONS]->(:Entity)` — **cannot** be
reconstructed from a legacy graph. The backfill script only has access to the
persisted nodes and edges; the original chunk→entity extraction mapping is lost
after the legacy index ran. To obtain full provenance coverage, run a full
re-index:

```bash
uv run python scripts/run_indexer.py data/books/agentic-architectural-patterns.pdf
```

## Dry run

Preview the Cypher without writing:

```bash
uv run python scripts/backfill_resilience.py all --dry-run
```

## Rollback

If you need to revert the schema changes introduced by this backfill:

```cypher
DROP INDEX entity_name_aliases_index IF EXISTS;
MATCH (n:Entity)
REMOVE n.aliases, n.canonical_name;
```

> Note: this removes the new properties from all `:Entity` nodes. It does not
> delete `:MENTIONS` edges created by a subsequent re-index; delete those
> separately if required:
> ```cypher
> MATCH (:Chunk)-[m:MENTIONS]->(:Entity) DELETE m;
> ```

The id migration is forward-only and deletes old nodes after relationship
repointing. Take a Neo4j backup before production use. Roll back by restoring
that backup, or by running a reverse migration from recorded old/new id pairs.

## Full rebuild command

For a clean re-index that includes all PR1–PR5 features:

```bash
uv run python scripts/run_indexer.py data/books/agentic-architectural-patterns.pdf
```

After re-indexing, verify coverage:

```cypher
MATCH (c:Chunk)-[:MENTIONS]->(e:Entity)
RETURN count(DISTINCT e) AS mentioned_entities,
       count(DISTINCT c) AS mentioned_chunks;
```
