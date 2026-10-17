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
| T2 | Neo4j credentials | app process → DB | Separate read/write credentials are configured, but production Community has no RBAC; lateral write risk remains |
| T3 | Text2Cypher `query_cypher` | LLM-generated string → DB | Write/admin statement escaping the denylist |
| T4 | Schema description fed to LLM | graph-derived schema (APOC → native → manifest) → LLM | Readable mismatch reports drift; live-read failure is `unknown`; a stale manifest may still describe the graph; `schema verify` fails closed |
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

### R2 — Neo4j write boundary unavailable on the deployed edition (HIGH)

- **Evidence `[VERIFIED]`:** the MCP uses a distinct read credential, and the production
  service is configured with it; its smoke test passes. The live DBMS is Neo4j Community
  Edition 5.6: `CREATE ROLE`/`GRANT ROLE` are unsupported and `SHOW USERS` reports
  `roles: NULL`. A raw write transaction under the read credential succeeded, while
  `RETURN 1` succeeded as well. Community has no DBMS RBAC, so credential separation
  does not make that credential read-only.
- **Mitigation (implemented):** the application-managed MCP read path uses
  `READ_ACCESS` routing + managed `execute_read` transactions, and the separate
  credential provides consumer attribution and forward-compatibility. These controls
  protect the managed read path; they are not a server-side boundary against a process
  opening its own write session.
- **Mitigation (target):** use an edition with RBAC and configure/test a distinct
  least-privilege role for the MCP/query path; confine the write credential to CLI
  index/validate paths.
- **Residual risk:** HIGH on the deployed Community edition: the server accepted a raw
  write, so a compromised process using the read credential can still write directly.
  Server-side enforcement becomes available only on an edition with RBAC and after its
  role is configured and verified.

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
  `EXPLAIN` gate + disabled-by-default + application-managed read session
  (`READ_ACCESS` + `execute_read`, R2); Community does not add a DBMS write boundary.
- **Mitigation (target):** close the scope-parameter binding gap (see R8 — closed).
- **Residual risk:** LOW — the allowlist blocks write/admin vectors structurally and the
  scope-parameter binding gap (R8) is now closed; the keyword denylist remains only as
  a diagnostic prefilter.

### R4 — Hardcoded schema drift (mitigated)

- **Evidence `[IMPLEMENTED]` (inference):** inference walks a ladder and returns the tier that
  produced the schema (`infrastructure/neo4j_schema_reader.py::Neo4jSchemaReader`): APOC first because
  it can expose richer schema metadata; real rows are classified by `elementType` (`node` or
  `relationship`), not `type` (the property type or the literal `RELATIONSHIP`). This parser correction
  is part of this PR; the previous parser skipped every real APOC row. Next are the built-in
  `db.schema.nodeTypeProperties` and `db.schema.relTypeProperties`, whose availability is probed with
  `SHOW PROCEDURES` instead of by catching an exception per call, with a bounded fallback to
  `db.labels()` + `db.relationshipTypes()` plus a one-row per-label sample; then the generated manifest
  at `data/schema/graph_schema.json`; and finally a typed `SchemaInferenceError`, so no Cypher is ever
  generated against a guess. The constant `_HARDCODED_SCHEMA` is deleted and `schema_source` is
  `Literal["apoc", "native", "manifest"]`. The source label is returned with the schema from the same
  successful tier; an unparseable native result is not accepted as a trusted empty schema, so a
  non-empty graph cannot be described as `- (empty graph)`. The failure message goes through
  `format_redacted_error`, because a driver error can carry a URL or a credential.
- **Evidence `[IMPLEMENTED; DORMANT UNTIL MANIFEST]` (drift):** `domain/schema_manifest.py` contains
  the comparison mechanism for a bounded, sorted diff in result metadata and a warning log, together
  with the `graph_version` it was measured at (reused from `domain/graph_version.py`). The generated
  manifest does not exist yet, so runtime drift comparison is dormant and no live drift result has
  been verified. With no manifest, the comparison is `unknown` and `schema verify` returns `11`
  (incomplete). If live schema cannot be read while a manifest is present, the result is `unknown`,
  not drift; runtime may still use that stale manifest to describe the graph, while `schema verify`
  fails closed (`12` unreachable). A query is **never** blocked by drift. The live drift test runs
  only when `NEO4J_URI` is exported and fails on drift. `book-graph-rag schema snapshot` generates
  the manifest from the live schema, file only, through the same reader the runtime uses.
