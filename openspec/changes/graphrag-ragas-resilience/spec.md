# Spec: GraphRAG RAGAS Resilience

**Change**: `graphrag-ragas-resilience`
**Store**: OpenSpec (`openspec/changes/graphrag-ragas-resilience/spec.md`)
**Proposal source**: `openspec/changes/graphrag-ragas-resilience/proposal.md`
**Status**: No prior `openspec/specs/{domain}/spec.md` exists — this is a **FULL spec** (not a delta) for all four capabilities.
**Conventions**: RFC 2119 keywords; GIVEN/WHEN/THEN scenarios; English technical register.

---

## 1. Overview and Goals

### Overview
The GraphRAG pipeline currently lacks auditable provenance, silently drops relationships, performs exact-match-only entity lookup, and spawns duplicate nodes for aliases. These gaps make the GR.3 RAGAS baseline (Faithfulness, Context Recall, Context Precision) unreliable and un-debuggable.

### Goals
1. Make the graph **traceable**: every extracted entity is linked to its source chunk via `(:Chunk)-[:MENTONS]->(:Entity)`.
2. Make the graph **canonical**: aliases fold into a single canonical entity node; no duplicate `:Entity` for "MCP" vs "Model Context Protocol".
3. Make import **fail-loud / auditable**: relationships with missing endpoints are detected and logged or rejected — never silently dropped.
4. Make lookups **forgiving**: `find_entity` cascades exact → case-insensitive → `CONTAINS` → fulltext, so paraphrased queries resolve.
5. Improve GR.3 RAGAS Faithfulness, Context Recall, and Context Precision by ≥10% over baseline.

### Non-Goals (Out of Scope — reminders)
- Changing the LLM extractor model (`deepseek-chat` remains).
- Redesigning Leiden / community detection / hierarchical summaries (C0-C3).
- Re-architecting the CQRS port split; writes stay on `GraphDatabasePort`, reads on `GraphQueryPort`.
- Vector/embedding retrieval or semantic similarity (reserved, not in this change).

## 2. Functional Requirements

Capabilities addressed (proposal mapping): **new** = `chunk-entity-provenance`, `entity-canonicalization`; **modified** = `relationship-import`, `entity-lookup`.

### 2.1 Capability: chunk-entity-provenance (NEW)

**REQ-PROV-01 — Mentions edge creation.** The system SHALL create a `(:Chunk)-[:MENTIONS]->(:Entity)` edge for every entity extracted from that chunk. The `:MENTIONS` edge MUST be idempotent (`MERGE`) and created in the same persistence batch as the entities it references.

**REQ-PROV-02 — Chunk identity binding.** The `:MENTIONS` edge SHALL be anchored on the `Chunk` node keyed by `(chunk_index, book_id)` and the `Entity` node keyed by `id`. When `book_id` is `None` (TOC-less PDF), the chunk remains identifiable by `chunk_index` alone within that book context.

**REQ-PROV-03 — Coverage guarantee.** The system SHALL achieve `:MENTIONS` coverage ≥ **95%** of extracted entities after a full index. An entity with no `:MENTIONS` edge MUST be an exception, attributable to extraction failure (already in the dead-letter log) — not a silent import skip.

**REQ-PROV-04 — Provenance in read results.** `find_entity` and `find_entities_batch` results (`EntityWithContext`) SHALL populate `source` with the originating `chunk_index` (and `book_id` when available) so retrieval evidence is citable as `[Data: Chunk (idx)]`.

#### Scenarios

| ID | GIVEN | WHEN | THEN |
|----|-------|------|------|
| SCEN-PROV-01 | A chunk extracts 3 entities, all with valid slug ids | The batch is flushed | 3 `:MENTIONS` edges exist from that `Chunk`, one per extracted `Entity` id; no duplicate edges on re-flush (idempotent) |
| SCEN-PROV-02 | The same entity name appears in chunk #5 and chunk #12 | Both chunks flush | `:ENTITY` node has ONE `id`; TWO `:MENTIONS` edges (one from each chunk); no duplicate entity |
| SCEN-PROV-03 | A PDF has no TOC (`book_id = None`) | Chunk #4 flushes | `:MENTIONS` from `Chunk{chunk_index:4, book_id:null}` to its entities is created |
| SCEN-PROV-04 | Extraction raises and the chunk is dead-lettered | The batch continues | No `:MENTIONS` edges for that chunk; dead-letter entry exists; coverage metric excludes dead-lettered chunks from the denominator |

