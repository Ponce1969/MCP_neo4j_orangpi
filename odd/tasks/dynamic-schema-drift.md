# dynamic-schema-drift — R4: the schema comes from the graph, and a frozen manifest catches drift

R4 of `docs/spec/07-security-and-operational-risks.md` §5 records that `text2cypher_adapter.py` falls
back to `_HARDCODED_SCHEMA` — a hand-maintained constant listing only `Entity`, `Chunk` and a `RELATED`
edge — whenever `apoc.meta.data()` fails or returns nothing, so the model generates Cypher against a
schema that may have nothing to do with the graph, and the result is labelled with a provenance that
can even be wrong. This change makes the schema come from the graph, keeps a frozen manifest as the
last resort instead of a stale constant, and turns drift between the two into something the operator
can see — loudly where it can be acted on, quietly where it must not break a query.

## Specs

- **S1 — The ladder, in this order.** Verbatim from the approved decision: **"Ladder: APOC primero ->
  Nativo (`db.schema.*` + fallback a `db.labels()`/muestra) -> Manifiesto -> Fail tipado."**
  1. **APOC** — `CALL apoc.meta.data()` → `schema_source = "apoc"`. Kept first because it is the richer
     source and the one already proven in production.
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
  - `schema verify` (§S9) is the fail-closed surface, and an integration test (skipped without a live
    graph) fails on drift.
  - A missing manifest is **not** drift: it is "unknown", reported as such.
- **S4 — The frozen manifest lives at `data/schema/graph_schema.json`.** Verbatim: **"Ubicación:
  `data/schema/graph_schema.json`."** It is **generated** by a command, never hand-written, and it
  carries the schema body plus its provenance in the shape this repository already uses for committed
  artifacts (`data/evaluation/*_baseline.json`: `schema_version`, `committed_at`, `code_commit`), with
  `graph_version` taken from `domain/graph_version.py` and the tier that produced it in `source`. The
  body holds labels, relationship types and the properties of each, as names.
- **S5 — The provenance never lies.** Today `_format_schema` can return the hardcoded constant while
  the caller labels the result `"apoc"`: a result can claim a source it did not use. The redesign makes
  that impossible by construction, `schema_source` becomes `Literal["apoc", "native", "manifest"]`, and
  the constant is deleted. This is a **contract change** for the MCP tool payload's `schema_source`
  values: additive in mechanism, but a consumer comparing against `"hardcoded"` would see something
  new — say so in the PR's risk line rather than letting it pass silently.
- **S6 — Never depend on `propertyTypes`.** Its meaning changed between Neo4j versions ("potential Java
  types" became "possible Cypher types"). Use `nodeLabels`, `propertyName` and `relType`; types are
  informational at most, and no code may branch on their content.
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
  Route: worker. `commit: pending`
- **T2 · S3, S4** — the manifest model and its provenance, the drift comparison, the runtime surfacing
  (metadata + warning) and the `graph_version` it is reported at. Test-first. Route: worker.
  `commit: pending`
- **T3 · S9** — the two CLI commands with their exit codes, plus the integration test that fails on
  drift and skips without a live graph. Test-first. Route: worker. `commit: pending`
- **T4 · S7, S8** — the register, the docs and the tests that still assert `"hardcoded"`. Route: parent.
  `commit: pending`

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