- **Impact:** invalid Cypher, wrong field assumptions, and silent degradation of `query_cypher`.
- **Residual risk:** LOW, with three limits stated rather than implied. The description carries names
  (labels, relationship types, properties) and **not** relationship endpoints — the previous
  description never had them either, so that remains a recorded limitation and not an invented
  feature. The bounded fallback samples one node per label, so it is lossy when properties vary
  between nodes with the same label. `propertyTypes` remains deliberately ignored because its meaning
  changed across Neo4j versions ("potential Java types" became "possible Cypher types"); APOC row
  classification uses `elementType`, not the property `type` column, so nothing branches on schema
  property types. The manifest is generated, never hand-written, and does not exist yet: until the
  operator runs `schema snapshot` against the production graph, drift comparison is dormant and
  `schema verify` reports `11` (incomplete), rather than pretending a frozen contract exists.

### R5 — Raw free-text logging (mitigated)

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
- **Evidence `[VERIFIED]`:** indexing failures use the single `redacted_error()` rule
  (`src/book_graph_rag/domain/checkpoint_models.py`) to persist exception type,
  raw-message character length and a keyed HMAC-SHA256 `QueryFingerprint`, never raw
  text. It is called by
  `IndexBookUseCase._fail_chunk` (checkpoint and dead-letter port record),
  `IndexBookUseCase._write_dead_letter` (legacy JSONL), and
  `ReplayDeadLetterUseCase`'s re-fail path. `CheckpointPort.release_lease_to_failed`
  in `src/book_graph_rag/ports/checkpoint_port.py` accepts one `RedactedError`,
  preventing raw text from crossing the port boundary. `Neo4jCheckpointAdapter` in
  `src/book_graph_rag/infrastructure/neo4j_checkpoint_adapter.py` writes flat
  `error_type`, `error_length`, `error_fingerprint_hex` and
  `error_fingerprint_key_id` properties and sets `c.error_message = null` on the node
  it touches; `error_metadata_from_node` in
  `src/book_graph_rag/infrastructure/checkpoint_error_metadata.py` is the inverse
  mapping used by both
  checkpoint readers and does not read `error_message`. Tests:
  `tests/unit/test_redacted_error.py` (18 cases),
  `tests/unit/dead_letter_record_test.py`,
  `tests/unit/checkpoint_port_signature_test.py`,
  `tests/unit/index_book_use_case_lease_test.py`, and
  `tests/test_index_book_use_case.py` (exact JSONL keys and keyed-HMAC filtering).
  Without a valid key, no fingerprint is stored and indexing does not abort.
- **Mitigation (implemented):** metadata-only query log + HMAC fingerprints +
  redaction + dev-only raw gate + bounded retention; dead-letter/checkpoint errors
  store `error_type`, `error_length` and, when a valid key is available, a keyed HMAC
  fingerprint — never raw text. Evaluation/readiness exception strings that reach a
  **persisted artifact** use the same redacted rendering, including the fields the RAGAS
  subprocess writes itself; the gate copies the resulting warnings. The child's stderr is
  the deliberate exception: dependency-fault messages there keep their text, because the
  runner captures and discards that stream and a human running the script standalone needs
  it to diagnose a missing dependency.
- **Residual risk:** LOW — fingerprints are only as private as their key, and this rule cannot tell
  a generated key from a pasted one (see `mcp_security.effective_token`). Dead-letter files and
  `:Checkpoint` nodes written before this change retain their raw error text: scrubbing existing
  checkpoint nodes is a graph write subject to §7.2, so pre-existing records are not silently
  migrated. Pre-change evaluation files may also retain raw errors: an escaped
  `evaluation_results.jsonl` existed in the checkout and was removed; copies elsewhere are not
  scrubbed. A standalone manual run of the RAGAS script still writes its local results file, now
  redacted. The fingerprint's key id is a configuration label and keeps its punctuation by design
  (the operator reads it to interpret the digest): a key id shaped like a secret is a configuration
  mistake that the 80-character bound only limits, and it is recorded here rather than papered
  over with a filter that would make every key id unreadable. The exception class name is the same
  shape of gap: the identifier filter strips punctuation, so an **alphanumeric** secret carried in a
  dynamically created class name survives intact — `type("sk_live_abc123", (Exception,), {})`
  renders it verbatim — and reaching that state already requires the attacker to be creating
  classes, which means running code. Bounding is what this rule guarantees for those two labels;
  hiding their content is not.