**Acceptance criteria (REQ-PROV)**
- AC-PROV-01: After re-indexing the current book, a Cypher audit `MATCH (c:Chunk)-[:MENTIONS]->(e:Entity)` count ÷ total extracted entities ≥ 0.95.
- AC-PROV-02: Re-flushing the same batch produces zero new `:MENTIONS` rows (idempotency assertion in tests).
- AC-PROV-03: `EntityWithContext.source` is non-null for ≥95% of `find_entity` hits.

### 2.2 Capability: entity-canonicalization (NEW)

**REQ-CANON-01 — Canonical id computation.** The `LLMAdapter` SHALL compute each entity's `id` deterministically. For an entity with `canonical_name`, the canonical id MUST equal `_slugify(canonical_name)`. For an entity without `canonical_name` or `aliases`, the id MUST equal `_slugify(name)` — preserving backward compatibility with existing graph ids (no mass id migration).

**REQ-CANON-02 — Alias capture.** The `_LLMEntityDTO` SHALL carry an `aliases: list[str]` field (default `[]`) populated by the LLM extraction prompt. The persistent `Entity` SHALL store `aliases` and `canonical_name`.

**REQ-CANON-03 — Alias persistence model.** The system SHALL make aliases queryable for retrieval. Aliases MUST be stored such that `find_entity("MCP")` can resolve to the entity whose `canonical_name` is "Model Context Protocol". The system SHOULD represent aliases via an `:ALIAS_OF` edge / alias nodes rather than destructive merge, so a false-positive alias can be rolled back without loss.

**REQ-CANON-04 — Conservative merge policy.** Canonicalization MUST be conservative: a **false-negative** (missed alias) is acceptable; a **false-positive** (two distinct entities merged) MUST NOT occur. The merge threshold MUST be tunable via `Settings` (similarity cut-off / match mode), defaulting to a high-confidence level.

**REQ-CANON-05 — Type awareness.** Two entities with the same `canonical_name` but different `EntityType` SHALL remain distinct nodes (type-aware canonical id), to avoid merging e.g. a "MCP" `tool` with an "MCP" `concept`.

#### Scenarios

| ID | GIVEN | WHEN | THEN |
|----|-------|------|------|
| SCEN-CANON-01 | LLM returns entity name "Model Context Protocol" with alias "MCP" | Extraction completes | One `Entity` node created with `id = slugify("Model Context Protocol")`; alias "MCP" persisted and queryable |
| SCEN-CANON-02 | An entity has no aliases and no canonical_name | Id computed | `id == _slugify(name)` (unchanged behavior; existing ids stay stable) |
| SCEN-CANON-03 | Two entities share canonical name "Agent" but differing types `pattern` and `agent` | Both flush | Two distinct `Entity` nodes (type-aware ids); both queryable |
| SCEN-CANON-04 | A legacy graph node has `id = _slugify(name)`, no alias fields | Backfill runs on it | The legacy node is preserved; alias fields default to `[]`; no destructive merge |
| SCEN-CANON-05 | "MCP" also names an unrelated concept in another book domain | `find_entity("MCP")` invoked | Results are disambiguated by `EntityType` filter and/or a configurable domain stoplist; both are returned, ranked, not merged |

**Acceptance criteria (REQ-CANON)**
- AC-CANON-01: Unit test asserts `slugify(canonical_name)` id path and stable legacy `slugify(name)` path.
- AC-CANON-02: `find_entity("Model Context Protocol")` and `find_entity("MCP")` return the same canonical `Entity.id`.
- AC-CANON-03: A configurable merge threshold value is read from `Settings`; no hard-coded magic number in the adapter.
- AC-CANON-04: A false-positive merge scenario is represented by a test asserting the two nodes remain distinct.

