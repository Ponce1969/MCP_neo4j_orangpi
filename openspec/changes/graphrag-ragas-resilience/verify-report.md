# Verify Report: graphrag-ragas-resilience — Phase 4 / PR4 (Tiered `find_entity` + fulltext)

- **Change**: `graphrag-ragas-resilience`
- **Scope**: PR4 only — `infrastructure/neo4j_query_adapter.py`, `ports/graph_query_port.py`, `tests/test_neo4j_query_adapter.py`
- **Mode**: openspec
- **Execution mode**: interactive
- **Verifier**: sdd-verify sub-agent
- **Date**: 2026-08-13

## Executive Summary

PR4 implements the tiered `find_entity` cascade (Tier 1→4 with early-stop, scores 1.0/0.8/0.6/ft*0.4), `find_entities_batch` source extraction, and the `entity_name_aliases_index` fulltext index, matching spec REQ-FIND-01..06, REQ-PROV-04, AC-FIND-01..04 and the design. One quality gate (`ruff`) was **failing as-committed** (E501 at `test_neo4j_query_adapter.py:489`, line 107 > 100) despite the apply-progress report claiming "All checks passed" — this is fixed in this run and now green. A spec requirement (REQ-NFR-03) for `EXPLAIN` validation of the new Cypher has no test coverage in PR4 (deferred by design to container-gated integration tests, which are not present).

## Quality Gates (re-run independently)

| Gate | Command | Result (before) | Result (after fix) |
|------|---------|-----------------|--------------------|
| Lint | `uv run ruff check .` | **FAILED** — 1 error (E501 line 489) | **PASSED** — "All checks passed!" |
| Type check | `uv run mypy` | PASSED — "Success: no issues found in 35 source files" | PASSED (unchanged) |
| Tests | `uv run pytest tests -q` | 367 passed, 2 warnings | 367 passed, 2 warnings (PR4 file: 45 passed) |
| Architecture | `uv run python scripts/validate_architecture.py` | PASSED — "Arquitectura Hexagonal validada correctamente" | PASSED (unchanged) |

Command evidence:
- `ruff`: exit 1 → exit 0 after fix.
- `mypy`: exit 0.
- `pytest tests/test_neo4j_query_adapter.py -q`: 45 passed in 1.21s.
- `pytest tests -q`: 367 passed, 2 warnings in 21.47s.
- `validate_architecture.py`: exit 0.
- test_output_hash / build_output_hash: not captured (no hashing tooling in this run; output quoted verbatim above).

## Spec Compliance Matrix (PR4 requirements)

| Req / AC | Description | Status | Evidence |
|----------|-------------|--------|----------|
| REQ-FIND-01 | Tiered cascade Tier1 exact → Tier2 case-insensitive → Tier3 CONTAINS → Tier4 fulltext, early stop | COMPLIANT | `neo4j_query_adapter.py` L96-161; tests `test_find_entity_tier1_short_circuits`, `tier2_case_insensitive`, `tier3_partial`, `tier4_alias_returns_canonical_entity` |
| REQ-FIND-02 | `entity_type` filter in every tier | COMPLIANT | `WHERE $entity_type IS NULL OR n.type = $entity_type` in all 4 tier queries; `test_find_entity_type_filter_applied_to_all_tiers` |
| REQ-FIND-03 | Ranking (1.0/0.8/0.6/ft*0.4) + dedup by id keeping highest tier | COMPLIANT-with-caveat | Scores in queries; dedup `if entity.entity.id not in results_by_id`; `test_find_entity_dedup_keeps_highest_tier`. Caveat: early-stop means later tiers never run once an earlier tier returns, so cross-tier merge only occurs within a tier (see Finding S-2). |
| REQ-FIND-04 | Fulltext only after index; degrade gracefully if unavailable | COMPLIANT | Tier 4 inside `try/except Exception` → `logger.warning`; `test_find_entity_graceful_degradation_without_fulltext_index` |
| REQ-FIND-05 | `find_entity("mcp")` returns canonical "Model Context Protocol" via alias | COMPLIANT (adapter path) | `test_find_entity_tier4_alias_returns_canonical_entity` (entity with `aliases=["MCP"]`, `canonical_name`); relies on PR1/PR5 canonicalization for data |
| REQ-FIND-06 | `LIMIT $limit` (default 100) and signature unchanged | COMPLIANT | `limit = 100`; `LIMIT $limit` per tier; signature `find_entity(name, entity_type) -> list[EntityWithContext]` unchanged |
| REQ-PROV-04 | `source` populated with `chunk_index` (+`book_id`) in `find_entity` & `find_entities_batch` | COMPLIANT | `_format_source` → `"book_id={b},chunk_index={i}"`; `test_find_entity_tier1_short_circuits`, `test_find_entities_batch_populates_source` |
| AC-FIND-01 | `find_entity("mcp")` via alias | COMPLIANT | `test_find_entity_tier4_alias_returns_canonical_entity` |
| AC-FIND-02 | Tier1 short-circuit; Tier4 counter 0 | COMPLIANT | `test_find_entity_tier1_short_circuits` asserts `CALL db.index.fulltext.queryNodes` not in queries[0] |
| AC-FIND-03 | Graceful degradation with no exception to caller | COMPLIANT | `test_find_entity_graceful_degradation_without_fulltext_index` |
| AC-FIND-04 | Backward-compat exact-match unchanged | COMPLIANT | `test_find_entity_by_name` asserts `MATCH (n:Entity {name: $name})`; Tier1 identical to prior behavior |
| Design: index DDL | `entity_name_aliases_index` fulltext over `n.name, n.canonical_name, n.aliases` | COMPLIANT | `ensure_indexes` L402-406; `test_ensure_indexes_fulltext_uses_on_each` |