- **Evidence `[VERIFIED]` (closed):** evaluation and readiness reports no longer persist raw
  exception text. `format_redacted_error`, defined beside the rule so producers share one format,
  renders `ErrorType (message_length=N, fingerprint=<key_id>:<hex>)` when keyed. Both labels keep
  printable characters only, collapse whitespace and are capped at 80 characters; the class name is
  reduced further to identifier characters, which strips the punctuation a secret would need to stay
  readable but does **not** stop an alphanumeric one (see the residual below); an empty class becomes
  `UnknownError`, and a raising `str(error)` yields length 0. Without a usable key, errors remain
  redacted but have no fingerprint. Converted producers are the generation
  layer's dataset-load rationale and per-question failure tuple (the question id is preserved), the
  retrieval and resolution dataset-load rationales, both `subprocess_ragas_runner` failure notes,
  and `scripts/run_ragas_evaluation.py`: it redacts the per-question `error` persisted in
  `evaluation_results.jsonl` and forwarded into `--json-output` and `--after-output` (including the
  `failed_questions` list, which uses the same value). The runner passes `MCP_HMAC_KEY_ID` and
  `MCP_HMAC_KEY` through the child environment, never argv, and runs the script in a per-run
  temporary working directory; it resolves the script and dataset paths and gives `--json-output`
  and `--after-output` absolute temporary paths, so the fixed-name artifact is removed with that
  directory. The gate needs no code change: it copies `result.warnings` verbatim. A strengthened
  test drives a marker-carrying exception through the real generation producer into the gate's
  serialized result, asserting the marker is absent while the class, message length, question id
  and fingerprint remain. Artifacts are written by `main.py --output`, the RAGAS subprocess
  (`evaluation_results.jsonl` and its `--json-output`/`--after-output` outputs), and the evaluation
  harness (`data/evaluation/resolution_metrics.json`). Tests:
  `tests/unit/test_evaluate_generation_layer.py`, `test_evaluate_retrieval_layer.py`,
  `test_evaluate_resolution_layer.py`, `test_subprocess_ragas_runner.py`,
  `test_readiness_gate_evaluator.py`, `tests/unit/test_run_ragas_evaluation_script.py`.

### R6 — Prompt injection via natural-language tools (MEDIUM)

- **Evidence `[VERIFIED]`:** `query_cypher` and `ask_global` accept free-text questions
  and feed them to an LLM; `query_cypher` (when enabled) then executes generated Cypher
  under the structural allowlist.
- **Mitigation (implemented):** application-side structural allowlist + managed
  `READ_ACCESS` read path (`execute_read`, R2/R3) — enforcement does not rely on the
  LLM's compliance;
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
- **Least privilege:** a distinct credential is configured for the query/MCP surface
  and the writer credential is used for CLI ingest/validate. On production Community
  this provides attribution, not server-enforced read-only access; DBMS least privilege
  requires an edition with RBAC and a configured role (R2).
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
2. R2 mitigated — **partial:** the separate credential exists and is in use, and the
   application-managed `READ_ACCESS` + `execute_read` path remains in place. Production
   is Community Edition 5.6 (`roles: NULL`; role commands unsupported), where the server
   accepted a raw write under that credential; the server-side half is unavailable until
   an edition with RBAC is used and a restricted role is configured.
3. R3 mitigated — **verified complete:** structural allowlist + `EXPLAIN` + disabled-by-default
   verified; the scope-parameter binding gap (R8) is now closed. `ask_global`'s community read
   path is namespace-scoped (R7, code-verified — see §3).
4. R4 mitigated — **partial:** the inference mechanism is implemented, and this pull request fixes
   the APOC parser to read real `elementType` rows and rejects unparseable native output as a trusted
   empty schema. The hardcoded fallback is gone; the schema comes from the graph through the ladder
   (APOC → `db.schema.*` → bounded labels/sample → manifest → typed fail), and the source label
   identifies the tier that produced it. The manifest does not exist yet, so runtime drift comparison
   is dormant; drift is not verified complete until a manifest is generated and compared.
