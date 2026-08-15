# Archive Report: graphrag-ragas-resilience

- Change: graphrag-ragas-resilience
- Status: COMPLETED
- Date: 2026-08-13
- Artifact store: openspec
- Delivery strategy: auto-chain, feature-branch-chain

## Summary

The graphrag-ragas-resilience change remediates structural debt in the GraphRAG pipeline so that RAGAS metrics for the GR.3 baseline reflect real retrieval and generation quality instead of silent failures. The implementation spans five chained PRs: entity canonicalization, chunk-entity provenance via :MENTIONS, relationship endpoint validation with dead-letter logging, tiered entity lookup with fulltext fallback, and operational backfill plus RAGAS comparison tooling.

## Outcome

- All functional requirements from spec.md implemented.
- All quality gates pass.
- Verification verdict: PASS with no CRITICAL or WARNING findings.
- The change is ready for review and merge via the feature-branch-chain.

## What Was Implemented

### PR1 — Domain, DTO, canonical id, prompt
- Added aliases and canonical_name to Entity and _LLMEntityDTO.
- Added _resolve_entity_id with legacy slugify(name) compatibility and type-aware canonical ids.
- Extended extraction prompt to request aliases and canonical_name.
- Tests for legacy id, canonical id, stoplist filtering, alias deduplication.

### PR2 — :MENTIONS + _flush_batch
- Added upsert_mentions to GraphDatabasePort.
- Implemented NULL-safe :MENTIONS MERGE in Neo4jCommandAdapter.
- Wired per-chunk mention creation in IndexBookUseCase._flush_batch.
- Tests for idempotency, book_id=None, source_page coalesce, dead-letter exclusion.

### PR3 — Endpoint detection + dead-letter
- Added DeadLetterPort and JSONLDeadLetter.
- Added batched endpoint detection before relationship writes.
- Implemented fail_loud and log_orphan policies.
- Added chunk_index to Relationship for provenance.
- Added relationship_orphan_policy and dead_letter_path_orphans to Settings.
- Tests for batch detection, fail_loud, log_orphan JSONL fields, zero-silent-drop invariant.

### PR4 — Tiered find_entity + fulltext + EXPLAIN remediation
- Rewrote find_entity with exact / case-insensitive / CONTAINS / fulltext tiers and early stop.
- Added source extraction from (:Chunk)-[:MENTIONS]->(:Entity) in find_entity and find_entities_batch.
- Added entity_name_aliases_index fulltext index to ensure_indexes.
- Added graceful degradation when fulltext is unavailable.
- Extended explain() with optional parameters.
- Added container-gated EXPLAIN integration test for all PR4 Cypher statements.
- Remediated as-committed ruff E501 and missing EXPLAIN coverage findings.

### PR5 — Backfill + settings + RAGAS compare
- Added canonical_match_mode, canonical_fuzzy_threshold, and canonical_stoplist to Settings.
- Wired canonicalization settings into LLMAdapter.
- Created scripts/backfill_resilience.py for legacy graph alias/canonical defaults and fulltext index.
- Created docs/ops/backfill_resilience.md with full rebuild command and rollback steps.
- Updated scripts/run_ragas_evaluation.py to compare against gr3_baseline.json and emit gr3_after.json with deltas.
- Added --no-compare flag.
- Tests for config validators, fuzzy gating, backfill idempotency, dry-run, and baseline/delta helpers.

## Final Quality Gates

| Gate | Command | Result |
|------|---------|--------|
| Lint | uv run ruff check . | All checks passed |
| Type check | uv run mypy | Success: no issues found in 35 source files |
| Tests | uv run pytest tests -q | 381 passed, 1 skipped, 2 warnings |
| Architecture | uv run python scripts/validate_architecture.py | Arquitectura Hexagonal validada correctamente |

The skipped test is the container-gated Neo4j integration test that runs only when a live Neo4j container is configured.

## Artifacts

- openspec/changes/graphrag-ragas-resilience/proposal.md
- openspec/changes/graphrag-ragas-resilience/spec.md
- openspec/changes/graphrag-ragas-resilience/design.md
- openspec/changes/graphrag-ragas-resilience/tasks.md
- openspec/changes/graphrag-ragas-resilience/apply-progress.md
- openspec/changes/graphrag-ragas-resilience/verify-report.md
- openspec/changes/graphrag-ragas-resilience/archive-report.md (this file)

## Decisions and Risks Accepted

- Canonicalization defaults to deterministic slug mode to avoid false-positive merges.
- Fuzzy canonicalization is opt-in via Settings and gated by a high threshold (0.92).
- find_entity uses early-stop after the first non-empty tier; this is a speed/precision trade-off documented as a known limitation.
- LIMIT applies per tier before deduplication; highly-mentioned entities can consume the cap.
- Cross-batch entity references remain dead-lettered at first pass (SCEN-REL-05).
- Backfill script does not reconstruct :MENTIONS edges; full re-index is required for provenance.

## Known Limitations and Follow-ups

- Container-gated EXPLAIN test only runs against a live Neo4j container; enable in CI by setting NEO4J_PASSWORD and starting the container.
- RAGAS comparison requires an existing gr3_baseline.json; use --no-compare when no baseline exists.
- SUGGESTION S-1: tier_queries carries an unused leading score tuple element. Cosmetic; can be refactored later.
- SUGGESTION S-2: early-stop limits cross-tier merging. Accepted design trade-off.

## Definition of Done (Archive Phase)

- [x] All artifacts exist in openspec/changes/graphrag-ragas-resilience/.
- [x] Apply and verify phases report success.
- [x] Final quality gates pass.
- [x] Archive report summarizes outcome, decisions, and known limitations.
- [x] No CRITICAL or WARNING findings remain unaddressed.

## Next Steps

- Review the chained PRs in order and merge via the feature-branch-chain strategy.
- Run the full RAGAS evaluation on the OrangePi after re-indexing to validate the >=10% improvement target.
