# Proposal: Namespace Question Routing

## Decision

Add a deterministic, local namespace router that selects the relevant book before a
question reaches the GraphRAG retrieval path. The router combines catalog-aware lexical
hints with local embedding centroids and abstains when confidence is insufficient.

This change is intentionally additive and read-only in its first delivery. It MUST NOT
mutate the production Neo4j graph, weaken MCP scope enforcement, or persist raw query
text in the existing metadata-only MCP log.

## Problem

The production graph contains three active book namespaces, but the MCP currently
expects the caller to provide a validated `source_id`. There is no reusable component
that infers the source from a natural-language question. Sending every question to an
LLM orchestrator adds cost and latency; forcing every question into one book risks
retrieving evidence from the wrong namespace.

## Goals

- Route clear questions to one validated `SourceNamespace` without an orchestrator LLM.
- Use the existing local embedding provider and namespaced graph data.
- Detect ambiguity and cross-book questions instead of forcing a wrong top-1 result.
- Preserve `CatalogScopeResolver`, `ScopeContext`, and MCP `require_scope` as the
  security boundary.
- Establish a versioned routing dataset and baseline before runtime integration.
- Add opt-in SQLite cache/telemetry as a separate adapter, not as a replacement for
  metadata-only MCP logging.
- Keep every implementation slice small, reversible, and reviewable under ODD/RDD.

## Non-goals

- No LLM-based routing.
- No fine-tuning in this change.
- No automatic changes to Neo4j data, indexes, communities, or entity resolution.
- No removal of fail-closed MCP scope requirements.
- No assumption that every stored answer is a golden answer.
- No broad redesign of the existing MCP tool surface.

## Delivery boundary

Unit 0 is the first apply slice. It creates the OpenSpec artifacts, a curated routing
dataset, and a production baseline note. It does not call the Orange Pi, change the
service, or write to Neo4j.

Units 1-5 are planned target slices:

1. domain routing contracts and pure classifier;
2. read-only profile building and local centroid artifact;
3. application routing with calibrated abstention/fallback;
4. separate opt-in SQLite cache and telemetry;
5. Gentle-AI integration, evaluation, and guarded rollout.

## Acceptance criteria for the change

- A question with a clear lexical or semantic signal produces the expected namespace.
- Low score or low top-1/top-2 margin produces an explicit abstention.
- Cross-book questions are represented explicitly and never silently become unscoped.
- All selected namespaces are validated through the existing catalog/scope path.
- Routing artifacts record model, dimension, catalog version, profile version, and graph
  snapshot metadata.
- SQLite telemetry is separately configurable, bounded, and does not alter `QueryLogEntry`.
- Routing quality is measured independently from retrieval and generation quality.
- The initial production rollout is read-only and reversible.

## Risks and mitigations

| Risk | Mitigation |
|------|------------|
| Similar books produce close centroid scores | Use top-1 score plus margin; abstain and support top-2/fan-out. |
| A stale centroid returns an old route | Bind profiles to model, catalog, and graph snapshot metadata. |
| Router bypasses MCP authorization | Return a validated namespace; keep MCP scope resolution mandatory. |
| Raw user data leaks through telemetry | Separate adapter, opt-in raw fields, retention, local permissions, and HMAC metadata. |
| Review scope grows beyond one candidate | Five work units, tests with behavior, and a 400-line slice budget. |

## Rollback

Unit 0 rollback is deletion/reversion of documentation, dataset, and manifest entries.
Future runtime rollback is disabling the router integration and retaining the existing
explicit-scope MCP path. SQLite rollback is disabling its adapter and preserving the
MCP JSONL logger unchanged.

## Next step

Complete Unit 0 verification, then review and approve the domain contracts before any
profile-generation or runtime code is written.
