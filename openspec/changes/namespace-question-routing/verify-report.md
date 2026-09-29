# Verify Report: namespace-question-routing — Full Change (Units 0-5 + Caller A/B)

- Change: namespace-question-routing
- Scope: Full change — routing dataset, pure-domain classifier, read-only profile
  builder, runtime router, SQLite cache/telemetry, evaluation/calibration, and the
  caller-facing integration (`route_for_caller` behind `router_enabled`).
- Mode: openspec
- Execution mode: interactive
- Verifier: orchestrator inline verification (native RDD blocked; issue #14 / upstream
  #4968; alternate path authorized by maintainer)
- Date: 2026-09-29

## Executive Summary

All Units 0-5 plus the caller integration (Units A/B) are implemented, committed on
`main` (`b8d6d22..0673c52`), pushed to `origin`, deployed to the production clone on the
Orange Pi, and smoke-verified read-only against the real environment. All project
quality gates pass; no blocking findings remain. Native RDD review is waived by the
documented alternate path (issue #14), while review evidence never grants delivery
authority.

## Quality Gates (final run, 2026-09-29)

| Gate | Command | Result |
|------|---------|--------|
| Lint | `uv run ruff check .` | PASSED — All checks passed! |
| Type check | `uv run mypy .` | PASSED — Success: no issues found in 367 source files |
| Architecture | `uv run python scripts/validate_architecture.py` | PASSED — Arquitectura Hexagonal validada correctamente |
| Tests | `uv run pytest -q -m "not neo4j_integration"` | PASSED — 1537 passed, 76 deselected, 2 pre-existing warnings |

The 76 deselected tests are the container-gated `neo4j_integration` suite (requires a
live Neo4j container; excluded from unit verification, consistent with prior units).

## Production operational window (Orange Pi, 2026-09-29) — read-only

Executed with explicit maintainer approval; no graph mutation (parameterized MATCH
reads and local artifact writes only).

1. Profile artifact built on the Pi: `data/router/namespace_profiles.json`, snapshot
   `pi-prod-2026-09-29`, 3 profiles, dim=384, mode 600.
2. Threshold calibration over the committed dataset with real profiles/embeddings
   (`scripts/calibrate_namespace_routing.py`): selected `min_top_score=0.10`,
   `min_margin=0.05` → single accuracy 0.727, wrong 0.0, abstention 0.452,
   multi containment 0.667 (initial defaults scored 0.227/0.806 — replaced).
3. Caller integration smoke (`build_routing_caller` + `route_for_caller` on the Pi,
   `uv run --no-sync`, purely local — catalog hints + profile artifact + local
   embeddings; no Neo4j connection):
   - flag off (default) → `route_for_caller` returns `None` (explicit-scope path kept);
   - telemetry enabled without key → `RoutingTelemetryError` (fail closed);
   - flag on → "¿qué es un agente arquitectónico?" → single 0.525
     `knowledge:agentic-architectural-patterns`; "how do graphrag community summaries
     relate to retrieval?" → single 0.677 `knowledge:essential-graphrag`;
     "¿cuánto dura un pipeline de indexación?" → abstain with fan-out
     `[knowledge:essential-graphrag, knowledge:graphrag-agentic]`.

## Spec Compliance Summary

### Namespace source of truth

- REQ-NS-01 — Catalog authority: hints derived from the versioned catalog
  (`hints_from_catalog`, moved to domain in `bff9b6c`); the router never invents a
  namespace.
- REQ-NS-02 — Scope validation: every candidate passes `CatalogScopeResolver` before
  any route is reported (`RouteQuestionUseCase._validate`).
- REQ-NS-03 — Fail closed: missing/inactive/unknown namespaces yield typed abstention
  (`low_score`/`low_margin`/`no_profiles`), never a graph query.

### Units

- Unit 0 — Dataset + validator: `namespace_routing_dataset.jsonl` (31 records,
  Spanish/English, single/multi/abstain/out-of-domain), MANIFEST hashes, validator
  script + tests.
- Unit 1 — Domain contracts: profiles, candidates, thresholds, decisions, cosine
  scoring, abstention policy; pure stdlib+pydantic.
- Unit 2 — Read-only profile builder: `NamespaceProfileSourcePort` +
  `Neo4jNamespaceProfileSource` (MATCH-only), centroid math, local JSON artifact.
- Unit 3 — Runtime router: `RouteQuestionUseCase` (lexical fast path + embedding
  fallback), top-2 `fanout_namespaces` only on `low_margin`, catalog validation.
- Unit 4 — SQLite cache/telemetry: `RoutingTelemetryPort` separate from
  `QueryLoggerPort`; raw storage opt-in and off by default; correction labels gate
  golden eligibility.
- Unit 5 — Integration/evaluation/rollout: `RoutingMetrics` + `evaluate_router`,
  `scripts/evaluate_namespace_routing.py`, `scripts/route_question.py`, feature flags
  `router_enabled`/`router_telemetry_enabled` (default off keeps explicit scope).
- Caller A/B — `infrastructure/routing_caller.py`: single composition root
  (`build_route_question_use_case`), `RoutingCaller.route_for_caller` (`None` = flag
  off; abstain never conflated), `RoutingTelemetryError` fail-closed keyed HMAC
  fingerprints, `docs/ops/namespace-routing-caller-integration.md`.

## Deferred / not applicable

- 5.6: N/A — the MCP contract and exposure surface are unchanged (external proposer);
  no Phase 6/7 evidence update required.
- Native RDD re-enable: pending upstream fix of issue #14 / #4968.