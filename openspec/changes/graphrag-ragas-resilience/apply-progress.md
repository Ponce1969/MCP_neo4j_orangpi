# Apply Progress: graphrag-ragas-resilience

## PR Boundary

- **Current PR**: PR1 — Domain, DTO, canonical id, prompt
- **Chain strategy**: feature-branch-chain
- **Branch**: `feature/graphrag-ragas-resilience/pr1-canonical-ids`
- **Work-unit commit**: `9f40e24f89442923fcb6e825a6a19bea563b3ca8`
- **Changed lines (code + tests)**: 204 insertions, 17 deletions (under 400-line budget)

## PRs Completed

- [x] PR1 — Domain, DTO, canonical id, prompt
  - `domain/models.py`: added `aliases` and `canonical_name` to `Entity`; updated `EntityWithContext.source` docstring.
  - `infrastructure/llm_adapter.py`: extended `_LLMEntityDTO`, extraction prompt, added `_resolve_entity_id`, populated `Entity` in `extract_graph`.
  - `tests/test_llm_adapter.py`: tests for legacy id, canonical type-aware id, stoplist filtering, alias deduplication.

## PRs Pending

- [ ] PR2 — `:MENTIONS` + `_flush_batch`
- [ ] PR3 — Endpoint detection + dead-letter
- [ ] PR4 — Tiered `find_entity` + fulltext
- [ ] PR5 — Backfill + settings + RAGAS compare

## Current PR Status and Evidence

### Quality Gates

| Gate | Command | Result |
|------|---------|--------|
| Lint | `uv run ruff check .` | All checks passed |
| Type check | `uv run mypy` | Success: no issues found in 33 source files |
| Tests | `uv run pytest tests -q` | 344 passed, 2 warnings |
| Architecture | `uv run python scripts/validate_architecture.py` | ✅ Arquitectura Hexagonal validada correctamente |

### Test Evidence

- `test_resolve_entity_id_legacy_path_no_canonical` — asserts `id == slugify(name)` with no type suffix.
- `test_resolve_entity_id_legacy_path_ignores_aliases` — aliases alone preserve legacy id.
- `test_resolve_entity_id_canonical_appends_type` — canonical id is `slugify(canonical_name)-type`.
- `test_resolve_entity_id_type_aware_distinct_nodes` — same canonical name with different types yields distinct ids.
- `test_resolve_entity_id_filters_stoplist` — stoplisted aliases removed case-insensitively.
- `test_resolve_entity_id_deduplicates_aliases_case_insensitive` — aliases deduplicated case-insensitively.
- `test_extract_graph_populates_aliases_and_canonical_name` — end-to-end extraction populates `aliases`/`canonical_name` and computes canonical id.

### Files Changed

| File | Action | Description |
|------|--------|-------------|
| `src/book_graph_rag/domain/models.py` | Modified | Added `aliases`/`canonical_name` to `Entity`; updated `EntityWithContext.source` docstring |
| `src/book_graph_rag/infrastructure/llm_adapter.py` | Modified | Extended DTO/prompt; added `_resolve_entity_id`; populated `Entity` with alias/canonical fields |
| `tests/test_llm_adapter.py` | Modified | Added canonicalization unit tests and extraction integration test |

## Blockers

None.

## Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| LLM may return empty `canonical_name` strings | Low | `_resolve_entity_id` treats empty/falsy `canonical_name` as legacy path |
| Stoplist filtering is case-insensitive by design | Low | Documented in helper docstring; matches expected domain-stoplist behavior |
| PR1 commit tree includes SDD artifact commits | Low | Work-unit commit is isolated; artifact commits are documentation-only |
