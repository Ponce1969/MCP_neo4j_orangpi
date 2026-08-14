# Apply Progress: graphrag-ragas-resilience

## PR Boundary

- **Current PR**: PR4 — Tiered `find_entity` + fulltext + EXPLAIN remediation
- **Chain strategy**: feature-branch-chain
- **Branch**: `feature/graphrag-ragas-resilience/pr4-tiered-find-entity`
- **Work-unit commit**: `30548ef` (original PR4) + remediation commit
- **Changed lines (code + tests)**: 398 insertions, 27 deletions (425 total; slightly over 400-line budget due to required tier + graceful-degradation test coverage)

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
  - `application/index_book_use_case.py`: sets `chunk_index` on relationships; `orphan_policy` is passed from `Settings`.
  - `domain/models.py`: added `chunk_index: int | None = None` to `Relationship` so the use case can propagate provenance to the adapter without changing the `upsert_relationships` signature.
  - `config.py`: added `relationship_orphan_policy` (Literal["fail_loud", "log_orphan"], default "log_orphan") and `dead_letter_path_orphans`.
  - `main.py`: passes `settings.relationship_orphan_policy` to `IndexBookUseCase`.
  - `tests/test_neo4j_command_adapter.py`: batched endpoint query, `fail_loud` raises, `log_orphan` JSONL fields, valid-only persistence, invariant test.
  - `tests/test_index_book_use_case.py`: asserts `chunk_index` is recorded on relationships.
  - `tests/test_ports.py`: `DeadLetterPort` abstract/instantiation tests; `JSONLDeadLetter` field test.
  - `tests/test_config.py`: default and validation tests for `relationship_orphan_policy`.
  - `tests/test_cli_main.py`: updated `FakeSettings` with `relationship_orphan_policy`.
- [x] PR4 — Tiered `find_entity` + fulltext + EXPLAIN remediation
  - `ports/graph_query_port.py`: updated docstrings for `find_entity` and `ensure_indexes` to describe tiered cascade and fulltext index.
  - `infrastructure/neo4j_query_adapter.py`: rewrote `find_entity` with Tier 1→4 cascade (scores 1.0/0.8/0.6/ft*0.4), early stop, dedup by `n.id`, and `source` from `(:Chunk)-[:MENTIONS]->(n)`.
  - `infrastructure/neo4j_query_adapter.py`: wrapped Tier 4 fulltext call in try/except; logs warning and returns Tiers 1-3 when the index is unavailable.
  - `infrastructure/neo4j_query_adapter.py`: extended `find_entities_batch` with `OPTIONAL MATCH (:Chunk)-[:MENTIONS]->(n)` source extraction.
  - `infrastructure/neo4j_query_adapter.py`: added `entity_name_aliases_index` fulltext index to `ensure_indexes`.
  - `infrastructure/neo4j_query_adapter.py`: extended `explain()` with optional `parameters` so parameterized Cypher can be validated with `EXPLAIN`.
  - `tests/test_neo4j_query_adapter.py`: tier order, early-stop, `find_entity("mcp")` via alias, type-filter across tiers, dedup, graceful degradation, batch source extraction, and updated ensure_indexes tests.
  - `tests/integration/test_neo4j_query_adapter_explain.py` (NEW): container-gated EXPLAIN validation for all 4 tier queries, the `find_entities_batch` source query, and the `entity_name_aliases_index` DDL (REQ-NFR-03, verify-report F-2).
  - `pyproject.toml`: registered `neo4j_integration` pytest marker for container-gated tests.

## PRs Pending

- [ ] PR5 — Backfill + settings + RAGAS compare

## Current PR Status and Evidence

### Quality Gates

| Gate | Command | Result |
|------|---------|--------|
| Lint | `uv run ruff check .` | All checks passed |
| Type check | `uv run mypy` | Success: no issues found in 35 source files |
| Tests | `uv run pytest tests -q` | 367 passed, 1 skipped, 2 warnings |
| Architecture | `uv run python scripts/validate_architecture.py` | ✅ Arquitectura Hexagonal validada correctamente |

### Test Evidence

