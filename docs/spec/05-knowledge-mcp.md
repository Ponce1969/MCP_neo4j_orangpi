# 05 — Knowledge MCP

> **Status: Target (extends existing).** 8 tools already exist, including `query_cypher`.
> This spec defines the target logical architecture, tool surface, and enforcement.

## 1. Current state `[VERIFIED]`

- `mcp_server_main.py` composes the server; `infrastructure/mcp/mcp_server_adapter.py`
  registers **8 tools** on a `FastMCP("book-graph-rag")` instance:
  `find_entity`, `traverse_relationships`, `search_chunks`, `list_entities`,
  `count_entities`, `search_rag`, `query_cypher`, `ask_global`.
- The server binds `settings.mcp_bind_host`, whose default is loopback `127.0.0.1`
  (fail-closed); `app_env == "production"` rejects wildcard/public binds
  (`0.0.0.0`/`::`/`""`) at startup. `[VERIFIED]` `config.py::Settings.mcp_bind_host`
  + `_validate_bind_host_is_private_in_production`, wired through
  `mcp_server_main.py::_run_server`.
- `query_cypher` is **disabled by default** (`mcp_enable_query_cypher=False`); when
  disabled it returns a typed `policy_violation` without contacting the LLM or graph.
  `[VERIFIED]` `mcp_server_adapter.py::query_cypher`.
- Dynamic Cypher is governed by a **structural allowlist validator** (a restricted
  tokenizer + recursive-descent grammar), not a pure keyword denylist: only the
  read-only subset (`MATCH`/`OPTIONAL MATCH`/`WITH`/`RETURN`/`ORDER BY`/`LIMIT`)
  is accepted, and `EXPLAIN` is mandatory before execution. `[VERIFIED]`
  `structural_cypher_policy.py`, `text2cypher_adapter.py`.
- Namespace scoping is enforced server-side: a catalog-backed `ScopeResolver`
  validates `source_id` and propagates a `ScopeContext` through all 8 structured
  tools; scope is required and cross-namespace traversal is blocked. `[VERIFIED]`
  `catalog_scope_resolver.py`, `neo4j_query_adapter.py::_build_scope_clause`.
- Per-tier concurrency/rate budgets and hard limits (max rows/nodes, traversal
  depth, timeout) fail with typed errors, not silent truncation. `[VERIFIED]`
  `mcp_resource_budget_adapter.py`, `neo4j_query_adapter.py`.
- Query logging is **metadata-only**: raw query/prompt/error text never enters the
  JSONL; requests are correlated via keyed HMAC-SHA256 fingerprints. `[VERIFIED]`
  `mcp_server_adapter.py::_log`, `domain/models.py::QueryLogEntry`.
- `ask_global` returns cited community-summary answers via `GlobalQueryUseCase`;
  the community read path is not yet namespace-filtered (see 07 R7).

## 2. Target logical architecture `[TARGET]`

Two separable concerns; keep them decoupled:

1. **Modular agents** — domain-level reasoning that composes graph primitives to answer
   a task (e.g. "design a multi-agent architecture using patterns from the book"). These
   consume the Knowledge MCP, not a monolithic endpoint.
2. **Topic-filtered Knowledge MCP** — a read-oriented surface exposing **outcome-oriented
   tools** over namespaced graph data.

"Modular" here is **logical**, not physical: one read-only MCP process, topic scoping via
namespaces (02) enforced server-side. See 00 §8 for the closed decision.

The MCP MUST remain **read-only** at the tool surface: it answers questions and returns
evidence; it does not ingest, resolve, audit-mutate, or expose write/admin operations.
Writes belong to the CLI (`book-graph-rag index|audit|validate`) and scripts.

## 3. Outcome-oriented tools `[TARGET]`

Tools are named for what the caller wants to accomplish, not for the underlying Cypher.

