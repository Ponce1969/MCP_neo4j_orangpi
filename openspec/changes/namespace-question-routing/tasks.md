# Tasks: Namespace Question Routing

**Decision:** Approved by maintainer for Units 0-5.
**Current phase:** Unit 0.
**Delivery strategy:** Chained work-unit commits; keep each review candidate below the 400-line budget. Unit 0 is split into 0A/0B/0C because the complete authored scope is approximately 848 lines.

**Review gate (documented waiver, 2026-09-29):** the native RDD reviewer stalls on the
provider side (`reviewing` → `collect` → `reviewer_results_required` with no artifact,
reproduced on 3.6.0 and 3.7.0, capture rejected as outside the session route). Reported
as [issue #14](https://github.com/Ponce1969/MCP_neo4j_orangpi/issues/14). The maintainer
authorized abandoning the affected lineages and reviewing each Unit 0 slice by the
alternate path: full diff review + Ruff/mypy/architecture gates + filtered tests, with
this waiver recorded. Re-enable native RDD for Units 1-5 once issue #14 is resolved.
**Production policy:** No Orange Pi or Neo4j mutation during Units 0-2.

## Unit 0 — Baseline and dataset

### Unit 0A — OpenSpec intent and design (candidate ~250 lines)

- [x] 0A.1 Create `proposal.md` and `design.md` (commit `b8d6d22`).
- [x] 0A.2 Review intent/design by the authorized alternate path (diff of `b8d6d22` +
  gates + filtered tests) because native RDD is blocked by issue #14; the affected
  lineage was abandoned with recorded operator disposition.

### Unit 0B — Normative contract and baseline (candidate ~310 lines)

- [x] 0B.1 Create `spec.md`, `tasks.md`, and the production baseline note.
- [x] 0B.2 Review the contract/baseline candidate by the authorized alternate path
  (diff + gates + filtered tests) with waiver from issue #14.

### Unit 0C — Dataset and deterministic validation (candidate ~300 lines)

- [x] 0C.1 Create `data/evaluation/namespace_routing_dataset.jsonl` with single-book,
  ambiguous, cross-book, and out-of-domain examples in English and Spanish.
- [x] 0C.2 Add dataset SHA-256, provenance, model-independent status, and record count to
  `data/evaluation/MANIFEST.json` and document the dataset in `README.md`.
- [x] 0C.3 Add and run `scripts/validate_namespace_routing_dataset.py` plus behavior tests
  for JSONL records, catalog namespace labels, manifest hash, and route cardinality.
- [x] 0C.4 Run `uv run ruff check .`, `uv run mypy .`,
  `uv run python scripts/validate_architecture.py`, and focused JSON/data validation.
- [x] 0C.5 Review the dataset/validator candidate by the authorized alternate path
  (diff + gates + focused tests) with waiver from issue #14.

**Unit 0 closure:** all three slices are complete and verified by the authorized
alternate path. Native RDD remains blocked by issue #14 and is explicitly waived for
Unit 0. Pre-existing full-suite blockers are documented in the baseline note; the
filtered verification (1409 tests) and all repository gates pass.

## Unit 1 — Domain contracts and classifier

- [ ] 1.1 Add immutable domain models for profiles, candidates, and route decisions.
- [ ] 1.2 Add pure cosine, score ordering, margin, and abstention policy functions.
- [ ] 1.3 Add `NamespaceProfilePort` contract without infrastructure imports.
- [ ] 1.4 Add behavior-first tests for dimensions, zero norms, ties, and thresholds.
- [ ] 1.5 Verify architecture, Ruff, mypy, and full tests.

## Unit 2 — Read-only profile builder

- [ ] 2.1 Add a read port for namespace profile source documents.
- [ ] 2.2 Implement a read-only Neo4j adapter using parameterized Cypher.
- [ ] 2.3 Build profiles from chunks/TOC first; use entities/summaries only as supplements.
- [ ] 2.4 Persist a local artifact with model/catalog/graph/profile metadata.
- [ ] 2.5 Add deterministic fixture tests and a dry-run command.
- [ ] 2.6 Do not write profile nodes, embeddings, or indexes to Neo4j.

## Unit 3 — Runtime routing and fallback

- [ ] 3.1 Add `RouteQuestionUseCase` using lexical hints and local embeddings.
- [ ] 3.2 Validate every selected namespace through `CatalogScopeResolver`.
- [ ] 3.3 Implement score, margin, stale-profile, out-of-domain, and cross-book abstention.
- [ ] 3.4 Preserve MCP `require_scope`; never silently issue an unscoped query.
- [ ] 3.5 Add top-2/fan-out contract without changing existing MCP tool signatures.
- [ ] 3.6 Test Spanish/English and ambiguous dataset cases.

## Unit 4 — SQLite cache and telemetry

- [ ] 4.1 Add `RoutingTelemetryPort` separate from `QueryLoggerPort`.
- [ ] 4.2 Add SQLite adapter with parameterized SQL, WAL/busy timeout, and retention.
- [ ] 4.3 Bind cache keys to query fingerprint, namespace, model/profile/catalog versions,
  and graph snapshot.
- [ ] 4.4 Keep raw query/answer storage opt-in and disabled by default.
- [ ] 4.5 Add correction labels and prevent unlabeled events from becoming goldens.
- [ ] 4.6 Add temporary-database tests, expiry tests, and concurrent-write tests.

## Unit 5 — Integration, evaluation, and rollout

- [ ] 5.1 Add routing metrics and a reproducible report over the committed dataset.
- [ ] 5.2 Compare retrieval/generation metrics separately from routing metrics.
- [ ] 5.3 Integrate with the Gentle-AI caller while keeping MCP scope enforcement intact.
- [ ] 5.4 Add feature-flag rollback to the existing explicit-scope path.
- [ ] 5.5 Run read-only production smoke/evaluation with explicit approval and no graph
  mutation.
- [ ] 5.6 Update relevant Phase 6/7 evidence only if the MCP contract or exposure surface
  changes.

## ODD/RDD completion checklist

- [ ] One clear work-unit commit per completed unit.
- [ ] Tests and evidence live with the behavior they verify.
- [ ] Changed-line forecast stays below 400 per review candidate, or is explicitly chained.
- [ ] Review receipt is bound to the exact candidate and scope.
- [ ] Independent read-only validation passes.
- [ ] Delivery remains ordinary repository policy; review evidence never grants delivery
  authority.
