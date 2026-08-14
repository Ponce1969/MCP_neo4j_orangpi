# Design: GraphRAG RAGAS Resilience

## Technical Approach

Extend the existing hexagonal CQRS pipeline without changing its model/port/adapter split. `IndexBookUseCase` will capture per-chunk entity IDs while flattening a batch, then persist idempotent `(:Chunk)-[:MENTIONS]->(:Entity)` edges after entity and relationship writes. `LLMAdapter` resolves deterministic IDs, `Neo4jCommandAdapter` performs one batched endpoint check and applies the configured orphan policy, and `Neo4jQueryAdapter` adds provenance-rich tiered search. Every new query is parameterized, idempotent, and validated with `EXPLAIN` before production use.

## Architecture Decisions

### Decision: OQ-1 — aliases as entity properties
**Choice:** `Entity.aliases: list[str]` plus `canonical_name: str | None`; no `Alias` nodes or `ALIAS_OF` edges.
**Alternatives considered:** Alias nodes would require extra edges/joins and larger backfills.
**Rationale:** Array properties are directly supported by the required entity fulltext index, keep the lookup path simple, and make rollback `REMOVE aliases, canonical_name`. No destructive node merge is performed.

### Decision: OQ-2 — no silent cross-batch replay
**Choice:** Check only committed nodes in each batch and apply `fail_loud` or `log_orphan`; later-batch references may be dead-lettered under SCEN-REL-05.
**Alternatives considered:** Topological sorting is unstable with parallel producers; an end-of-run replay would expand scope and alter the documented limitation.
**Rationale:** This preserves spec semantics and the ≤5% dead-letter target. Any second-pass replay requires a separate config flag and is explicitly out of scope.

### Decision: OQ-3 — conservative configurable match policy
**Choice:** Default `canonical_match_mode="slug"` with deterministic slug equality; optional `fuzzy` mode uses a validated threshold (default `0.92`).
**Alternatives considered:** Fuzzy matching by default risks false-positive merges.
**Rationale:** False negatives are acceptable, false positives are not. Threshold and mode come from `Settings`, not adapter constants.

### Decision: OQ-4 — settings-owned stoplist
**Choice:** `canonical_stoplist: list[str] = []`, parsed from `CANONICAL_STOPLIST`; stoplisted aliases are ignored for canonicalization.
**Alternatives considered:** Hardcoding domain terms would leak assumptions into adapters and make corpus behavior non-configurable.
**Rationale:** It keeps the default safe and empty while allowing disambiguation for future domains.

## Data Flow

```text
Chunk ──extract──> _LLMEntityDTO ──resolve id/aliases──> Entity
  │                                                        │
  └────────────── chunk_index/book_id/entity_ids ─────────┘
                                                           │
                                  IndexBookUseCase._flush_batch
                                   ├─ upsert_entities
                                   ├─ endpoint batch check
                                   ├─ upsert valid relationships / dead-letter orphans
                                   └─ upsert_mentions per chunk
```

Provenance is sourced from `(:Chunk)-[:MENTIONS]->(e:Entity)`. `find_entity` and `find_entities_batch` return `source` as a structured string containing `chunk_index` and `book_id` when present (for example, `book_id=book-1,chunk_index=5`).

## File Changes

| File | Action | Description |
|---|---|---|
| `domain/models.py` | Modify | Add aliases/canonical fields; make `EntityWithContext.source` provenance-bearing. |
| `infrastructure/llm_adapter.py` | Modify | Extend prompt/DTO and resolve IDs. |
| `application/index_book_use_case.py` | Modify | Capture chunk IDs and call mention port. |
| `ports/graph_db_port.py` | Modify | Add `upsert_mentions` and orphan policy contract. |
| `infrastructure/neo4j_command_adapter.py` | Modify | Add endpoint detection, dead-letter writer, and mention writes. |
| `infrastructure/neo4j_query_adapter.py` | Modify | Tiered lookup, batch source extraction, fulltext index. |
| `ports/dead_letter_port.py` | Create | Dead-letter abstraction. |
| `infrastructure/dead_letter.py` | Create | Structured JSONL orphan writer. |
| `config.py` | Modify | Add validated settings. |
| `scripts/backfill_resilience.py` | Create | Alias/index migration and re-index guidance. |
| `scripts/run_ragas_evaluation.py` | Modify | Compare baseline metrics separately. |

## Interfaces / Contracts

```python
# Entity
aliases: list[str] = Field(default_factory=list)
canonical_name: str | None = None

# GraphDatabasePort
async def upsert_mentions(
    chunk_index: int, book_id: str | None, entity_ids: list[str]
) -> None: ...
```

Orphan entries contain `reason`, `type`, source/target IDs, description, source page, chunk index, timestamp, and `missing_endpoint` (`source`, `target`, or `both`).