5. R5 mitigated — **verified complete:** MCP query logs are metadata-only + HMAC +
   redacted; dead-letter and checkpoint failures use `redacted_error()`
   (`src/book_graph_rag/domain/checkpoint_models.py`) to store `error_type` and
   `error_length`, plus a keyed HMAC-SHA256 fingerprint when a valid key is available,
   never raw text. The three writers are `IndexBookUseCase._fail_chunk`,
   `IndexBookUseCase._write_dead_letter`, and `ReplayDeadLetterUseCase`'s re-fail path.
   Evaluation/readiness errors use `format_redacted_error`, including the RAGAS subprocess
   fields in `evaluation_results.jsonl`, `--json-output` and `--after-output`; the gate
   copies those warnings. The strengthened gate test covers the real generation producer.
6. Readiness gate (06) passes; audit (04) shows no BLOCKING, no FAILED/UNREACHABLE —
   **run as the Phase 7 exposure checkpoint:** `gate expose-mcp` exit `0` (PASS).
   `gate expose-mcp-readiness` is **runnable again: its two documented blockers are gone.**
   The mechanism-first threshold delta landed on 2026-10-15 and every committed baseline now
   carries `thresholds_finalized: true` with a numeric threshold (retrieval
   `precision_at_k_min = 0.1175`, generation `faithfulness_min = 0.8`, resolution
   `f1_min = 0.6` + `hard_over_merge_max = 0.0`), so the gate **passes or fails on its own
   numbers** instead of reporting `INCOMPLETE` by design. The dead `QUERY_LLM` is no longer an
   obstacle either: production runs `deepseek-v4-flash` (verified read-only on the host), the
   host's checkout carries the current `main` after the 2026-10-16 deploy, and a local run
   points the query role at that model with per-run environment overrides — never editing
   `.env`. What remains is recording the checkpoint verdict with those baselines: a read-only
   run, since the gate never mutates the graph.
7. Explicit human approval recorded; production graph untouched by validation —
   **not yet granted (production exposure is a human decision).**

## 6. Acceptance criteria

- [x] R1/R2/R3/R5/R6 mitigations are verified in code with evidence (see register);
  R7/R8 are mitigated (code-verified, audit-verified) with the scope-aware community
  read and the scope-parameter binding wired end-to-end.
- [x] The managed MCP read path uses `READ_ACCESS` + managed `execute_read` and the
  structural allowlist. On production Community Edition 5.6, however, a raw write under
  the separate read credential was accepted; the integration test therefore verifies
  the read and skips only server-denial assertion when `SHOW USERS` reports `roles: NULL`.
  Credential separation is delivered for attribution; server-side least privilege remains
  target until an edition with RBAC and a configured role are used (R2).
- [x] MCP query logs contain no raw secrets or unbounded raw query text (metadata-only
  + HMAC fingerprints). Dead-letter/checkpoint error metadata written by current code
  is redacted: it contains type, length and, when keyed, HMAC fingerprints, never raw
  text (R5). Evaluation/readiness errors use the same redacted rendering, including the
  RAGAS script's `evaluation_results.jsonl` and `--json-output`/`--after-output` fields;
  the gate copies the warnings, and a test carries a marker exception through the real
  generation producer into the serialized gate result.
- [x] The exposure gate cannot pass while any CRITICAL residual risk remains — no
  CRITICAL residual remains after R1's mitigation, and the gate stays fail-closed
  against CRITICAL residuals; final exposure still requires the 06/07 gate + human
  approval.

## 7. Open decisions

- `[OPEN]` Whether to introduce per-role Neo4j credentials in dev first or only for
  production.
- `[OPEN]` Transport choice (TLS vs. Tailscale-only vs. both) for the first real exposure.
- `[OPEN]` Logging redaction policy specifics (mask vs. drop vs. hash) for query text.
  (Decided for the MCP query log and dead-letter/checkpoint errors: drop + keyed
  HMAC-SHA256 fingerprints — R5; error metadata also retains `error_type` and
  `error_length`.)
