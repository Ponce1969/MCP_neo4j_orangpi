# 07 — Security and Operational Risks

> **Status: Active (normative).** This risk register binds before any production
> exposure. Findings are `[VERIFIED]` against the current code/config. Mitigations
> marked implemented are verified against the code; a non-empty residual risk means
> the exposure gate (00 §3) is not yet passable.

## 1. Scope and method

This document covers the application surface of `book-graph-rag`: the MCP server, the
CLI, the Neo4j connection, Text2Cypher, and logging. It does **not** cover the Orange Pi
host itself, other projects' containers, or the OS — those are out of scope and governed
by `AGENTS.md` §7. Severity uses: **CRITICAL / HIGH / MEDIUM / LOW**. "Residual risk" is
what remains after the listed mitigations; a non-empty residual risk means the exposure
gate (00 §3) is not yet passable. Phase 6 (MCP hardening) added two open risks: **R7**
(`ask_global` community-scope gap) and **R8** (Text2Cypher scope-parameter binding).

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

### R1 — Network exposure: plaintext SSE bind (CRITICAL)

- **Evidence `[VERIFIED]`:** `Settings.mcp_bind_host` defaults to loopback
  `127.0.0.1`; `_validate_bind_host_is_private_in_production` rejects wildcard/public
  binds (`0.0.0.0`/`::`/`""`) when `app_env == "production"` (`config.py`).
  `mcp_server_main.py::_run_server` passes `settings.mcp_bind_host` to `run_sse`;
  the production systemd unit sets `Environment=MCP_BIND_HOST=100.106.85.109` (the
  Tailscale IP) and `.env.example` documents `MCP_BIND_HOST`
  (`deploy/mcp-server.service`).
- **Impact:** Any host that can reach the bound port can invoke all 8 tools and read
  graph contents without authentication (SSE is plaintext HTTP).
- **Mitigation (implemented):** loopback default (fail-closed) + production wildcard
  rejection + private Tailscale bind in the systemd unit. Consumption remains opt-in
  (`AGENTS.md` §6).
- **Mitigation (target):** TLS and/or an authenticated transport before any broader
  exposure.
- **Residual risk:** LOW on a private Tailscale-only bind; TLS/auth are still absent,
  so exposure beyond the private network stays HIGH until the transport is hardened.

### R2 — Shared Neo4j read/write credentials (HIGH)

- **Evidence `[VERIFIED]`:** all Neo4j adapters still construct drivers from the same
  `settings.neo4j_user`/`settings.neo4j_password` (`config.py`); `neo4j_read_database`
  defaults to the same `"neo4j"` database. Credential separation (a distinct
  read-only role) is **not yet done**.
- **Mitigation (implemented):** the MCP read path enforces read-only authority via
  `READ_ACCESS` routing + managed `execute_read` transactions; write statements are
  rejected with a typed `UnsupportedQueryError` (`neo4j_query_adapter.py::_read_session`,
  `_read_records`, `_raise_typed_driver_error`). Proven by a write-rejection
  integration test asserting graph-snapshot equality (T-C.4).
- **Mitigation (target):** least-privilege — a separate read-only Neo4j role/credential
  for the MCP/query path; write credential confined to the CLI index/validate path.
- **Residual risk:** MEDIUM until a distinct read-only credential/role exists — the
  read path is restricted by access mode, but a compromised write credential could
  still reach the DB directly.

### R3 — Dynamic Text2Cypher escaping the guard (HIGH)

- **Evidence `[VERIFIED]`:** the pure keyword denylist has been superseded by a
  **structural allowlist validator** (restricted tokenizer + recursive-descent
  grammar). It accepts only the read-only subset and rejects
  `CREATE`/`SET`/`MERGE`/`DETACH`/`REMOVE`/`DELETE`/`DROP`, `CALL`, `LOAD CSV`,
  `FOREACH`, `UNWIND`, subqueries, and literal `WHERE` values
  (`structural_cypher_policy.py`). `EXPLAIN` is mandatory before execution
  (`text2cypher_adapter.py::_generate_and_validate`). `query_cypher` is disabled by
  default (`mcp_enable_query_cypher=False`).
- **Mitigation (implemented):** structural allowlist (the security decision) +
  `EXPLAIN` gate + disabled-by-default + read-only session (R2).
- **Mitigation (target):** close the scope-parameter binding gap (see R8 — closed).
- **Residual risk:** LOW — the allowlist blocks write/admin vectors structurally and the
  scope-parameter binding gap (R8) is now closed; the keyword denylist remains only as
  a diagnostic prefilter.

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

- **Evidence `[VERIFIED]`:** MCP query logging is now **metadata-only** — raw
  query/prompt/error text never enters the persisted JSONL. Free-text inputs are
  replaced by keyed HMAC-SHA256 fingerprints (`query_fingerprint`,
  `prompt_fingerprint`) and failures are reduced to a stable `error_code`
  (`domain/models.py::QueryLogEntry`, `mcp_server_adapter.py::_log`). A v2 schema +
  v1→v2 migration drops legacy raw fields (`models.py::migrate_query_log_record`).
  Raw logging is development-only and fail-closed
  (`config.py::_validate_raw_logging_is_development_only`); secret redaction is
  applied to metadata (`models.py::redact_sensitive_metadata`). Retention is bounded
  (`mcp_log_retention_days`, `json_query_logger_adapter.py`).