## Cypher

### :MENTIONS and batch provenance

```cypher
UNWIND $entity_ids AS eid
MATCH (c:Chunk {chunk_index: $chunk_index})
WHERE ($book_id IS NULL AND c.book_id IS NULL) OR c.book_id = $book_id
MATCH (e:Entity {id: eid})
MERGE (c)-[m:MENTIONS]->(e)
SET m.source_page = coalesce(m.source_page, e.source_page)
```

The `WHERE` guard is required because Cypher equality with `NULL` evaluates to `UNKNOWN`; a direct `{book_id: $book_id}` would fail to match nodes whose `book_id` is null (TOC-less PDFs, REQ-PROV-02).

For `find_entity` and `find_entities_batch`, every tier uses the same source-extraction pattern:

```cypher
OPTIONAL MATCH (c:Chunk)-[:MENTIONS]->(n)
RETURN n, <tier_score> AS score, c.chunk_index AS chunk_index, c.book_id AS book_id
```

`EntityWithContext.source` is populated from the first matching `c`; for batch reads, source is populated for every returned entity whenever a `MENTIONS` edge exists. If an entity has multiple sources, retain one deterministic source row or the adapter may serialize the first source after deduplicating by `chunk_index`; no entity is returned without attempting source extraction.

### Endpoint detection

```cypher
WITH $source_ids AS src_ids, $target_ids AS dst_ids
UNWIND (src_ids + dst_ids) AS id
WITH DISTINCT id
OPTIONAL MATCH (n:Entity {id: id})
WITH collect(id) AS requested, collect(n) AS found_nodes
RETURN [i IN requested WHERE NOT i IN [n IN found_nodes | n.id]] AS missing_ids
```

Valid relationships are written with the existing `MATCH`/`MERGE :RELATED` pattern. Under `fail_loud`, missing IDs raise before writes. Under `log_orphan`, one structured JSONL record is written per orphan relationship, and the relationship is skipped. The zero-silent-drop invariant is `input_relationships = persisted_relationships + dead_lettered_orphans`; later-batch references remain subject to the spec's known limitation.

### Tiered `find_entity`

Queries are executed in order and stop after a non-empty tier. Each tier includes the `OPTIONAL MATCH (:Chunk)-[:MENTIONS]->(n)` source extraction and the same type guard.

#### Tier 1 — exact match

```cypher
MATCH (n:Entity {name: $name})
WHERE $entity_type IS NULL OR n.type = $entity_type
OPTIONAL MATCH (c:Chunk)-[:MENTIONS]->(n)
RETURN n, 1.0 AS score, c.chunk_index AS chunk_index, c.book_id AS book_id
LIMIT $limit
```

#### Tier 2 — case-insensitive

```cypher
MATCH (n:Entity)
WHERE toLower(n.name) = toLower($name)
  AND ($entity_type IS NULL OR n.type = $entity_type)
OPTIONAL MATCH (c:Chunk)-[:MENTIONS]->(n)
RETURN n, 0.8 AS score, c.chunk_index AS chunk_index, c.book_id AS book_id
LIMIT $limit
```

#### Tier 3 — partial (CONTAINS)

```cypher
MATCH (n:Entity)
WHERE n.name CONTAINS $name
  AND ($entity_type IS NULL OR n.type = $entity_type)
OPTIONAL MATCH (c:Chunk)-[:MENTIONS]->(n)
RETURN n, 0.6 AS score, c.chunk_index AS chunk_index, c.book_id AS book_id
ORDER BY size(n.name) ASC
LIMIT $limit
```

#### Tier 4 — fulltext over names, canonical names and aliases

```cypher
CALL db.index.fulltext.queryNodes("entity_name_aliases_index", $name)
YIELD node AS n, score AS ft_score
WHERE $entity_type IS NULL OR n.type = $entity_type
OPTIONAL MATCH (c:Chunk)-[:MENTIONS]->(n)
RETURN n, ft_score * 0.4 AS score, c.chunk_index AS chunk_index, c.book_id AS book_id
ORDER BY score DESC
LIMIT $limit
```

Deduplicate results by `n.id`, retaining the highest score (earliest tier wins ties). If fulltext is unavailable, log a warning and return Tiers 1–3.

## Canonicalization Algorithm

`_resolve_entity_id(name, canonical_name, aliases, entity_type)` follows this exact compatibility rule:

```python
if canonical_name is not None:
    return f"{_slugify(canonical_name)}-{entity_type}"
return _slugify(name)
```