### 2.3 Capability: relationship-import (MODIFIED — full spec; no prior main spec)

**REQ-REL-01 — Endpoint detection.** Before upserting a relationship, the system SHALL detect whether both `source_entity_id` and `target_entity_id` resolve to existing `:Entity` nodes. The detection MUST run as a batched set-membership check (one `MATCH` over the union of referenced ids per batch), not one query per relationship.

**REQ-REL-02 — Orphan handling policy.** A relationship with a missing endpoint MUST NOT be silently dropped. The system SHALL apply a configurable policy: **`fail_loud`** raises an error and aborts the batch (fail-fast at indexing time), or **`log_orphan`** writes the orphan relationship to the dead-letter log (JSONL) and skips it, continuing the batch. The default indexing-time policy MUST be `log_orphan` with structured fields. Queries remain forgiving (read path unaffected).

**REQ-REL-03 — Dead-letter schema.** Orphan relationship dead-letter entries SHALL include `type`, `source_entity_id`, `target_entity_id`, `description`, `source_page`, `missing_endpoint` (`"source" | "target" | "both"`), `chunk_index`, `timestamp`, and `reason="orphan_endpoint"`.

**REQ-REL-04 — Zero silent drops invariant.** After a full index, the count of input relationships MUST equal `persisted_relationships + dead_lettered_orphans`. No relationship MAY vanish without either a `:RELATED` edge or a dead-letter record. This invariant MUST be asserted in the indexing verification step.

#### Scenarios

| ID | GIVEN | WHEN | THEN |
|----|-------|------|------|
| SCEN-REL-01 | A batch has 10 rels; 2 reference a non-existent target id | Flush with `log_orphan` | 8 `:RELATED` edges created; 2 dead-letter entries with `missing_endpoint="target"`; batch completes |
| SCEN-REL-02 | Same batch with policy `fail_loud` | Flush | An exception is raised; no `:RELATED` edges from that batch are committed; IDs of missing endpoints are in the error message |
| SCEN-REL-03 | A relationship's source and target both exist as `:Entity` | Flush | A `:RELATED` edge is created/updated (idempotent); no dead-letter entry |
| SCEN-REL-04 | Same endpoint pair+`type` already exists | Re-flush | Edge is merged (no duplicate); `description`/`source_page` updated |
| SCEN-REL-05 | An endpoint exists only in a *later* batch (cross-batch ref) | Batch N flushes | The relationship is dead-lettered at batch N (endpoints checked against committed nodes only); documented as a known limitation |
| SCEN-REL-06 | Verification runs post-index | Relations counted | `input_rel_count == persisted_rel_count + orphan_deadletter_count` holds (zero silent drops) |

**Acceptance criteria (REQ-REL)**
- AC-REL-01: Unit test seeds missing endpoints and asserts exactly the dead-letter entries described above; asserts zero `:RELATED` edges for those rels.
- AC-REL-02: `fail_loud` mode raises with endpoint ids in the message; `log_orphan` mode completes the batch.
- AC-REL-03: A re-index of the current book leaves dead-letter growth ≤ 5% of total relationships (proposal success criterion).

### 2.4 Capability: entity-lookup (MODIFIED — full spec; no prior main spec)

**REQ-FIND-01 — Tiered search cascade.** `find_entity(name, entity_type)` SHALL return results by cascading through tiers in order, **stopping early** when a tier yields matches so the common (exact-match) path stays fast:

1. **Tier 1 — exact**: `MATCH (n:Entity {name: $name})` (existing exact behavior).
2. **Tier 2 — case-insensitive**: `WHERE toLower(n.name) = toLower($name)`.
3. **Tier 3 — partial**: `WHERE n.name CONTAINS $name` (substring).
4. **Tier 4 — fulltext**: `CALL db.index.fulltext.queryNodes("entity_name_aliases_index", $name)` over names **and** aliases.