- **Evidence `[VERIFIED]` (residual):** the indexing dead-letter path still writes
  `error_message = str(error)` verbatim (`index_book_use_case.py::_write_dead_letter`,
  `::_release_to_failed`).
- **Mitigation (implemented):** metadata-only query log + HMAC fingerprints +
  redaction + dev-only raw gate + bounded retention.
- **Mitigation (target):** bound/sanitize dead-letter `error_message`.
- **Residual risk:** LOW for the MCP query log (metadata-only); MEDIUM for the
  dead-letter error messages until bounded/sanitized.

### R6 — Prompt injection via natural-language tools (MEDIUM)

- **Evidence `[VERIFIED]`:** `query_cypher` and `ask_global` accept free-text questions
  and feed them to an LLM; `query_cypher` (when enabled) then executes generated Cypher
  under the structural allowlist.
- **Mitigation (implemented):** server-side structural allowlist + `READ_ACCESS`
  read-only session (R3/R2) — enforcement does not rely on the LLM's compliance;
  fail-closed scope is required for all structured tools and per-tier budgets bound
  every tool.
- **Mitigation (target):** scope `ask_global`'s community read path to the caller's
  namespace (see R7).
- **Residual risk:** LOW for the structured/`query_cypher` paths (read-only + scoped +
  bounded); MEDIUM for `ask_global` until the community read path is
  namespace-filtered (R7).

### R7 — `ask_global` community read path ignores resolved scope (mitigated — code-verified, audit-verified)

- **Evidence `[VERIFIED]`:** `McpServerAdapter.ask_global` validates scope fail-closed
  (`InvalidScopeError`/`MissingScopeError` raised before the use case) and now threads the
  resolved `ScopeContext` through `GlobalQueryUseCase.ask(question, detail_level, scope=scope)`
  (`mcp_server_adapter.py::ask_global`) and
  `CommunityReadPort.get_summaries_by_level(level, *, scope=None)`
  (`ports/community_read_port.py`) into `Neo4jCommunityAdapter.get_summaries_by_level`
  (`community_adapter.py`), which binds
  `WHERE ANY(x IN c.entity_ids WHERE x STARTS WITH $scope_prefix)` with
  `scope_prefix = f"{scope.source.source_id}:"`.
- **Impact:** a scoped `ask_global` could read community summaries across namespaces.
- **Mitigation (implemented):** the scoped branch filters by the namespaced `entity_ids`
  prefix. ``ANY`` (not ``ALL``) follows the approved R7 scope: it excludes any summary whose
  `entity_ids` are entirely outside the prefix, while remaining permissive toward
  mixed-coverage summaries (deliberately out of scope). The ingestion contract enforced by
  `SCOPE_KEYS_BY_LABEL` (`domain/mcp_security.py`) keeps every namespaced `Entity.id`
  prefixed, so summaries are derived from namespaced entities.
- **Verified:** `test_ask_global_scope_community.py` (cross-namespace leak),
  `test_ask_global_scope.py` (scope reaches the read port),
  `test_neo4j_community_adapter.py` (scoped filter + unscoped default),
  `test_global_query_use_case.py::test_ask_forwards_scope_to_read_port`.
- **Residual risk:** mitigated (code-verified, audit-verified): `gate expose-mcp` exit
  `0` at the Phase 7 exposure checkpoint; the scoped branch is proven by the
  cross-namespace leak test and the unscoped path
  (`Neo4jRetrievalAdapter.fetch_contexts`, `infrastructure/neo4j_retrieval_adapter.py`)
  is unchanged.

### R8 — Text2Cypher does not bind scope parameters end-to-end (mitigated — code-verified, audit-verified)

- **Evidence `[VERIFIED]`:** `Text2CypherAdapter.generate_and_run(question, *, scope=None)`
  builds a parameter map strictly from the validator's `scope_proofs` via
  `_build_scope_parameter_map` (`text2cypher_adapter.py`) and threads it through
  `_CypherExecutor.explain(cypher, parameters=...)` (EXPLAIN must also carry the map or
  Neo4j fails with `Expected parameter(s)`) and `execute_read(cypher, parameters=...)`
  into `Neo4jQueryAdapter.execute_read(cypher, parameters=None)`
  (`neo4j_query_adapter.py::execute_read`), which binds `parameters or {}` into the
  managed read transaction. `query_cypher` accepts optional scope kwargs and resolves a
  `ScopeContext` fail-closed before calling `generate_and_run`
  (`mcp_server_adapter.py::query_cypher`).
- **Impact:** a scope-proof dynamic query can now execute end-to-end with its proven
  `$param` bindings supplied.
