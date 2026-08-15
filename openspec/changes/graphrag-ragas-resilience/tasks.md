# Tasks: GraphRAG RAGAS Resilience

Decision needed before apply: No
Chained PRs recommended: Yes
Chain strategy: feature-branch-chain
400-line budget risk: High

| Field | Value |
|-------|-------|
| Estimated changed lines | ~1300 across 5 PRs |
| Delivery strategy | auto-chain |
| Suggested split | PR1 → PR2 → PR3 → PR4 → PR5 |
| PR1 rollback | Revert domain fields and prompt |
| PR2 rollback | Drop `upsert_mentions` call |
| PR3 rollback | Restore pre-PR3 `upsert_relationships` Cypher |
| PR4 rollback | Revert `find_entity` to exact match |
| PR5 rollback | Skip backfill; settings at defaults |

## Phase 1: PR1 — Domain, DTO, canonical id, prompt

- [x] 1.1 `domain/models.py`: ADD `aliases: list[str]` + `canonical_name: str | None` on `Entity`. Update `EntityWithContext.source` docstring.
- [x] 1.2 `infrastructure/llm_adapter.py`: ADD `aliases`, `canonical_name` to `_LLMEntityDTO`; extend `_SYSTEM_PROMPT`.
- [x] 1.3 `infrastructure/llm_adapter.py`: add `_resolve_entity_id(name, canonical_name, aliases, type)` → `f"{_slugify(canonical_name)}-{type}"` if canonical, else `_slugify(name)`; populate `Entity` in `extract_graph`.
- [x] 1.4 Tests: legacy `id == _slugify(name)`; canonical appends type; stoplist filter (REQ-CANON-01/02/05, AC-CANON-01/04).

## Phase 2: PR2 — `:MENTIONS` + `_flush_batch`

- [x] 2.1 `ports/graph_db_port.py`: ADD abstract `async def upsert_mentions(chunk_index, book_id, entity_ids)`.
- [x] 2.2 `infrastructure/neo4j_command_adapter.py`: implement with `WHERE ($book_id IS NULL AND c.book_id IS NULL) OR c.book_id = $book_id` guard, `MERGE (c)-[:MENTIONS]->(e)`, `coalesce(m.source_page, e.source_page)`.
- [x] 2.3 `application/index_book_use_case.py`: collect per-chunk `(chunk_index, book_id, [e.id])` in `_flush_batch`; call `upsert_mentions` after rels. Add `orphan_policy` constructor arg.
- [x] 2.4 Tests: idempotency, `book_id IS NULL`, source_page coalesce, mention call per chunk, dead-lettered chunks produce none (SCEN-PROV-01..04, AC-PROV-02).

## Phase 3: PR3 — Endpoint detection + dead-letter

- [x] 3.1 `ports/dead_letter_port.py` (NEW): `DeadLetterPort.write_orphan_relationship(record)`.
- [x] 3.2 `infrastructure/dead_letter.py` (NEW): `JSONLDeadLetter` writing required fields (`type`, src/dst ids, description, source_page, chunk_index, timestamp, missing_endpoint, `reason="orphan_endpoint"`).
- [x] 3.3 `infrastructure/neo4j_command_adapter.py`: in `upsert_relationships`, run batched endpoint query first. `fail_loud` raises with missing ids; `log_orphan` writes JSONL + persists valid subset. Track invariant.
- [x] 3.4 `application/index_book_use_case.py`: pass `orphan_policy` from Settings; record `chunk_index` on orphans.
- [x] 3.5 Tests: one batched endpoint query; `fail_loud` raises; `log_orphan` JSONL fields + valid-only persistence; invariant `input == persisted + deadletter` (SCEN-REL-01..05, AC-REL-01..03).

## Phase 4: PR4 — Tiered `find_entity` + fulltext

- [x] 4.1 `ports/graph_query_port.py`: docstring update on `find_entity` (behavior modified) and `ensure_indexes`.
- [x] 4.2 `infrastructure/neo4j_query_adapter.py`: rewrite `find_entity` cascade Tier1→4 with early stop. Scores 1.0/0.8/0.6/`ft*0.4`. Dedup by `n.id` keeping highest tier. `source` = `"book_id={b},chunk_index={i}"`.
- [x] 4.3 `infrastructure/neo4j_query_adapter.py`: Tier 4 wrapped in try/except — on fulltext missing log warning, return Tiers 1-3 (SCEN-FIND-05, AC-FIND-03).
- [x] 4.4 `infrastructure/neo4j_query_adapter.py`: extend `find_entities_batch` with `OPTIONAL MATCH (:Chunk)-[:MENTIONS]->(n)` source extraction.
- [x] 4.5 `infrastructure/neo4j_query_adapter.py`: add `CREATE FULLTEXT INDEX entity_name_aliases_index IF NOT EXISTS FOR (n:Entity) ON EACH [n.name, n.canonical_name, n.aliases]` to `ensure_indexes`.
- [x] 4.6 Tests: tier order, early-stop (Tier 4 counter == 0), `find_entity("mcp")` via alias, type filter, dedup, graceful degradation (SCEN-FIND-01..06, AC-FIND-01..04).

## Phase 5: PR5 — Backfill + settings + RAGAS compare

- [x] 5.1 `config.py`: ADD `canonical_match_mode` (Literal["slug","fuzzy"], default "slug"), `canonical_fuzzy_threshold` (0.5..1.0 validator, default 0.92), `relationship_orphan_policy` ("fail_loud"|"log_orphan", default "log_orphan"), `canonical_stoplist` (list, default []), `dead_letter_path_orphans` (Path).
- [x] 5.2 `infrastructure/llm_adapter.py`: load stoplist + match mode/threshold from Settings; gate fuzzy path (REQ-CANON-04, AC-CANON-03).
- [x] 5.3 `scripts/backfill_resilience.py` (NEW): `python scripts/backfill_resilience.py all` — `SET n.aliases = coalesce(n.aliases, [])`, `SET n.canonical_name = coalesce(n.canonical_name, n.name)`, create fulltext index. Idempotent; `--dry-run`.
- [x] 5.4 `docs/ops/backfill_resilience.md` (NEW): full rebuild command, `:MENTIONS` non-reconstructibility, rollback steps.
- [x] 5.5 `scripts/run_ragas_evaluation.py`: compare to `gr3_baseline.json`, emit `gr3_after.json` with `delta` per metric. Add `--no-compare`.
- [x] 5.6 Tests: `canonical_fuzzy_threshold` validator rejects `0.4`/`1.1`; defaults safe; backfill idempotent on second run; `--dry-run` does not write.

## Dependencies

PR1 → PR2 → PR3. PR2 → PR4 → PR5. PR3 + PR4 → PR5.

## Definition of Done (Tasks phase)

- [x] Tasks in English at `openspec/changes/graphrag-ragas-resilience/tasks.md`; per-PR task ID, files, acceptance criteria, est. lines, dependencies.
- [x] Work-unit commit plan per PR slice (test-gated checklist ordering); Review Workload Forecast carries the four literal guard lines.
- [x] Dependencies documented; concrete file paths; every REQ-PROV/CANON/REL/FIND/NFR criterion covered by ≥1 test task.
