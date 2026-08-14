# Proposal: GraphRAG RAGAS Resilience

## Intent

The current GraphRAG pipeline lacks auditable provenance: chunks do not link to the entities they contain, relationships silently vanish when endpoints are missing, entity lookup is exact-match only, and aliases spawn duplicate nodes. These gaps make GR.3 RAGAS metrics (Faithfulness, Context Recall, Context Precision) unreliable and impossible to debug. This change makes the graph traceable, canonical, and retrievable.

## Target Users & Situations

- Book consumers and agentic query tools asking precise or paraphrased entity questions.
- Evaluators running the GR.3 RAGAS baseline who need evidence for retrieval and generation scores.
- Future corpus maintainers who need automatic alias handling without manual curation.

## Scope

### In Scope

- Add `:MENTIONS` edges from `(:Chunk)` to every `(:Entity)` extracted within it.
- Validate relationship endpoints before upsert: fail loud or log orphan relationships.
- Relax `find_entity` with tiered matching: exact → case-insensitive → partial (`CONTAINS`) → fulltext.
- Automatic entity canonicalization with aliases detected by the system.

### Out of Scope

- Changing the LLM extractor model (deepseek-chat remains).
- Redesigning Leiden / community detection.

## Capabilities

### New Capabilities

- `chunk-entity-provenance`: `:MENTIONS` edges connecting source chunks to entities.
- `entity-canonicalization`: alias detection and canonical node merging during extraction/import.

### Modified Capabilities

- `relationship-import`: endpoint validation and orphan handling.
- `entity-lookup`: ranked tiered entity search.

## Approach

1. Extend `_LLMEntityDTO` with `aliases` and `canonical_name`; compute the canonical id deterministically in `LLMAdapter` while keeping `_slugify` stable for names without aliases.
2. In `Neo4jCommandAdapter.upsert_relationships`, validate endpoints before `MERGE` or use `OPTIONAL MATCH` to collect missing ids into the dead-letter log; create `:MENTIONS` edges in the same batch.
3. In `Neo4jQueryAdapter.find_entity`, cascade through exact, lowercased, `CONTAINS`, and fulltext index queries; score and deduplicate results.
4. Add fulltext index creation in `ensure_indexes` for entity names and aliases.
5. Re-run the GR.3 RAGAS evaluation and compare metrics against the baseline.

## Affected Areas

| Area | Impact | Description |
|---|---|---|
| `src/book_graph_rag/domain/models.py` | Modified | `Entity` gains alias/canonical fields. |
| `src/book_graph_rag/infrastructure/llm_adapter.py` | Modified | Alias extraction, canonical id resolution. |
| `src/book_graph_rag/infrastructure/neo4j_command_adapter.py` | Modified | Endpoint validation, `:MENTIONS` creation. |
| `src/book_graph_rag/infrastructure/neo4j_query_adapter.py` | Modified | Tiered `find_entity` with fulltext fallback. |
| `src/book_graph_rag/ports/graph_db_port.py` | Modified | Mention/canonical persistence contract. |
| `scripts/run_ragas_evaluation.py` | Modified | Before/after metric comparison. |

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Canonicalization over-aggressively merges distinct entities | Medium | Conservative similarity threshold; store aliases as `:ALIAS_OF` before destructive merge. |
| Re-indexing the current book is expensive | High | Provide incremental backfill script; document full rebuild command. |
| Endpoint validation slows bulk import | Medium | Batch endpoint check with one `MATCH` per batch. |
| Fulltext index unavailable in some Neo4j deployments | Low | Gate with APOC schema helpers; mock in unit tests. |

## Rollback Plan

- Drop new fulltext index and `:MENTIONS` edges via migration script.
- Revert canonical id computation to `_slugify(name)` only.
- Restore original exact-match `find_entity` Cypher.

## Dependencies

- Neo4j APOC plugin for index/schema helpers.
- Existing GR.3 RAGAS evaluation dataset and baseline scores.

## Success Criteria

- [ ] ≥95% of extracted entities have a `(:Chunk)-[:MENTIONS]->(:Entity)` edge.
- [ ] Zero silent relationship drops; missing endpoints are logged or rejected.
- [ ] `find_entity("mcp")` returns the "Model Context Protocol" entity.
- [ ] GR.3 RAGAS Faithfulness, Context Recall, and Context Precision improve ≥10% versus baseline.
- [ ] Re-index of the current book completes with dead-letter growth ≤5%.

## Business Rules & Tradeoffs

- Canonicalization is conservative: a false-negative alias is acceptable; a false-positive merge is not.
- Fallback search runs only after faster tiers fail to keep the common path fast.
- Endpoint validation is fail-loud at indexing time; queries remain forgiving.

## Edge Cases & Decision Gaps

- Same entity name with different types across chunks: preserve type-aware canonical nodes.
- Legacy graph without aliases: provide offline backfill script; do not require full re-extraction.
- Ambiguous aliases (e.g., "MCP" in another domain): mitigate with type filters and a domain stoplist.

## Definition of Done

This proposal is approved, stored in `openspec/changes/graphrag-ragas-resilience/proposal.md`, and the next phase (`sdd-spec`) can derive unambiguous delta specs from it without further clarification.