**REQ-FIND-02 — Type filtering across tiers.** The optional `entity_type` filter SHALL apply to every tier. When `entity_type` is provided, results MUST be restricted to that `EntityType` in all four tiers.

**REQ-FIND-03 — Ranking and deduplication.** Results from lower tiers SHALL be scored and merged with earlier-tier results; duplicate entity ids MUST be deduplicated, preserving the highest-confidence (earliest-tier) score. The returned `EntityWithContext.confidence` SHALL reflect the tier that produced the hit (e.g. 1.0 exact, decreasing per tier); `source` SHALL carry the originating `chunk_index`.

**REQ-FIND-04 — Index existence.** The fulltext fallback (Tier 4) MUST only execute after `ensure_indexes` has created the `entity_name_aliases_index` fulltext index. If the fulltext index is unavailable (Neo4j without fulltext/APOC), the adapter MUST degrade gracefully to Tiers 1-3 and log a warning, never crash.

**REQ-FIND-05 — Parity requirement.** `find_entity("mcp")` MUST return the entity whose canonical name is "Model Context Protocol" (proposal success criterion). Existing exact-match callers that relied on Tier-1 behavior MUST observe no regression (Tier 1 returns identical results when an exact match exists).

**REQ-FIND-06 — Result limit unchanged.** The existing `LIMIT $limit` (default 100) SHALL remain the per-tier / merged cap. The method signature `find_entity(name, entity_type) -> list[EntityWithContext]` SHALL NOT change — behavior is modified, the contract is preserved.

#### Scenarios

| ID | GIVEN | WHEN | THEN |
|----|-------|------|------|
| SCEN-FIND-01 | Entity "Model Context Protocol" exists, alias "MCP" stored | `find_entity("Model Context Protocol")` | Returns that entity via Tier 1; `confidence=1.0` |
| SCEN-FIND-02 | Same graph | `find_entity("mcp")` | Tiers 1-3 miss; Tier 4 fulltext returns the entity via alias "MCP"; single deduped result |
| SCEN-FIND-03 | Entity "MCP" exists as a distinct `type=tool` and "Model Context Protocol" as `type=concept` | `find_entity("mcp", entity_type="tool")` | Returns only the `tool` entity across all tiers |
| SCEN-FIND-04 | Exact match "Agent" exists | `find_entity("Agent")` | Tier 1 returns it; Tiers 2-4 are NOT executed (fast path) |
| SCEN-FIND-05 | No fulltext index (fresh DB, `ensure_indexes` not run) | `find_entity("someparaphrase")` | Tiers 1-3 run; Tier 4 raises/caught internally; adapter logs warning and returns Tiers 1-3 results — no exception to caller |
| SCEN-FIND-06 | Multiple tiers each yield the same canonical id | merge step | Returns one entry with the highest-tier confidence and a `source` chunk_index |

**Acceptance criteria (REQ-FIND)**
- AC-FIND-01: Test asserts `find_entity("mcp")` returns "Model Context Protocol" (canonical) via alias.
- AC-FIND-02: Test asserts Tier 1 short-circuit when exact match exists (Tier 4 query counters show 0 executions).
- AC-FIND-03: Test asserts graceful degradation when fulltext index absent (no raised exception).
- AC-FIND-04: Backward-compat test: pre-existing exact-match expectations pass unchanged.

## 3. Non-Functional Requirements

**REQ-NFR-01 — Performance.** The tiered `find_entity` exact-match path (Tier 1) MUST add ≤ 5 ms p95 latency over the current single-query implementation. Fallback tiers MUST enforce the existing ≥3 s query timeout (reused `_run_with_timeout`). Endpoint detection (REQ-REL-01) MUST use a single batched set-membership query per flush batch, not N queries.

**REQ-NFR-02 — Observability.** All new code paths SHALL emit structured logs: orphan relationships (warn), fulltext degradation (warn), canonical merge decisions (info, with canonical id + alias), `:MENTIONS` coverage shortfalls (warn after index). No secrets, PII, or full chunk text in logs.

