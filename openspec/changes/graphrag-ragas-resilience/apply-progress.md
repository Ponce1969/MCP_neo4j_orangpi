# Apply Progress: graphrag-ragas-resilience

## PR Boundary

- **Current PR**: PR2 — `:MENTIONS` + `_flush_batch`
- **Chain strategy**: feature-branch-chain
- **Branch**: `feature/graphrag-ragas-resilience/pr2-mentions-flush`
- **Work-unit commit**: `68c8706fdcef6c922c1b064405109c45d1b7b720`
- **Changed lines (code + tests)**: 223 insertions, 0 deletions (under 400-line budget)

## PRs Completed

- [x] PR1 — Domain, DTO, canonical id, prompt
  - `domain/models.py`: added `aliases` and `canonical_name` to `Entity`; updated `EntityWithContext.source` docstring.
  - `infrastructure/llm_adapter.py`: extended `_LLMEntityDTO`, extraction prompt, added `_resolve_entity_id`, populated `Entity` in `extract_graph`.
  - `tests/test_llm_adapter.py`: tests for legacy id, canonical type-aware id, stoplist filtering, alias deduplication.
- [x] PR2 — `:MENTIONS` + `_flush_batch`
  - `ports/graph_db_port.py`: added abstract `upsert_mentions(chunk_index, book_id, entity_ids)`.
  - `infrastructure/neo4j_command_adapter.py`: implemented guarded `MATCH`/`MERGE` `:MENTIONS` write with `source_page` coalesce.
  - `application/index_book_use_case.py`: collected per-chunk entity ids in `_flush_batch`, called `upsert_mentions` after relationships, added `orphan_policy` constructor arg.
  - `tests/test_neo4j_command_adapter.py`: mention guard, `book_id=None`, idempotency, source-page coalesce tests.
  - `tests/test_index_book_use_case.py`: per-chunk mention calls, `book_id=None`, dead-lettered chunks produce no mentions.
  - `tests/test_ports.py`: updated dummy implementations and async-method assertions for `upsert_mentions`.

## PRs Pending

- [ ] PR3 — Endpoint detection + dead-letter
- [ ] PR4 — Tiered `find_entity` + fulltext
- [ ] PR5 — Backfill + settings + RAGAS compare

## Current PR Status and Evidence

### Quality Gates

| Gate | Command | Result |
|------|---------|--------|
| Lint | `uv run ruff check .` | All checks passed |
| Type check | `uv run mypy` | Success: no issues found in 33 source files |
| Tests | `uv run pytest tests -q` | 350 passed, 2 warnings |
| Architecture | `uv run python scripts/validate_architecture.py` | ✅ Arquitectura Hexagonal validada correctamente |

### Test Evidence

- `test_neo4j_adapter_upsert_mentions_uses_chunk_guard_and_merge` — asserts guarded `book_id` match and `MERGE (:Chunk)-[:MENTIONS]->(:Entity)`.
- `test_neo4j_adapter_upsert_mentions_with_null_book_id` — asserts `book_id=None` keeps the chunk match via the NULL guard.
- `test_neo4j_adapter_upsert_mentions_idempotent_on_repeats` — re-flush produces identical Cypher/parameters (AC-PROV-02).
- `test_neo4j_adapter_upsert_mentions_*` — Cypher contains `coalesce(m.source_page, e.source_page)`.
- `test_use_case_calls_upsert_mentions_per_successful_chunk` — one mention call per successful chunk (SCEN-PROV-01).
- `test_use_case_mentions_use_null_book_id_for_toc_less_pdf` — TOC-less chunks propagate `book_id=None` (SCEN-PROV-03).
- `test_use_case_dead_lettered_chunks_do_not_produce_mentions` — failed chunks never create `:MENTIONS` edges (SCEN-PROV-04).

### Files Changed

| File | Action | Description |
|------|--------|-------------|
| `src/book_graph_rag/ports/graph_db_port.py` | Modified | Added abstract `upsert_mentions` method to write contract |
| `src/book_graph_rag/infrastructure/neo4j_command_adapter.py` | Modified | Implemented idempotent `:MENTIONS` write with `book_id` NULL guard and `source_page` coalesce |
| `src/book_graph_rag/application/index_book_use_case.py` | Modified | Collected per-chunk entity ids; calls `upsert_mentions` after relationships; added `orphan_policy` arg |
| `tests/test_neo4j_command_adapter.py` | Modified | Mention guard, NULL `book_id`, idempotency, source-page coalesce tests |
| `tests/test_index_book_use_case.py` | Modified | Per-chunk mention wiring, NULL `book_id`, dead-letter exclusion tests |
| `tests/test_ports.py` | Modified | Updated dummy port implementations and async-method assertions |

## Blockers

None.

## Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Cross-batch entity references may still dead-letter in PR3 | Medium | Documented as SCEN-REL-05 limitation; PR3 will add batched endpoint detection |
| `orphan_policy` constructor arg is not yet consumed | Low | Reserved for PR3 endpoint-detection/dead-letter wiring; default `"log_orphan"` keeps current behavior |
| `upsert_mentions` runs after all relationships in a batch | Low | Matches design: entity writes already happened, so matches succeed; fail-loud orphan policy in PR3 aborts before mentions |
