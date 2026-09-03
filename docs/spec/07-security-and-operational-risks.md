# 07 — Security and Operational Risks

> **Status: Active (normative).** This risk register binds before any production
> exposure. Findings are `[VERIFIED]` against the current code/config and are
> proposals-for-mitigation, not claims that mitigations already exist.

## 1. Scope and method

This document covers the application surface of `book-graph-rag`: the MCP server, the
CLI, the Neo4j connection, Text2Cypher, and logging. It does **not** cover the Orange Pi
host itself, other projects' containers, or the OS — those are out of scope and governed
by `AGENTS.md` §7. Severity uses: **CRITICAL / HIGH / MEDIUM / LOW**. "Residual risk" is
what remains after the listed mitigations; a non-empty residual risk means the exposure
gate (00 §3) is not yet passable.

## 2. Threat model (summary)

| # | Asset / flow | Trust boundary | Threat |
|---|--------------|----------------|--------|
| T1 | MCP SSE endpoint | untrusted network → server | Unauthenticated read access to the graph over plaintext |
| T2 | Neo4j credentials | app process → DB | Single read/write credential reused across adapters; lateral write risk |
| T3 | Text2Cypher `query_cypher` | LLM-generated string → DB | Write/admin statement escaping the denylist |
| T4 | Schema description fed to LLM | hardcoded fallback → LLM | Schema drift → invalid/queries or wrong `EXPLAIN` assumptions |
| T5 | Logs (dead-letter + query log) | app → disk | Raw query text / errors persist, may contain sensitive content |
| T6 | Prompt injection via natural language | user/agent input → LLM | `query_cypher`/`ask_global` prompts manipulated |

## 3. Risk register

### R1 — Network exposure: plaintext SSE on `0.0.0.0` (CRITICAL)

- **Evidence `[VERIFIED]`:** `mcp_server_main.py::_run_server` calls
  `server_adapter.run_sse(host="0.0.0.0", port=settings.mcp_port)`; `mcp_port` default
  `8003` (`config.py`). SSE is HTTP without TLS in this path.
- **Impact:** Any host that can reach port 8003 can invoke all 8 tools and read graph
  contents without authentication.
- **Mitigation (target):** bind to loopback/private interface (e.g. Tailscale IP) in
  production; require TLS and/or an authenticated transport; keep consumption opt-in
  (`AGENTS.md` §6). Never bind `0.0.0.0` on an untrusted network.
- **Residual risk:** LOW once bound to a private authenticated network; HIGH until then.

### R2 — Shared Neo4j read/write credentials (HIGH)

- **Evidence `[VERIFIED]`:** `Neo4jCommandAdapter`, `Neo4jQueryAdapter`,
  `Neo4jAuditAdapter`, `Neo4jValidationAdapter` all construct a driver from the same
  `settings.neo4j_user` / `settings.neo4j_password` (`config.py`). The MCP read path uses
  a credential that also permits writes.
- **Impact:** A compromise of the read-only MCP (or a write leaked through R3) can mutate
  or delete the graph.
- **Mitigation (target):** least-privilege — separate read-only Neo4j role/credential for
  the MCP/query path; write credential confined to the CLI index/validate path. Enable the
  read-only transaction/session for the query surface.
- **Residual risk:** MEDIUM until read-only credentials are enforced server-side.

### R3 — Incomplete Text2Cypher denylist (HIGH)

- **Evidence `[VERIFIED]`:** `text2cypher_adapter.py::_WRITE_KEYWORDS_RE` blocks
  `CREATE|DELETE|SET|MERGE|DETACH|REMOVE|DROP` and `CALL dbms`, but **not** `FOREACH`,
  `LOAD CSV`, `CALL db.*` write/admin procedures, `apoc.*` write procedures, or
  write keywords reachable via `UNWIND`, subqueries (`CALL { ... }`), or procedure
  arguments.
- **Impact:** A generated (or injected) query could execute a write/admin operation the
  denylist misses.
- **Mitigation (target):** defense-in-depth — (a) close the denylist; (b) keep `EXPLAIN`
  validation; (c) enforce a read-only session/transaction so even a missed keyword cannot
  commit; (d) `LIMIT` + timeout (05 §8).
- **Residual risk:** MEDIUM until the read-only session is enforced at the DB layer
  (denylist alone is not sufficient).

### R4 — Hardcoded schema drift (MEDIUM)