**REQ-NFR-03 — Testability.** Every requirement above SHALL be covered by automated tests: unit tests for `LLMAdapter` canonical id logic, `Neo4jCommandAdapter` orphan handling + `:MENTIONS` creation (using a test Neo4j or fakes per existing `tests/test_neo4j_command_adapter.py`), `Neo4jQueryAdapter.find_entity` tier cascade (where fulltext is mocked/gated), and the dead-letter invariant. New Cypher MUST be validated with `EXPLAIN` (graphrag-architect Hard Rule 2).

**REQ-NFR-04 — Idempotency & migration safety.** All new write Cypher SHALL use `MERGE` (`:MENTIONS`, `:ALIAS_OF`, canonical `:Entity`). The change MUST ship an offline backfill script for legacy graphs (adds `aliases=[]`/`canonical_name` defaults, creates `:MENTIONS` from chunk→entities, retrofits the fulltext index) and document the full rebuild command. Re-indexing current book MUST complete with dead-letter growth ≤ 5%.

**REQ-NFR-05 — Config / fail-fast.** New configuration (orphan policy, canonical merge threshold, domain stoplist, backfill flags) SHALL come from `pydantic_settings.BaseSettings` with `SecretStr` where sensitive. Missing required settings MUST fail at app start (project Fail-Fast rule). No hard-coded URLs/keys.

## 4. Interface / API Contract Changes

### 4.1 Domain models (`src/book_graph_rag/domain/models.py`)

| Model | Change | Detail |
|-------|--------|--------|
| `Entity` | ADD fields | `aliases: list[str] = Field(default_factory=list)`; `canonical_name: str \| None = None` |
| `_LLMEntityDTO` (infra) | ADD fields | `aliases: list[str] = Field(default_factory=list)`; `canonical_name: str \| None = None` — prompt-backed |
| `EntityWithContext` | SEMANTIC | `source` repurposed from "reserved for Fase 08" → carries `chunk_index` (+`book_id`); `confidence` carries the tier score |
| `Relationship` | UNCHANGED | fields already sufficient; orphan handling wraps it |

### 4.2 Ports

**`src/book_graph_rag/ports/graph_db_port.py` — `GraphDatabasePort` (writes)**

| Method | Change | Contract |
|--------|--------|----------|
| `upsert_entities(list[Entity])` | UNCHANGED signature | Stays idempotent `MERGE` by `id` (backward compat) |
| `upsert_relationships(list[Relationship])` | MODIFIED behavior | Endpoint detection + orphan policy BEFORE `MERGE`; no silent drops |
| NEW `upsert_mentions(chunk_index, book_id, entity_ids)` (exact signature for sdd-design) | ADD | Idempotently `MERGE (:Chunk{chunk_index,book_id})-[:MENTIONS]->(:Entity{id})` for each id; `book_id` may be `None` |

> The orchestrator (`IndexBookUseCase._flush_batch`) SHALL call `upsert_mentions` per chunk with that chunk's extracted entity ids, restoring the chunk→entity link lost by flattening.

**`src/book_graph_rag/ports/graph_query_port.py` — `GraphQueryPort` (reads)**

| Method | Change | Contract |
|--------|--------|----------|
| `find_entity(name, entity_type)` | MODIFIED behavior, UNCHANGED signature | Tiered cascade (REQ-FIND-01); returns deduped, scored `EntityWithContext` with `source`/`confidence` |
| `find_entities_batch(ids)` | MODIFIED behavior | Populate `source` chunk_index when cheap |
| `ensure_indexes()` | MODIFIED | ADD `entity_name_aliases_index` fulltext over `(n:Entity)` name + alias properties; keep existing indexes |

### 4.3 Adapters

| Adapter | Change |
|---------|--------|
| `Neo4jCommandAdapter` | Implement endpoint detection; implement `upsert_mentions`; orphan dead-letter writing |
| `Neo4jQueryAdapter` | Implement tiered `find_entity` with early-stop + dedup + score; graceful fulltext degradation; extend `ensure_indexes` |
| `LLMAdapter` | Extend extraction prompt to request `aliases`/`canonical_name`; compute canonical id (`_slugify(canonical_name)` if present else `_slugify(name)`); keep `_slugify` stable |
| `IndexBookUseCase` | Call `upsert_mentions` per chunk in `_flush_batch`; pass new config; honor orphan policy |