- `test_find_entity_tier1_short_circuits` — exact match returns Tier 1 results and only one query is executed; Tier 4 fulltext is not reached (AC-FIND-02, SCEN-FIND-04).
- `test_find_entity_tier2_case_insensitive` — Tier 2 matches when exact match is absent; confidence is 0.8 (REQ-FIND-01).
- `test_find_entity_tier3_partial` — Tier 3 `CONTAINS` runs only when Tiers 1-2 are empty; confidence is 0.6 (REQ-FIND-01).
- `test_find_entity_tier4_alias_returns_canonical_entity` — `find_entity("mcp")` resolves to the canonical "Model Context Protocol" entity via alias fulltext hit (AC-FIND-01, SCEN-FIND-02).
- `test_find_entity_type_filter_applied_to_all_tiers` — `entity_type` filter appears in every tier query (REQ-FIND-02, SCEN-FIND-03).
- `test_find_entity_dedup_keeps_highest_tier` — duplicate ids within a tier collapse to one entry with the highest-tier score (SCEN-FIND-06).
- `test_find_entity_graceful_degradation_without_fulltext_index` — missing fulltext index logs a warning and returns Tiers 1-3 results without raising to the caller (AC-FIND-03, SCEN-FIND-05).
- `test_find_entities_batch_populates_source` — batch lookup extracts `chunk_index`/`book_id` via `OPTIONAL MATCH (:Chunk)-[:MENTIONS]->(n)` (REQ-PROV-04).
- `test_ensure_indexes_creates_expected_indexes` / `test_ensure_indexes_fulltext_uses_on_each` — `entity_name_aliases_index` fulltext index is created over `n.name`, `n.canonical_name`, `n.aliases` (REQ-FIND-04).
- `tests/integration/test_neo4j_query_adapter_explain.py::test_explain_pr4_tiered_find_entity_queries` — container-gated EXPLAIN validation for the 6 PR4 Cypher statements; skipped when `NEO4J_PASSWORD` is unset or the container is unreachable (REQ-NFR-03, F-2).

### Files Changed

| File | Action | Description |
|------|--------|-------------|
| `src/book_graph_rag/ports/graph_query_port.py` | Modified | Updated docstrings for tiered `find_entity` and fulltext `ensure_indexes` |
| `src/book_graph_rag/infrastructure/neo4j_query_adapter.py` | Modified | Tiered `find_entity` cascade, source extraction, graceful fulltext degradation, `find_entities_batch` source extraction, new fulltext index, optional `parameters` for `explain()` |
| `tests/test_neo4j_query_adapter.py` | Modified | Tier order, early-stop, alias lookup, type filter, dedup, graceful degradation, batch source, updated ensure_indexes tests, and E501 fix |
| `tests/integration/test_neo4j_query_adapter_explain.py` | Created | Container-gated EXPLAIN validation for PR4 Cypher |
| `pyproject.toml` | Modified | Registered `neo4j_integration` marker |
| `openspec/changes/graphrag-ragas-resilience/verify-report.md` | Created | PR4 verification report including F-1/F-2 findings |

## Blockers

None.

## Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| PR4 diff is 425 changed lines, slightly above 400-line budget | Low | Driven by required tier + graceful-degradation test coverage; PR5 should be smaller |
| Tiered `find_entity` changes query behavior for existing exact-match callers | Low | Tier 1 is identical to the previous query when an exact match exists; backward-compat test covers this |
| Fulltext index may be unavailable on some Neo4j deployments | Low | Tier 4 is wrapped in try/except and degrades to Tiers 1-3 with a warning |
| `LIMIT $limit` applies to rows before dedup, so highly-mentioned entities can consume the cap | Medium | Documented limitation; acceptable for current retrieval use cases |
| Cross-batch entity references remain dead-lettered | Medium | Documented as SCEN-REL-05 limitation; dead-letter growth target ≤5% |
| Container-gated EXPLAIN test only runs when a live Neo4j is configured | Low | Test auto-skips with clear message; CI can enable it by setting `NEO4J_PASSWORD` and starting the container |

## PR4 Remediation Notes

- **F-1 (ruff E501)**: fixed by wrapping the long assertion in `test_find_entity_graceful_degradation_without_fulltext_index`.
- **F-2 (REQ-NFR-03 EXPLAIN coverage)**: added `tests/integration/test_neo4j_query_adapter_explain.py` with `@pytest.mark.neo4j_integration`. The test calls `adapter.explain()` on each PR4 Cypher statement after creating the fulltext index. It is skipped when `NEO4J_PASSWORD` is unset or the container is unreachable.
- The `explain()` method was extended with an optional `parameters` argument so the EXPLAIN of parameterized tier/batch queries can bind real values without changing existing callers.
