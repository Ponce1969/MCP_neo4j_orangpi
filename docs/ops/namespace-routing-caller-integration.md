# Namespace Routing — Caller Integration (Units A/B)

## Purpose

Document the caller-facing surface of the namespace router: the reusable
composition factory, the `route_for_caller` contract, and the opt-in keyed
telemetry. This is the boundary a future external proposer uses to suggest a
namespace; it is **not** an authorization mechanism and it never changes the
MCP surface.

## Caller contract

One entry point, one meaning for `None`:

```python
from book_graph_rag.config import Settings
from book_graph_rag.infrastructure.routing_caller import build_routing_caller

caller = build_routing_caller(Settings())          # e.g. from env
route = await caller.route_for_caller(question)    # -> ResolvedRoute | None
await caller.close()
```

| Result | Meaning | Caller action |
|--------|---------|---------------|
| `None` | `router_enabled` is **off** | Keep the existing explicit-scope path; the router is not invoked. |
| `ResolvedRoute` with `validated_namespace is None` | Router **abstains** (low score / low margin / no profiles) | Do not force a scope; treat the question as unrouted. |
| `ResolvedRoute` with `validated_namespace set` | `single` route | Use the validated namespace. |
| `ResolvedRoute` with `fanout_namespaces` (≤ 2) | `multi`/ambiguous fan-out | Route to the listed namespaces only. |

Two outcomes are never conflated: `None` means flag-off, an abstaining
`ResolvedRoute` means "no safe namespace". The route is a **proposal**: it
still must pass the catalog backed scope resolver, and MCP `require_scope`
remains the security gate. The router on its own never issues a graph query.

## Composition

- `build_route_question_use_case(settings)` — the single composition root for
  the router's concrete adapters (catalog resolver, JSON profile artifact,
  local embeddings, catalog-derived lexical hints). `scripts/route_question.py`
  consumes it, so there is exactly one wiring to change.
- `build_routing_caller(settings)` — composes the use case, `router_enabled`,
  and (when enabled) the SQLite telemetry adapter.

The `route_for_caller` executor slot is a structural `RouteExecutor` protocol
satisfied by `RouteQuestionUseCase`; embedding models load lazily, so building
the caller does not touch the model cache, Neo4j, or any profile reader until
a route is actually requested.

## Telemetry contract

Routing telemetry is separate from the MCP query log and opt-in:

- `router_telemetry_enabled` enables recording; events go to
  `routing_telemetry_path` (SQLite, bounded retention).
- Every event fingerprint is a **keyed HMAC-SHA256** of the question
  (`router_telemetry_hmac_key` + `router_telemetry_hmac_key_id`). The raw
  question is never stored and never hashed plainly.
- **Fail closed:** with telemetry enabled and an empty key, composition raises
  `RoutingTelemetryError` instead of recording unkeyed fingerprints. The same
  guard protects `build_routing_event` for direct builds.

| Setting | Default | Meaning |
|---------|---------|---------|
| `router_enabled` | `false` | Off ⇒ `route_for_caller` returns `None`; callers keep the explicit-scope path. |
| `router_telemetry_enabled` | `false` | Opt-in event recording to the bounded SQLite store. |
| `router_telemetry_hmac_key` | empty | Keyed HMAC secret; empty ⇒ fail closed when telemetry is enabled. |
| `router_telemetry_hmac_key_id` | `router-telemetry-v1` | Key identifier for rotation/audit. |
| `routing_telemetry_path` | `data/router/router_telemetry.db` | Event store path. |
| `namespace_profile_store_path` | `data/router/namespace_profiles.json` | Read-only centroid artifact, never in Neo4j. |

## Safety boundary

- Read-only: no SSH, no Neo4j write, no container change, no MCP config
  change, no raw question/answer added to the MCP log.
- The MCP server, its `require_scope` enforcement, and its query logging are
  untouched by this integration; the proposer is external by design.
- Profile data lives in a local artifact; storing derived profiles in Neo4j
  would require the project's backup → dry-run → human approval → apply →
  audit protocol.

## Out of scope (this document)

- The Orange Pi smoke run (pull + read-only route check against the live graph)
  is an approved operational window, not part of the code slice.
- Golden promotion of telemetry events remains gated on explicit
  correction labels (`is_golden_eligible`).

## Verification record

| Command | Result |
|---------|--------|
| `uv run pytest tests/unit/test_routing_caller.py tests/unit/test_routing_domain.py -q` | PASS — 39 tests (flag-off short-circuit, abstain ≠ `None`, keyed fingerprint determinism/rotation, fail-closed telemetry). |
| `uv run ruff check .` | PASS. |
| `uv run mypy .` | PASS. |
| `uv run python scripts/validate_architecture.py` | PASS. |
| `uv run pytest -q -m "not neo4j_integration"` | PASS — 1532 tests. |