## 5. Data Model Changes (Neo4j schema)

| Element | Type | Detail |
|---------|------|--------|
| `:MENTIONS` | NEW relationship | `(:Chunk)-[:MENTIONS]->(:Entity)`. Anchored on `Chunk{chunk_index, book_id}` → `Entity{id}`. Idempotent `MERGE`. Optional property: `source_page`. |
| `:ALIAS_OF` | NEW relationship (recommended) | `(:Alias {name})-[:ALIAS_OF]->(:Entity {id})` OR alias stored as `Entity.aliases[]` array property. Chosen form MUST support fulltext queryability (REQ-CANON-03). |
| `Entity.aliases` | NEW property | `list<string>` (or normalized array). |
| `Entity.canonical_name` | NEW property | `string?` (non-null when canonicalization applied). |
| `entity_name_aliases_index` | NEW index | `CREATE FULLTEXT INDEX entity_name_aliases_index IF NOT EXISTS FOR (n:Entity) ON EACH [n.name, n.canonical_name, n.aliases]` (aliased fields queryable). Gated by APOC/schema helpers; mocked in unit tests. |
| Existing indexes | UNCHANGED | `entity_name`, `entity_type`, `entity_id`, `rel_type`, `chunk_text_index` remain. |
| Backfill | NEW migration script | Legacy graphs: default missing alias/canonical fields, reconstruct `:MENTIONS` where chunk→entity linkage recoverable, create fulltext index. Document full rebuild. |
| Rollback | migration | Drop `entity_name_aliases_index`; delete `:MENTIONS` and `:ALIAS_OF`; revert `find_entity` to exact; revert canonical id to `_slugify(name)` only. |

## 6. Open Questions / Assumptions

- **OQ-1** Alias storage form: array property vs `:Alias` nodes + `:ALIAS_OF`. Spec RECOMMENDS `:ALIAS_OF`; sdd-design MUST decide based on fulltext index support for array properties in the target Neo4j version (Aura vs community).
- **OQ-2** Cross-batch endpoint references (SCEN-REL-05): currently dead-lettered. Is a second pass acceptable, or should the import order be topologically sorted? Assumption: accept dead-letter + ≤5% growth; revisit if exceeded.
- **OQ-3** Canonical merge threshold default value and similarity metric (string equality of slug vs fuzzy). Assumption: deterministic slug equality first; fuzzy only as configurable opt-in.
- **OQ-4** Domain stoplist source (hard-coded vs `Settings`). Assumption: `Settings`-driven, empty default.
- **ASM-1** APOC is available on the OrangePi Neo4j deployment (proposal dependency).
- **ASM-2** GR.3 RAGAS baseline scores exist and are reproducible (`scripts/run_ragas_evaluation.py`).

## 7. Definition of Done (Spec Phase)

- [x] All four in-scope capabilities have numbered functional requirements with ≥1 scenario each.
- [x] Every requirement references RFC 2119 keywords (MUST/SHALL/SHOULD/MAY).
- [x] Each capability has measurable acceptance criteria.
- [x] Interface (models/ports/adapters) and data model (schema/indexes) contracts are concrete enough for `sdd-design` to proceed without clarification.
- [x] Out-of-scope items, assumptions, and open questions are listed.
- [x] Spec grounded in real code signatures (`GraphDatabasePort`, `GraphQueryPort`, `Neo4jCommandAdapter.upsert_relationships`, `Neo4jQueryAdapter.find_entity`, `LLMAdapter._slugify`, `EntityWithContext.source`).
- [x] Spec persisted at `openspec/changes/graphrag-ragas-resilience/spec.md`.

**Next phase**: `sdd-design` — produce technical design (Cypher for `:MENTIONS`/endpoints/tiered find, canonicalization algorithm, index DDL, backfill script shape, sequence diagrams for `_flush_batch` changes).