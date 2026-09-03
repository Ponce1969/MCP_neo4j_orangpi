# 05 — Knowledge MCP

> **Status: Target (extends existing).** 8 tools already exist, including `query_cypher`.
> This spec defines the target logical architecture, tool surface, and enforcement.

## 1. Current state `[VERIFIED]`

- `mcp_server_main.py` composes the server; `infrastructure/mcp/mcp_server_adapter.py`
  registers **8 tools** on a `FastMCP("book-graph-rag")` instance:
  `find_entity`, `traverse_relationships`, `search_chunks`, `list_entities`,
  `count_entities`, `search_rag`, `query_cypher`, `ask_global`.
- Server runs over SSE with `host="0.0.0.0"`, `port=8003`. `[VERIFIED]`
  `mcp_server_main.py::_run_server` and `mcp_server_adapter.py::run_sse`.
- `query_cypher` is a free-form natural-language → Cypher path via
  `infrastructure/text2cypher_adapter.py` (schema inference → read-only guard → EXPLAIN →
  execute). This is the highest-risk tool (see 07).
- `ask_global` returns cited community-summary answers via `GlobalQueryUseCase`.
- Every tool logs a `QueryLogEntry` to a rotating JSONL (`json_query_logger_adapter.py`).

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
- Enforcement is defense-in-depth, applied server-side in this order:
  1. **Keyword denylist** (reject write/admin tokens). Current denylist is incomplete
     (07); the target must close it (add `FOREACH`, `LOAD CSV`, `CALL db.*`/`apoc.*`
     write procedures, and any `MERGE`/`CREATE` reachable via `UNWIND`, subqueries, or
     procedure calls).
  2. **`EXPLAIN` validation** (already done) before execution.
  3. **Read-only transaction/session** so even a missed keyword cannot commit a write.
  4. **LIMIT** and **timeout** enforcement (see §8).
- A query failing any layer is rejected with a typed error and logged, never silently
  executed.

## 8. Limits and timeouts `[TARGET]`

- Every tool enforces a result `LIMIT` (current defaults: entity 100, traverse depth
  ≤3, `LIMIT 100`). `[VERIFIED]` `neo4j_query_adapter.py` and
  `text2cypher_adapter.py` prompt.
- Every read enforces a hard timeout (current read adapter uses 3s;
  `text2cypher_timeout` default 10s). `[VERIFIED]` `neo4j_query_adapter.py::_run_with_timeout`,
  `config.py`.
- Pagination is cursor-based (`list_entities`). `[VERIFIED]`.
- Target: make limits/timeouts explicit tool parameters with documented defaults and
  hard ceilings, and reject requests exceeding ceilings (e.g. batch size).

## 9. Auth and network assumptions `[TARGET]`

- The MCP MUST NOT bind to `0.0.0.0` on an untrusted network in production. Current
  default is `0.0.0.0` (plaintext SSE). `[VERIFIED]` `mcp_server_main.py`.
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

- [ ] All 8 current tools remain callable (directly or via aliases) during transition.
- [ ] Outcome-oriented tools return evidence/provenance in every response.
- [ ] Namespace/topic filtering is enforced server-side and cannot be bypassed via
  `query_cypher`.
- [ ] The read-only Cypher denylist blocks all write/admin vectors (including
  `FOREACH`, `LOAD CSV`, write procedures).
- [ ] Limits/timeouts are enforced with typed errors, not silent truncation.
- [ ] The server does not bind to `0.0.0.0` in production without passing the 07
  exposure gate.

## 12. Tests

- **Unit:** tool contract shape; namespace parameter validation; denylist coverage
  (each blocked vector); timeout/limit enforcement.
- **Integration (Neo4j):** each tool against a seeded graph; provenance present; a
  write-vector `query_cypher` attempt is rejected and leaves the graph unchanged.
- **Compatibility:** existing tool names return the same shapes before/after transition.

## 13. Open decisions

- `[OPEN]` Deprecation timeline/window for the current tool names (Phase 6).
- `[OPEN]` Whether `query_cypher` is hidden, rate-limited, or permission-gated by default
  (Phase 6).
- **Decided (2026-09-02):** the topic→namespace mapping is the namespace catalog from 02,
  stored in a versioned config file. Single-process MCP confirmed (see 00 §8);
  "modular" is logical (namespaces + server-side scoping), not N physical servers.