- **Mitigation (implemented):** `_build_scope_parameter_map` derives values strictly from
  `scope_proofs` (`Chunk.book_id =` → `scope.source.source_id`; `Chunk.book_id IN` →
  `list(scope.book_ids)` or `[scope.source.source_id]`); any non-derivable proof
  (`Entity.id` or unknown) raises the existing `CypherGenerationError` after the retry
  budget (`retries=2` unchanged) with the failed query in context — no new error type.
  `query_cypher` stays disabled by default (`mcp_enable_query_cypher=False`).
- **Verified:** `test_text2cypher_adapter.py` (parameter-map binding, `IN` list binding,
  non-derivable proof fails typed only after retries=2), `test_query_cypher_scope.py`
  (fail-closed scope resolution and forwarding), `test_neo4j_query_adapter.py`
  (parameter pass-through into the read transaction).
- **Residual risk:** mitigated (code-verified, audit-verified): `gate expose-mcp` exit `0`
  at the Phase 7 exposure checkpoint; `query_cypher` remains disabled by default.

## 4. Operational controls `[TARGET]`

- **Secrets:** never in code, docs, or logs; `.env` gitignored; `SecretStr` at rest
  (`[VERIFIED]` `config.py`). Inject via environment/secret manager in production.
- **Least privilege:** read-only Neo4j credential for the query/MCP surface; write
  credential for CLI ingest/validate. (Read-only access mode is `[VERIFIED]`; the
  distinct credential is still target — R2.)
- **Network:** private transport (Tailscale-only), no `0.0.0.0` on untrusted networks,
  TLS/auth where required. (Loopback default + production wildcard rejection
  `[VERIFIED]` — R1.)
- **Observability:** structured, redacted logs with bounded retention; query provenance
  (tool, duration, result count, error type) retained for gap analysis.
  (Metadata-only + HMAC fingerprints `[VERIFIED]` — R5.)
- **Change control:** exposure/network/auth changes pass the exposure gate (00 §3) and
  require human approval.

## 5. Preconditions before production exposure

Phase 6 (MCP hardening) completed the code-side hardening; the exposure gate itself
remains human-gated. Verified complete vs. remaining:

1. ~~R1 mitigated~~ — **verified complete:** loopback default + production wildcard
   rejection + Tailscale bind in the systemd unit (`config.py`,
   `deploy/mcp-server.service`).
2. R2 mitigated — **partial:** read-only `READ_ACCESS` + managed `execute_read` proven
   (T-C.4), but a distinct read-only credential/role is not yet in place.
3. R3 mitigated — **verified complete:** structural allowlist + `EXPLAIN` + disabled-by-default
   verified; the scope-parameter binding gap (R8) is now closed. `ask_global`'s community read
   path is namespace-scoped (R7, code-verified — see §3).
4. R4 mitigated — **not done:** schema fallback is still the hardcoded constant;
   dynamic schema inference remains the live path (`text2cypher_adapter.py`).
5. R5 mitigated — **partial:** MCP query log is metadata-only + HMAC + redacted; the
   dead-letter `error_message` is still raw.
6. Readiness gate (06) passes; audit (04) shows no BLOCKING, no FAILED/UNREACHABLE —
   **run as the Phase 7 exposure checkpoint:** `gate expose-mcp` exit `0` (PASS).
   `gate expose-mcp-readiness` remains `INCOMPLETE` (exit `11`) by design while
   `data/evaluation/*_baseline.json` carry `thresholds_finalized: false` (mechanism-first,
   06 §11) — pending the later Phase 5 threshold delta. (Local checkpoint note: the
   readiness gate's generation layer requires a live `QUERY_LLM`; the configured model
   is end-of-life, so the local run surfaced an environment failure instead of the
   mechanism-first `INCOMPLETE` — re-run after the model is updated.)
7. Explicit human approval recorded; production graph untouched by validation —
   **not yet granted (production exposure is a human decision).**

## 6. Acceptance criteria

- [x] R1/R2/R3/R5/R6 mitigations are verified in code with evidence (see register);
  R7/R8 are mitigated (code-verified, audit-verified) with the scope-aware community
  read and the scope-parameter binding wired end-to-end.
- [x] The MCP path cannot execute a write statement (`READ_ACCESS` + managed
  `execute_read` + structural allowlist, proven by write-rejection integration test).
  A write-capable credential still exists (R2 residual) — least-privilege separation
  remains target.
- [x] MCP query logs contain no raw secrets or unbounded raw query text (metadata-only
  + HMAC fingerprints). Dead-letter `error_message` is still raw (R5 residual).
- [x] The exposure gate cannot pass while any CRITICAL residual risk remains — no
  CRITICAL residual remains after R1's mitigation, and the gate stays fail-closed
  against CRITICAL residuals; final exposure still requires the 06/07 gate + human
  approval.

## 7. Open decisions

- `[OPEN]` Whether to introduce per-role Neo4j credentials in dev first or only for
  production.
- `[OPEN]` Transport choice (TLS vs. Tailscale-only vs. both) for the first real exposure.
- `[OPEN]` Logging redaction policy specifics (mask vs. drop vs. hash) for query text.
  (Decided for the MCP query log: drop + keyed HMAC-SHA256 fingerprints — R5. The
  dead-letter `error_message` policy remains open.)