This is the critical REQ-CANON-01 rule: an entity without `canonical_name` has exactly `id = _slugify(name)`, with no type suffix. Type-aware distinctness is used only when canonicalization is active (`canonical_name` present); then append the normalized type. Legacy/existing entities keep their `_slugify(name)` IDs and are never mass-migrated. Aliases are normalized, deduplicated, stoplist-filtered, and persisted for lookup. Merge decisions log `canonical_id` and the selected `alias`; aliases are not secrets or PII and logging them satisfies REQ-NFR-02. No node is destructively merged.

## `_flush_batch` Flow

```text
Producer -> Queue -> _consume -> _flush_batch
                              ├─ collect chunk keys and entity IDs
                              ├─ upsert book/editorial structure
                              ├─ upsert entities
                              ├─ endpoint detection (one query)
                              │    ├─ fail_loud: abort before relationship writes
                              │    └─ log_orphan: write JSONL, persist valid subset
                              └─ upsert_mentions(chunk_index, book_id, entity_ids)
```

A failed or dead-lettered chunk never creates mentions. There is no implicit second pass after the sentinel; cross-batch resolution remains the documented SCEN-REL-05 limitation.

## Backfill Script Shape

`uv run python scripts/backfill_resilience.py all` performs idempotent `SET n.aliases = coalesce(n.aliases, [])`, `SET n.canonical_name = coalesce(n.canonical_name, n.name)`, creates the fulltext index, and reports that `:MENTIONS` cannot be reconstructed from a legacy graph without extracted chunk content. Operators must run the supported full re-index command for provenance. No legacy IDs are rewritten.

## Data Model

```text
(:Book) -> (:Chapter) -> (:Chunk {chunk_index, book_id})
                         └─[:MENTIONS {source_page?}]-> (:Entity {id, name, type,
                              description, source_page, aliases, canonical_name})
(:Entity)-[:RELATED {type, description, source_page}]->(:Entity)
```

## Testing Strategy

| Layer | What to Test | Approach |
|---|---|---|
| Unit | Legacy ID exactness, canonical/type path, alias filtering | `tests/test_llm_adapter.py`; assert no type suffix without canonicalization. |
| Unit | Endpoint batch query, fail-loud/log-orphan, JSONL fields, invariant | Existing fake driver; assert one endpoint query per batch. |
| Unit | Mention idempotency and per-chunk source | `tests/test_neo4j_command_adapter.py` and use-case tests. |
| Unit | `find_entity` tiers, early stop, type filter, dedup, missing fulltext | Fake driver counters and fulltext exception. |
| Unit | `find_entities_batch` source `(chunk_index, book_id)` | Fake records with `MENTIONS`; assert source is non-null. |
| Integration | Actual Neo4j `EXPLAIN` and full index audit | Container-gated tests; mention coverage and zero-silent-drop audit. |
| Evaluation | Separate retrieval/generation metrics | RAGAS baseline comparison: faithfulness, context recall, context precision. |

## Security / Config Considerations

Use `pydantic_settings.BaseSettings`; validate `canonical_match_mode`, `canonical_fuzzy_threshold` (`0.5..1.0`), `relationship_orphan_policy`, and `canonical_stoplist`. Never interpolate LLM/user names into Cypher. Parameterize all values, use `SecretStr` for existing credentials, and log only IDs, alias/canonical decision fields, counts, and policy—not credentials or chunk text. Aliases are intentionally logged for observability and are not classified as secrets/PII. Fulltext failure is a warning and graceful degradation.

## Chained PR Slice Boundaries

| PR | Scope | Finish/verification | Depends on |
|---|---|---|---|
| 1 | Domain fields, DTO, canonical ID and prompt | Unit tests; legacy ID contract | — |
| 2 | `upsert_mentions` and `_flush_batch` wiring | Mention and provenance tests | PR1 |
| 3 | Batch endpoint detection and dead-letter policy | Invariant and policy tests | PR2 |
| 4 | Tiered query, batch source, fulltext index | Query/fake-driver and EXPLAIN tests | PR2 |
| 5 | Backfill, settings, RAGAS comparison | Migration docs and baseline/after metrics | PR3, PR4 |

Keep each slice under 400 changed lines; tests stay with their work unit. The final child PRs target their immediate parent in a feature-branch chain; the tracker remains draft until all children land.

## Definition of Done

- [ ] All REQ-PROV, REQ-CANON, REQ-REL, REQ-FIND, and REQ-NFR requirements have tests.
- [ ] `id == _slugify(name)` is asserted for entities without `canonical_name` and aliases.
- [ ] Canonical merge logs include canonical id and alias; legacy IDs are preserved.
- [ ] `find_entity` and `find_entities_batch` populate source from `MENTIONS` source fields.
- [ ] No second-pass replay is silently added; SCEN-REL-05 behavior is explicit.
- [ ] `EXPLAIN` validation, architecture validation, Ruff, strict mypy, and Neo4j audits pass.