| Outcome tool | Replaces / builds on (current) | Returns |
|--------------|--------------------------------|---------|
| `find_concept` | `find_entity` | Matching entities + provenance |
| `explore_related` | `traverse_relationships` | Neighborhood + relationship evidence |
| `search_book` | `search_chunks` | Chunk hits + chapter/section provenance |
| `list_concepts` | `list_entities` | Paginated entities (namespace-scoped) |
| `count_concepts` | `count_entities` | Counts (namespace-scoped) |
| `retrieve_evidence` | `search_rag` | Chunks + entities + relations with citations |
| `answer_global` | `ask_global` | Cited community-summary answer |
| `query_graph` | `query_cypher` | **Gated** read-only Cypher results (see §6) |

## 4. Compatibility strategy from the current 8 tools `[TARGET]`

- Do **not** break existing consumers during transition. Keep current tool names working
  while new outcome-oriented names are introduced, or provide thin aliases, until a
  deprecation window closes.
- Any rename/alias MUST preserve behavior and logging so `QueryLogEntry` continuity is
  maintained.
- `query_cypher` is **not** deprecated outright; it is gated (see §6). Its replacement
  `query_graph` is the same capability with explicit enforcement and evidence.

## 5. Evidence and provenance in responses `[TARGET]`

- Every tool response MUST include provenance where it exists: source id, chunk/page,
  and (after 01) version dimensions. Current `EntityWithContext.source` already carries
  `book_id`/`chunk_index`; `[VERIFIED]` `neo4j_query_adapter.py::_format_source`.
- `answer_global` MUST cite its source summaries (it already does via
  `[Data: CommunitySummary(id)]`; `[VERIFIED]` `llm_adapter.py` compose prompt).
- Tools MUST not fabricate provenance; when a source is unknown, return `null`/absent,
  not a guess.

## 6. Server-side topic/namespace enforcement `[TARGET]`

- The MCP MUST enforce namespace scoping server-side (02). A tool parameter specifying a
  namespace is validated against the catalog and applied in Cypher; a caller cannot
  bypass it by phrasing or by `query_cypher`.
- Topic filtering is enforced by the server, not by trusting the agent's natural
  language. If a topic maps to a namespace/domain, the mapping is explicit and
  server-checked.

## 7. Read-only Cypher policy `[TARGET]`

- `query_cypher` / `query_graph` remains a **last-resort, gated** capability. Prefer
  parameterized outcome tools; use free-form Cypher only when no tool covers the need.
- `query_cypher` is **disabled by default** (`mcp_enable_query_cypher=False`); it must
  be explicitly enabled to reach the dynamic path. `[VERIFIED]` `config.py`.
- Enforcement is defense-in-depth, applied server-side in this order:
  1. **Structural allowlist** (the security decision, replacing the pure keyword
     denylist): a restricted tokenizer + recursive-descent grammar accepts only the
     read-only subset — `MATCH`/`OPTIONAL MATCH`/`WITH`/`RETURN`/`ORDER BY`/`LIMIT`,
     literal labels/relationship types, bound-parameter property maps, bounded
     relationship depths, and a fixed read-function allowlist. Write clauses
     (`CREATE`/`SET`/`MERGE`/`DETACH`/`REMOVE`/`DELETE`/`DROP`), `CALL`, `LOAD CSV`,
     `FOREACH`, `UNWIND`, subqueries, and literal `WHERE` values fail closed.
     `[VERIFIED]` `structural_cypher_policy.py`. A keyword denylist is retained only
     as a diagnostic prefilter, not the security decision.
  2. **`EXPLAIN` validation** before execution (mandatory). `[VERIFIED]`
     `text2cypher_adapter.py::_generate_and_validate`.
  3. **Read-only transaction/session** (`READ_ACCESS` routing + managed
     `execute_read`) so even a missed write cannot commit. `[VERIFIED]`
     `neo4j_query_adapter.py::_read_session`/`_read_records`.
  4. **LIMIT** and **timeout** enforcement (see §8).
- A query failing any layer is rejected with a typed error and logged, never silently
  executed. Residual: Text2Cypher does not yet bind scope parameters end-to-end
  (see 07 R8).

## 8. Limits and timeouts `[TARGET]`

- Every tool enforces a result `LIMIT` (current defaults: entity 100, traverse depth
  ≤3, `LIMIT 100`). `[VERIFIED]` `neo4j_query_adapter.py` and
  `text2cypher_adapter.py` prompt.
