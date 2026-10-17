# dynamic-schema-drift — R4: the schema comes from the graph, and a frozen manifest catches drift

R4 of `docs/spec/07-security-and-operational-risks.md` §5 records that the prior schema inference could
fall back to `_HARDCODED_SCHEMA` — a hand-maintained constant listing only `Entity`, `Chunk` and a
`RELATED` edge — when it could not produce usable schema data, so the model could generate Cypher
against a schema unrelated to the graph, with provenance that could be wrong. Adversarial verification
also established that the APOC parser skipped real `apoc.meta.data()` rows because it classified on
`type` rather than `elementType`; the APOC rung was not proven in production. This change makes the
schema come from the graph, keeps a frozen manifest as the last resort instead of a stale constant, and
makes drift between the two observable once the manifest exists — loudly where it can be acted on,
quietly where it must not break a query.

## Specs

- **S1 — The ladder, in this order.** Verbatim from the approved decision: **"Ladder: APOC primero ->
  Nativo (`db.schema.*` + fallback a `db.labels()`/muestra) -> Manifiesto -> Fail tipado."**
  1. **APOC** — `CALL apoc.meta.data()` → `schema_source = "apoc"`. It can expose richer schema
     metadata; classify rows by `elementType` (`node` or `relationship`), not `type` (property type or
     the literal `RELATIONSHIP`). This parser correction is part of this PR; the prior parser skipped
     real APOC rows and was not proven in production.
  2. **Native** — `CALL db.schema.nodeTypeProperties()` and `CALL db.schema.relTypeProperties()` →
     `schema_source = "native"`. They are built-in, **do not require APOC**, and give the
     label→properties mapping in one call each. If they are unavailable, fall back to
     `CALL db.labels()` + `CALL db.relationshipTypes()` plus a **bounded per-label sample**
     (`MATCH (n:Label) WITH n LIMIT 1 RETURN keys(n)`, and the relationship equivalent) → still
     `schema_source = "native"`. Probe availability explicitly with
     `SHOW PROCEDURES YIELD name WHERE name = 'db.schema.nodeTypeProperties'` rather than by catching an
     exception on every call.
  3. **Manifest** — the frozen `data/schema/graph_schema.json` (§4) → `schema_source = "manifest"`.
  4. **Typed fail** — nothing worked: see S2.
- **S2 — Fail closed when the graph cannot describe itself.** No Cypher may be generated against a
  guess. When every tier fails, raise one typed error (a dedicated exception raised from the adapter,
  surfaced by the MCP like any other tool failure) whose message names which probes failed so the
  operator knows what to check. That message must itself follow R5: no raw driver text (use
  `format_redacted_error`), because a driver error can carry a URL or a credential.
- **S3 — Drift is observable in runtime and fail-closed in verification.** Verbatim: **"Política de
  Drift: Observabilidad pura en runtime (reporta en metadata/log sin tumbar la query); fail-closed
  estricto en CLI/gates de evaluación y tests de integración."** Concretely:
  - In runtime, after inference, compare what the graph says against the manifest and carry a bounded,
    sorted, human-readable diff (added/removed labels, relationship types and properties) in the
    result metadata and in a warning log. **A query is never blocked by drift.**
  - The drift report **names the `graph_version` it was observed at**. Reuse
    `domain/graph_version.py::graph_version_from` — it already exists and the MCP already exposes it —
    because a drift report without the graph version it was measured against cannot be interpreted
    weeks later.
  - `schema verify` (§S9) is the fail-closed surface, and the integration test fails on drift when
    `NEO4J_URI` is exported; it skips when that variable is not exported.
  - A missing manifest is **not** drift: it is "unknown", reported as such. If the live schema
    cannot be read, the report is also `unknown`, not drift; a present stale manifest remains the
    runtime description fallback.
- **S4 — The frozen manifest lives at `data/schema/graph_schema.json`.** Verbatim: **"Ubicación:
  `data/schema/graph_schema.json`."** It is **generated** by a command, never hand-written, and it
  carries the schema body plus its provenance in the shape this repository already uses for committed
  artifacts (`data/evaluation/*_baseline.json`: `schema_version`, `committed_at`, `code_commit`), with
  `graph_version` taken from `domain/graph_version.py` and the tier that produced it in `source`. The
  body holds labels, relationship types and the properties of each, as names.