- **Evidence `[VERIFIED]`:** `text2cypher_adapter.py::_HARDCODED_SCHEMA` lists only
  `Entity`/`Chunk` and a `RELATED` edge. When `apoc.meta.data()` is unavailable it falls
  back to this stale schema, so the LLM generates against a schema that may not match the
  actual graph.
- **Impact:** Invalid Cypher, wrong field assumptions, and silent degradation of
  `query_cypher`.
- **Mitigation (target):** keep the schema source dynamic (APOC) and, when unavailable,
  derive a minimal schema from the catalog (02) or a committed schema manifest rather than
  a hand-maintained constant; mark `schema_source` in results (already surfaced in
  `Text2CypherResult.schema_source`).
- **Residual risk:** LOW once schema is catalog-derived and its source is always surfaced.

### R5 — Raw free-text logging (MEDIUM)

- **Evidence `[VERIFIED]`:** `index_book_use_case.py::_write_dead_letter` writes
  `error_message` (`str(error)`); MCP tool wrappers log `error=str(exc)` into
  `QueryLogEntry.error` (`mcp_server_adapter.py`); `text2cypher` failure contexts carry
  the generated Cypher string. Query log stores `query_params` verbatim
  (`json_query_logger_adapter.py`).
- **Impact:** Logs may persist query text, generated Cypher, or exception content that is
  sensitive; retention is bounded (7 days) but not sanitized.
- **Mitigation (target):** redact secrets and raw query text from structured logs before
  write (reuse the audit module's `safe_properties`/redaction patterns); log error *type*
  + a bounded, sanitized message; keep generated Cypher out of the default log or mask it.
- **Residual risk:** LOW once structured logs are redacted; MEDIUM for the dead-letter
  error messages until bounded/sanitized.

### R6 — Prompt injection via natural-language tools (MEDIUM)

- **Evidence `[VERIFIED]`:** `query_cypher` and `ask_global` accept free-text questions
  and feed them to an LLM; `query_cypher` then executes generated Cypher.
- **Impact:** A crafted prompt could steer Cypher generation toward disallowed operations
  or exfiltrate data via an overly broad read.
- **Mitigation (target):** rely on R3's server-side read-only enforcement (not the LLM's
  compliance); scope queries to the caller's namespace (05 §6); apply result `LIMIT`s and
  timeouts; treat natural language as untrusted input.
- **Residual risk:** LOW once read-only enforcement + namespace scoping + limits are in
  place; MEDIUM before.

## 4. Operational controls `[TARGET]`

- **Secrets:** never in code, docs, or logs; `.env` gitignored; `SecretStr` at rest
  (`[VERIFIED]` `config.py`). Inject via environment/secret manager in production.
- **Least privilege:** read-only Neo4j credential for the query/MCP surface; write
  credential for CLI ingest/validate.
- **Network:** private transport (Tailscale-only), no `0.0.0.0` on untrusted networks,
  TLS/auth where required.
- **Observability:** structured, redacted logs with bounded retention; query provenance
  (tool, duration, result count, error type) retained for gap analysis.
- **Change control:** exposure/network/auth changes pass the exposure gate (00 §3) and
  require human approval.

## 5. Preconditions before production exposure `[TARGET]`

None of the following are `[VERIFIED]` as complete today; each must be verified before the
MCP is exposed beyond a trusted dev loop:

1. R1 mitigated: private/authenticated transport, no `0.0.0.0` bind on untrusted network.
2. R2 mitigated: read-only credential + read-only session enforced for the MCP path.
3. R3 mitigated: read-only enforcement proven at the DB layer (not denylist-only).
4. R4 mitigated: schema source is dynamic/catalog-derived and surfaced in responses.
5. R5 mitigated: structured logs redacted; raw query text masked.
6. Readiness gate (06) passes; audit (04) shows no BLOCKING, no FAILED/UNREACHABLE.
7. Explicit human approval recorded; production graph untouched by validation.

## 6. Acceptance criteria

- [ ] Every finding above is either mitigated (with evidence) or explicitly accepted with
  rationale and a named owner.
- [ ] The MCP path cannot reach a write credential or execute a write statement.
- [ ] Logs contain no raw secrets or unbounded raw query text.
- [ ] The exposure gate cannot pass while any CRITICAL residual risk remains.

## 7. Open decisions

- `[OPEN]` Whether to introduce per-role Neo4j credentials in dev first or only for
  production.
- `[OPEN]` Transport choice (TLS vs. Tailscale-only vs. both) for the first real exposure.
- `[OPEN]` Logging redaction policy specifics (mask vs. drop vs. hash) for query text.