- Every read enforces a hard timeout (read adapter uses 3s; `text2cypher_timeout`
  default 10s). `[VERIFIED]` `neo4j_query_adapter.py::_run_with_timeout`, `config.py`.
- Exhaustion fails with **typed errors**, not silent truncation:
  `ResourceExhaustedError`, `TraversalDepthExceededError`, `QueryTimeoutError`,
  `ConcurrencyLimitExceededError`, `RateLimitExceededError`. `[VERIFIED]`
  `domain/mcp_security.py`, `mcp_resource_budget_adapter.py`.
- Every tool runs under a **per-tier budget** (LOW/MEDIUM/HIGH concurrency + rate
  limits) plus hard row/node/traversal caps. `[VERIFIED]`
  `domain/tool_tier_registry.py`, `mcp_resource_budget_adapter.py`, `config.py`.
- Pagination is cursor-based (`list_entities`). `[VERIFIED]`.
- Target: make limits/timeouts explicit tool parameters with documented defaults and
  hard ceilings, and reject requests exceeding ceilings (e.g. batch size).

## 9. Auth and network assumptions `[TARGET]`

- The MCP MUST NOT bind to `0.0.0.0` on an untrusted network in production. The
  default bind is now loopback `127.0.0.1` (fail-closed); `app_env == "production"`
  rejects wildcard/public binds at startup. `[VERIFIED]` `config.py::Settings.mcp_bind_host`
  + `_validate_bind_host_is_private_in_production`. The production systemd unit binds
  the private Tailscale IP (`deploy/mcp-server.service`).
- Target exposure assumes a **trusted, private transport** (e.g. Tailscale-only) and
  (where required) authentication/TLS. See 07 for the full exposure gate and preconditions.
- Consumption remains **opt-in per session** (`AGENTS.md` §6); the server does not
  self-enable on agent runtimes.

## 10. Separate physical MCP criteria `[TARGET]`

"Physical" deployment (separate process/host, resource limits, restart policy,
secret injection, observability) is governed by 07 and `deploy/`, **not** by this
logical spec. This spec only defines the logical tool surface and enforcement; 07 defines
when it is safe to physically expose it.

## 11. Acceptance criteria

- [x] All 8 current tools remain callable (no rename/alias transition landed this
  phase; outcome-oriented names remain target — §3).
- [ ] Outcome-oriented tools return evidence/provenance in every response (target;
  current tools already return provenance where it exists — §5).
- [x] Namespace/topic filtering is enforced server-side and cannot be bypassed via
  `query_cypher` (scope required, `ScopeContext` propagated through all structured
  tools, `query_cypher` disabled by default; residual Text2Cypher param gap in 07 R8).
- [x] The read-only Cypher path blocks all write/admin vectors — via the structural
  allowlist (write clauses, `CALL`, `LOAD CSV`, `FOREACH`, `UNWIND`, subqueries all
  fail closed) plus `READ_ACCESS` + managed `execute_read`, not a keyword denylist.
- [x] Limits/timeouts are enforced with typed errors, not silent truncation.
- [x] The server does not bind to `0.0.0.0` in production (loopback default
  fail-closed + production wildcard rejection).

## 12. Tests

- **Unit:** tool contract shape; namespace parameter validation; denylist coverage
  (each blocked vector); timeout/limit enforcement.
- **Integration (Neo4j):** each tool against a seeded graph; provenance present; a
  write-vector `query_cypher` attempt is rejected and leaves the graph unchanged.
- **Compatibility:** existing tool names return the same shapes before/after transition.

## 13. Open decisions

- `[OPEN]` Deprecation timeline/window for the current tool names (Phase 6).
- **Decided (Phase 6):** `query_cypher` is **disabled by default**
  (`mcp_enable_query_cypher=False`); enabling it is an explicit opt-in.
- **Decided (2026-09-02):** the topic→namespace mapping is the namespace catalog from 02,
  stored in a versioned config file. Single-process MCP confirmed (see 00 §8);
  "modular" is logical (namespaces + server-side scoping), not N physical servers.
