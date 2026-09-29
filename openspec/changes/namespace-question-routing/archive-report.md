# Archive Report: namespace-question-routing

- Change: namespace-question-routing
- Status: COMPLETED
- Date: 2026-09-29
- Artifact store: openspec
- Delivery strategy: chained work-unit commits on `main` (direct-to-main, maintainer
  decision; native RDD waived via alternate path, issue #14)

## Summary

`namespace-question-routing` adds a local, deterministic classifier that proposes the
correct book namespace before GraphRAG retrieval: catalog-derived lexical hints plus
local embedding centroids over a read-only profile artifact, with calibrated
abstention and top-2 fan-out. It preserves the catalog-backed, fail-closed MCP scope
boundary, never issues an unscoped graph query, and keeps raw question/answer storage
out of the MCP metadata-only log.

## Outcome

- All functional requirements from `spec.md` (REQ-NS-01/02/03 and Units 0-5) are
  implemented; Unit 5.5 (production read-only smoke/evaluation) executed in an
  approved operational window on the Orange Pi; 5.6 recorded N/A (MCP surface
  unchanged).
- The caller integration (Units A/B) is delivered: reusable `routing_caller.py`
  composition root and `route_for_caller`, keyed HMAC telemetry with fail-closed
  guard, integration docs.
- All quality gates pass (ruff, mypy 367 files, validate_architecture, 1537 filtered
  tests) and the read-only smoke on the Orange Pi is green.
- The change is ARCHIVED. Verification evidence in `verify-report.md`.

## What Was Implemented

### Unit 0 — Baseline and dataset
- `proposal.md`, `design.md`, `spec.md`, `tasks.md` (`b8d6d22`, `ee0f676`).
- `data/evaluation/namespace_routing_dataset.jsonl` (31 records, ES/EN) +
  `scripts/validate_namespace_routing_dataset.py` + MANIFEST/README updates
  (`599f958`).

### Unit 1 — Domain contracts and classifier
- Pure-domain profiles, thresholds, decisions, cosine scoring, abstention policy
  (`8bf612d`).

### Unit 2 — Read-only profile builder
- `NamespaceProfileSourcePort` + MATCH-only `Neo4jNamespaceProfileSource`, centroid
  math, local JSON profile store, dry-run CLI (`82ca3aa`, `1f34f3b`, `59ccb0d`).

### Unit 3 — Runtime routing and fallback
- `RouteQuestionUseCase` with lexical fast path + embedding fallback, catalog
  validation, top-2 fan-out on `low_margin` only (`556d180`, `e9229e4`).

### Unit 4 — SQLite cache and telemetry
- `RoutingTelemetryPort` separate from `QueryLoggerPort`; WAL/busy-timeout adapter,
  bounded retention, correction labels gating golden eligibility, raw storage opt-in
  and off by default (`0b0a205`, `8b2634f`).

### Unit 5 — Integration, evaluation, and rollout
- `RoutingMetrics` + deterministic `evaluate_router`, evaluation CLI, routing CLI
  (`scripts/route_question.py`), feature flags `router_enabled` /
  `router_telemetry_enabled` (default off keeps the explicit-scope path)
  (`adb28f8`, `3c957d3`, `8693400`).

### Orange Pi operational window (2026-09-29)
- Profile artifact `data/router/namespace_profiles.json` (snapshot
  `pi-prod-2026-09-29`); production bugs found and fixed by the dry-run (`ddf77a0`
  driver/summary fix, `bff9b6c` script-import fix); threshold calibrator (`9333720`)
  and calibrated defaults adopted (`77b171e`).

### Caller integration (Units A/B, 2026-09-29)
- `infrastructure/routing_caller.py`: `RouteExecutor` protocol,
  `build_route_question_use_case` (single composition root, consumed by the routing
  CLI), `RoutingCaller.route_for_caller` (`None` = flag off; abstain never conflated),
  `RoutingTelemetryError` fail-closed keyed HMAC fingerprints,
  `docs/ops/namespace-routing-caller-integration.md` (`10cebbc`, `0673c52`).

## Production deployment

- `git push origin main` (`77b171e..0673c52`) and `git pull --ff-only` on the Orange Pi
  production clone — `main == origin == 0673c52` (verified).

## Final Quality Gates

| Gate | Command | Result |
|------|---------|--------|
| Lint | `uv run ruff check .` | PASSED |
| Type check | `uv run mypy .` | PASSED — 367 source files |
| Architecture | `uv run python scripts/validate_architecture.py` | PASSED |
| Tests | `uv run pytest -q -m "not neo4j_integration"` | PASSED — 1537 passed, 76 deselected |

## Non-goals (carried forward)

- Per-chapter profiles to raise routing coverage (optional improvement).
- Phase 6/7 MCP exposure evidence — not applicable to this change (MCP surface
  unchanged); tracked in the roadmap as the next phase.