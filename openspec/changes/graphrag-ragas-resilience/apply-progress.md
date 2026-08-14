# Apply Progress: graphrag-ragas-resilience

## PR Boundary

- **Current PR**: PR3 — Endpoint detection + dead-letter
- **Chain strategy**: feature-branch-chain
- **Branch**: `feature/graphrag-ragas-resilience/pr3-endpoint-deadletter`
- **Work-unit commit**: TBD
- **Changed lines (code + tests)**: ~411 insertions, ~15 deletions (slightly over 400-line budget; scope is the minimum needed for the invariant and policy tests)

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
- [x] PR3 — Endpoint detection + dead-letter
  - `ports/dead_letter_port.py` (NEW): `DeadLetterPort.write_orphan_relationship(record)`.
  - `infrastructure/dead_letter.py` (NEW): `JSONLDeadLetter` writing required fields + timestamp.
  - `infrastructure/neo4j_command_adapter.py`: batched endpoint query before relationship writes; `fail_loud` raises with missing ids; `log_orphan` writes JSONL + persists valid subset; invariant tracked.
  - `application/index_book_use_case.py`: sets `chunk_index` on every relationship before flushing; `orphan_policy` is passed from `Settings`.
  - `domain/models.py`: added `chunk_index: int | None = None` to `Relationship` so the use case can propagate provenance to the adapter without changing the `upsert_relationships` signature.
  - `config.py`: added `relationship_orphan_policy` (Literal["fail_loud", "log_orphan"], default "log_orphan") and `dead_letter_path_orphans`.
  - `main.py`: passes `settings.relationship_orphan_policy` to `IndexBookUseCase`.
  - `tests/test_neo4j_command_adapter.py`: batched endpoint query, `fail_loud` raises, `log_orphan` JSONL fields, valid-only persistence, invariant test.
  - `tests/test_index_book_use_case.py`: asserts `chunk_index` is recorded on relationships.
  - `tests/test_ports.py`: `DeadLetterPort` abstract/instantiation tests; `JSONLDeadLetter` field test.
  - `tests/test_config.py`: default and validation tests for `relationship_orphan_policy`.
  - `tests/test_cli_main.py`: updated `FakeSettings` with `relationship_orphan_policy`.

## PRs Pending

- [ ] PR4 — Tiered `find_entity` + fulltext
- [ ] PR5 — Backfill + settings + RAGAS compare

## Current PR Status and Evidence

### Quality Gates

| Gate | Command | Result |
|------|---------|--------|
| Lint | `uv run ruff check .` | All checks passed |
| Type check | `uv run mypy` | Success: no issues found in 35 source files |
| Tests | `uv run pytest tests -q` | 359 passed, 2 warnings |
| Architecture | `uv run python scripts/validate_architecture.py` | ✅ Arquitectura Hexagonal validada correctamente |

### Test Evidence

- `test_upsert_relationships_runs_batched_endpoint_query_first` — one `OPTIONAL MATCH (n:Entity {id: id})` query per batch with all source/target ids (REQ-REL-01).
- `test_upsert_relationships_fail_loud_raises_with_missing_ids` — `fail_loud` raises `ValueError` with missing endpoint ids and aborts before any relationship write (SCEN-REL-02, AC-REL-02).
- `test_upsert_relationships_log_orphan_writes_jsonl_and_persists_valid` — orphan JSONL contains `type`, src/dst ids, `description`, `source_page`, `chunk_index`, `missing_endpoint`, `reason="orphan_endpoint"` and `timestamp`; valid subset is persisted (SCEN-REL-01, AC-REL-01).
- `test_upsert_relationships_log_orphan_invariant` — `input count == persisted + dead_lettered_orphans` (SCEN-REL-06, AC-REL-01).
- `test_use_case_records_chunk_index_on_relationships` — every relationship carries its source `chunk_index` (REQ-REL-03).
- `test_jsonl_dead_letter_appends_structured_record` — `JSONLDeadLetter` writes all required fields and appends a timestamp.
- `test_settings_orphan_policy_rejects_invalid_value` — `relationship_orphan_policy` is constrained to `"fail_loud"` / `"log_orphan"`.

### Files Changed

| File | Action | Description |
|------|--------|-------------|
| `src/book_graph_rag/ports/dead_letter_port.py` | Created | Abstract dead-letter port for orphan relationship records |
| `src/book_graph_rag/infrastructure/dead_letter.py` | Created | JSONL append-only dead-letter writer with timestamp |
| `src/book_graph_rag/infrastructure/neo4j_command_adapter.py` | Modified | Batched endpoint detection; `fail_loud`/`log_orphan` policy; invariant logging |
| `src/book_graph_rag/application/index_book_use_case.py` | Modified | Sets `chunk_index` on relationships; `orphan_policy` wired from Settings |
| `src/book_graph_rag/domain/models.py` | Modified | Added `chunk_index` to `Relationship` for provenance propagation |
| `src/book_graph_rag/config.py` | Modified | Added `relationship_orphan_policy` and `dead_letter_path_orphans` |
| `src/book_graph_rag/main.py` | Modified | Passes `relationship_orphan_policy` to `IndexBookUseCase` |
| `tests/test_neo4j_command_adapter.py` | Modified | Endpoint query, fail-loud, log-orphan, invariant tests |
| `tests/test_index_book_use_case.py` | Modified | `chunk_index` provenance test |
| `tests/test_ports.py` | Modified | `DeadLetterPort` and `JSONLDeadLetter` tests |
| `tests/test_config.py` | Modified | `relationship_orphan_policy` default/validation tests |
| `tests/test_cli_main.py` | Modified | `FakeSettings` exposes `relationship_orphan_policy` |

## Blockers

None.

## Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| PR3 diff slightly exceeds 400-line budget | Low | Scope is the minimum required for policy/invariant coverage; PR4/PR5 should stay smaller |
| `Relationship.chunk_index` was not in PR1/PR2 scope | Low | Field is additive with default `None`; documented as a PR3 deviation driven by REQ-REL-03 |
| Cross-batch entity references remain dead-lettered | Medium | Documented as SCEN-REL-05 limitation; dead-letter growth target ≤5% |
| `fail_loud` aborts the whole batch including valid relationships | Low | Matches spec intent: fail-fast at indexing time |