- **S5 — The source identifies the producing tier, not completeness.** Today
  `_format_schema` can return the hardcoded constant while the caller labels the
  result `"apoc"`: a result can claim a source it did not use. The redesign returns
  `schema_source` with the schema from the same tier and rejects unparseable native
  output rather than trusting it as an empty graph; the constant is deleted. This is
  a **contract change** for the MCP tool payload's `schema_source` values: additive in
  mechanism, but a consumer comparing against `"hardcoded"` would see something new —
  say so in the PR's risk line rather than letting it pass silently.
- **S6 — Never depend on `propertyTypes`.** Its meaning changed between Neo4j versions ("potential Java
  types" became "possible Cypher types"). Use `nodeLabels`, `propertyName` and `relType`; types are
  informational at most, and no code may branch on their content. APOC rows are classified by
  `elementType`, not the property `type` column; the prior parser's `type` branch was incorrect and is
  corrected in this PR.
- **S7 — The register and the docs stop claiming the old behaviour.** `docs/spec/07` §5's R4 item and
  any other document that describes the schema source (grep `hardcoded`, `schema_source`,
  `_HARDCODED_SCHEMA` across `docs/` and `README.md`) are updated to what the code does, including the
  residual: the description is derived from the graph's *shape*, and relationship **endpoints** are not
  part of it — the previous description never had them either, so that stays a recorded limitation
  rather than an invented feature.
- **S8 — Nothing else moves.** The text2cypher pipeline keeps its timeouts, retries, structural
  validator, `EXPLAIN` self-heal and error types. The MCP payload stays additive. Golden captions and
  metric-derived warnings are untouched.
- **S9 — Two commands, read-only and fail-closed.** `book-graph-rag schema verify` (read-only: it never
  writes the graph or the manifest) exits with the same codes this project already uses for
  `audit`/`gate` — **0** pass, **10** drift, **11** incomplete (no manifest), **12** unreachable,
  **13** failed — and `book-graph-rag schema snapshot --output data/schema/graph_schema.json` writes the
  manifest file (never the graph). Both go through the same ladder, so a snapshot taken with APOC
  missing is still true about its tier.

## Tasks

- **T1 · S1, S2, S5, S6** — the ladder, the typed fail and the source type in `text2cypher_adapter.py`
  and the port, test-first with a fake executor per tier (APOC present, APOC absent but `db.schema.*`
  present, both absent with only `db.labels()` available, everything failing, and an empty graph).
  Route: worker. `commit: 18a5ad4`
- **T2 · S3, S4** — the manifest model and its provenance, the drift comparison, the runtime surfacing
  (metadata + warning) and the `graph_version` it is reported at. Test-first. Route: worker.
  `commit: 18a5ad4`
- **T3 · S9** — the two CLI commands with their exit codes, plus the integration test that fails on
  drift when `NEO4J_URI` is exported and skips otherwise. Test-first. Route: worker. `commit: a2ee0b6`
- **T4 · S7, S8** — the register, the docs and the tests that still assert `"hardcoded"`. Route: parent.
  `commit: (this document's own commit)`

Note on the commit split: T1 and T2 landed together because they are coupled — the adapter's ladder
needs the reader, and the fixture that asserts the new `schema_source` literal cannot be compiled
against the old port. The pre-commit hook checks the staged snapshot, so a partial commit that does
not compile is impossible; the work units are therefore "the mechanism" and "the commands".

## Log

- **L1 (the task, verbatim, 2026-10-17):** "Una vez cerrado #38, arrancamos directamente con **R4
  (Schema dinámico)** en este repositorio." / "arranca de inmediato con R4 en su propia rama/feature
  doc." It implements R4 of `docs/spec/07-security-and-operational-risks.md` §5: "**Evidence
  `[VERIFIED]`:** `text2cypher_adapter.py::_HARDCODED_SCHEMA` lists only `Entity`/`Chunk` and a
  `RELATED` edge. When `apoc.meta.data()` is unavailable it falls back to this stale schema, so the LLM
  generates against a schema that may not match the actual graph. **Mitigation (target):** keep the
  schema source dynamic (APOC) and, when unavailable, derive a minimal schema from the catalog (02) or a
  committed schema manifest rather than a hand-maintained constant; mark `schema_source` in results."
- **L2 (the three decisions, verbatim, 2026-10-17):** "1. **Ubicación:** `data/schema/graph_schema.json`.
  2. **Política de Drift:** Observabilidad pura en runtime (reporta en metadata/log sin tumbar la
  query); fail-closed estricto en CLI/gates de evaluación y tests de integración. 3. **Ladder:** APOC
  primero -> Nativo (`db.schema.*` + fallback a labels/muestra) -> Manifiesto -> Fail tipado."
- **L3 (read-only exploration, 2026-10-17 — the facts this spec is written from).** Verified against the
  Neo4j 5 documentation, not from memory: `db.schema.nodeTypeProperties() :: (nodeType, nodeLabels,
  propertyName, propertyTypes, mandatory)` and `db.schema.relTypeProperties() :: (relType,
  propertyName, propertyTypes, mandatory)` are built-in and **do not require APOC**;
  `db.labels()`, `db.relationshipTypes()` and `db.propertyKeys()` (graph-wide, **no per-label mapping**)
  exist in Community; `propertyTypes` changed meaning across versions (S6). Repo side:
  `_HARDCODED_SCHEMA` at `text2cypher_adapter.py:25`; `_infer_schema` at `:163-175`; `_format_schema` at
  `:177-195` with the mislabel described in S5; the result carries `schema_source` at `:159`; the port
  type is `Literal["apoc", "hardcoded"]` (`ports/text2cypher_port.py:20,27`); the MCP surfaces it
  (`mcp_server_adapter.py:934`); tests asserting `"hardcoded"`:
  `tests/test_text2cypher_adapter.py:61,98,244,262`, `tests/test_mcp_server_adapter.py:179,1023,1035`,
  `tests/integration/test_query_cypher_scope.py:42`. **No schema snapshot or manifest exists anywhere in
  the repository** (`catalog.yaml` is the corpus catalog, not the graph schema; `config.py`'s
  `schema_version` is a label, not a contract), so T2 introduces it. **`graph_version` already exists**
  (`domain/graph_version.py::graph_version_from`, exposed by the MCP at `mcp_server_adapter.py:992,1088,
  1313`) and is reused rather than reinvented. Recorded for a later step: when the SSH tunnel is opened
  for R2's operator step, the same read-only session can inspect the live schema and generate the first
  manifest from production data instead of from what the documentation promises.
- **L4 (implementation, 2026-10-17).** The mechanism and the commands landed as two commits
  (`18a5ad4`, `a2ee0b6`) plus this documentation. RED was observed per task: the tier tests failed with
  `"hardcoded"` where `"native"` was expected and with `DID NOT RAISE` where the typed fail belongs,
  and the CLI tests failed with `No such command 'schema'` and exit 2. One honest limitation of that
  evidence: the drift test's RED was a `ModuleNotFoundError` for the new module, so the drift
  assertion itself was never seen failing — the module has to exist for the import to succeed — and it
  was verified in GREEN only. The pre-commit hook also decided the commit split for us: it checks the
  **staged** snapshot, so a partial commit that does not compile is impossible, and T1 and T2 are
  coupled (the adapter's ladder needs the reader, and the fixture asserting the new `schema_source`
  literal cannot compile against the old port). The worker flagged two pre-existing reds in
  `tests/integration/test_query_cypher_scope.py` and left them alone; the parent reproduced them on the
  base commit before touching them — the same two tests fail identically on `main` with the same
  `QueryFingerprintError`, caused by a sixteen-character fixture key against the thirty-two-character
  floor the fingerprint rule enforces — and fixed them here because this change modifies that file.
  Suite after the change: **2175 passed, 3 skipped, 0 failed**. The drift-test skip gate is specifically
  `NEO4J_URI` being exported, not whether Neo4j variables are present in `.env`; this machine's `.env`
  sets Neo4j variables but does not export `NEO4J_URI`, which is why that test skipped.
- **L5 (adversarial verification, 2026-10-17).** The APOC rung was dead: the parser
  classified rows using `type` as lowercase node/relationship, while a real
  `langchain_community` consumer in this environment runs
  `CALL apoc.meta.data() YIELD label, other, elementType, type, property WHERE NOT
  type = "RELATIONSHIP" AND elementType = "node"`. That actual consumer query established
  the row shape, not a guess; the code-side correction in this PR classifies by
  `elementType`. Verification also found an unreachable-masking defect, an empty-native
  schema defect, and `compare_schema(max_items=0)`; the implementation changes address
  these as code fixes, not documentation-only changes. A CRLF write
  was caught as a line-ending issue, not a schema behavior change. The generated
  `data/schema/graph_schema.json` path was gitignored; the parent found and fixed that
  separate `.gitignore` issue. The corrections
  in `docs/spec/07-security-and-operational-risks.md`, `docs/ops/mcp-service.md`, this task
  file and `.agents/skills/book-graph-mcp-usage/SKILL.md` are documentation-only; this
  worker changed no code or tests.