## Correctness Table (implementation vs design)

| Design element | Implemented? | Notes |
|----------------|--------------|-------|
| Tier scores 1.0/0.8/0.6/ft*0.4 | Yes | Literal in query; `_score` tuple value is unused (see S-1) |
| Early stop after first non-empty tier | Yes | `if results_by_id: break` L140 |
| `source` format `book_id={b},chunk_index={i}` | Yes | `_format_source` L75-82 |
| Tier 4 wrapped in try/except with warning | Yes | L143-175 |
| `find_entities_batch` `OPTIONAL MATCH (c:Chunk)-[:MENTIONS]->(n)` | Yes | L184-189 |
| Fulltext index `ON EACH [n.name, n.canonical_name, n.aliases]` | Yes | L403-406 |
| Docstring updates on port | Yes | `graph_query_port.py` L28-34, L78-82 |

## Design Coherence

- No design deviations found in the implemented code. The adapter matches the design's Cypher patterns (Tier 1-4 queries, batch source extraction, index DDL) and the `_format_source` contract.
- REQ-FIND-03 cross-tier merge is a partial realization due to the design's explicit early-stop decision (documented in design "stop after a non-empty tier"). This is consistent with AC-FIND-02, not a code deviation.

## Findings

### CRITICAL
None remaining. (One critical gate failure was present as-committed and fixed in this run — see below.)

- **F-1 (fixed, was CRITICAL gate failure)**: `uv run ruff check .` FAILED on `tests/test_neo4j_query_adapter.py:489` — E501 line too long (107 > 100). This contradicts `apply-progress.md`, which claimed "Lint | `uv run ruff check .` | All checks passed". The as-committed work-unit commit `30548ef` did NOT pass the project's own gate. Fixed by wrapping the assertion; re-run confirms "All checks passed!".
  - Evidence: `ruff` exit 1 → `E501 Line too long (107 > 100) --> tests\test_neo4j_query_adapter.py:489:101`; after fix exit 0.

### WARNING
- **F-2 (WARNING)**: REQ-NFR-03 ("New Cypher MUST be validated with EXPLAIN") is not satisfied by any test in PR4. No test runs `adapter.explain()` (or `EXPLAIN`) against the four tier queries, the `find_entities_batch` source query, or the fulltext index DDL. The only explain tests are generic (`test_explain_runs_explain_query` with a dummy query) and unrelated (`test_text2cypher_adapter`). The design's Testing Strategy table scopes EXPLAIN validation to "Container-gated tests", but no such test exists in the tree (`tests/`, `tests/integration/`).
  - Evidence: grep for `explain`/`queryNodes` across `tests/` shows only the generic dummy-query explain tests; no tier-query EXPLAIN.
  - Recommendation: add a container-gated integration test that calls `adapter.explain("<tier query>")` for each of the 4 tiers + batch source query + the fulltext `CREATE FULLTEXT INDEX` statement, gated by a real Neo4j marker. Alternatively document explicitly that PR4 relies on the integration suite and accept the deferral.

### SUGGESTION
- **S-1 (SUGGESTION)**: `tier_queries` is a `list[tuple[float, str]]` where the leading `float` (`1.0/0.8/0.6`) is never used — the score comes from the literal in the query string. Dead/confusing data; consider either dropping the tuple or using the value. Cosmetic; ruff does not flag (leading underscore). Not a gate issue.
- **S-2 (SUGGESTION)**: REQ-FIND-03's "merge results from lower tiers with earlier-tier results" is effectively limited to within-tier deduplication because of the early-stop design. For the common case (paraphrase aliases) this is fine since earlier tiers return empty and Tier 4 runs. But if an exact match co-exists with a relevant alias-only entity, only the exact match is returned. This matches the design's speed trade-off and AC-FIND-02; documented as a known limitation rather than a defect.

## Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| PR4 as-committed had a failing lint gate (E501) not caught by apply-progress | Low (now fixed) | Re-run ruff after fix; amend commit `30548ef` / note in PR. |
| REQ-NFR-03 EXPLAIN coverage missing (F-2) | Medium | Add container-gated EXPLAIN integration test before merge to main. |
| Early-stop may skip alias-only relevant entities when an exact match exists (S-2) | Low | Documented design trade-off; acceptable per AC-FIND-02. |
| Fulltext availability varies across Neo4j deployments | Low | Tier 4 wrapped in try/except; degrades to Tiers 1-3 with warning. |

## Verdict

**PASS WITH WARNINGS** — all quality gates are green after the trivial ruff fix; implementation matches spec REQ-FIND-01..06, REQ-PROV-04, AC-FIND-01..04 and the design for PR4. One WARNING remains (REQ-NFR-03 EXPLAIN validation not covered in PR4; deferred by design to container-gated integration tests which must be added before merge to main).

## Next Steps

- Proceed to PR5 (Backfill + settings + RAGAS compare) once the EXPLAIN integration test gap (F-2) is addressed or explicitly accepted as deferred.
- Amend/annotate commit `30548ef` to reflect the ruff fix.
