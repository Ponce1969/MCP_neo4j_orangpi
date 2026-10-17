# readonly-neo4j-credential — the MCP and the read-only commands authenticate separately

Risk register **R2** of `docs/spec/07-security-and-operational-risks.md` §5, still open: "read-only
`READ_ACCESS` + managed `execute_read` proven (T-C.4), but **a distinct read-only credential/role is
not yet in place**". Today the whole process authenticates with one writer credential
(`config.py::neo4j_user` / `neo4j_password`), and read-only authority lives **only in code**
(`Neo4jQueryAdapter._read_session`: `database=neo4j_read_database`, `default_access_mode=READ_ACCESS`,
managed `execute_read`). A bug therefore cannot write *through that path*, but a compromised process
can open its own session and write: the privilege boundary is missing at the server.

Branch: `feat/readonly-neo4j-credential` (from `main`). Route: parent designs, a worker implements,
an independent verifier checks it (credentials are a high-risk surface).

## Specs

- **S1 — Authorization (owner, 2026-10-16, verbatim).** "R2 — credencial read-only (recomendado)".
- **S2 — The finding this closes (spec 07 §5, verbatim).** "R2 mitigated — **partial:** read-only
  `READ_ACCESS` + managed `execute_read` proven (T-C.4), but a distinct read-only credential/role is
  not yet in place."
- **S3 — The privilege boundary is the server's, not the code's.** The read paths must authenticate
  with a credential that **cannot write** even if it tries, so the protection survives a bug or a
  compromised process. The code-level guards (`READ_ACCESS` + `execute_read`) stay as they are: the
  change adds the server-side boundary, it does not replace the existing one.
- **S4 — Who reads and who writes (measured in this repository, not assumed).**
  - **Read-only callers**: the MCP server's composition root (`mcp_server_main._run_server`) builds
    `Neo4jQueryAdapter`, `Neo4jCommunityAdapter` and the skill-registry driver; the CLI read commands
    (`audit`, `gate`, `evaluate`) build the query/audit/validation/retrieval adapters.
  - **Writers**: the indexer (`neo4j_command_adapter`, `neo4j_checkpoint_adapter`), the community
    builder (`Neo4jCommunityAdapter.upsert_*`), resolution (`resolution_wiring`, merge adapters),
    quarantine/ledger. **Every writer is a CLI/script path, never the MCP** — that is the boundary
    this item formalises.
  - `Neo4jCommunityAdapter` is **mixed** (reads summaries for `ask_global`, writes them from the
    community script): the credential is therefore decided **per consumer by the composition root**,
    not stored inside the adapter.
  - `Neo4jQueryAdapter.ensure_indexes()` creates six indexes but **is not called anywhere in the live
    tree** (only in archived views and tests), so no read path performs a schema write. It stays
    uncovered by this feature; if someone ever calls it from the MCP, the read credential will reject
    it at the server, which is the desired failure.
- **S5 — Where the production rule lives.** The credential stays **optional in `Settings`** and the
  production requirement is enforced in `_run_server`, exactly like `mcp_access_token` and
  `mcp_hmac_key`: a rule inside `Settings` would refuse to build in tests and local runs, because
  `app_env` defaults to `production`. In production the MCP **refuses to start** without a read
  credential; outside production it warns. The CLI read commands use the read credential **when
  configured** and fall back to the main one otherwise (a local checkout must keep working).
- **S6 — The 32-character floor does not apply.** That floor exists for the bearer token and the
  HMAC key. A database password is not a token: the writer credential on this host measures 15
  characters (read-only check on 2026-10-16). The rule for the read credential is "survives
  stripping **and keeps at least one printable character**" for both the user and the password, in
  **one** place so the boot check and the wiring cannot drift. That shape check is shared with the
  token rule (`domain.mcp_security.is_effective_secret_shape`), because the hole it closes is the
  same one: `str.strip` leaves invisible non-whitespace characters (U+200B, U+FEFF) behind, so a
  field made only of those looks configured, and production would boot with a credential that cannot
  authenticate.
- **S7 — Operators create the role; the code does not.** Creating a role/user and granting
  privileges is a **production write on the security surface**, so it is a documented, human-run step
  with a rollback (`DROP USER` / `DROP ROLE`), and the migration order is: role → environment →
  deploy the code that requires it. The repository never creates roles at runtime.
- **S8 — Verification is execution, not inspection.** The write rejection must be proven against a
  real server with the read credential, and the read paths must keep working with it (`SHOW USER …
  PRIVILEGES`, a read smoke, and a write attempt that must fail).

## Non-goals

- No role/user creation, no production write of any kind, no change to the writers' credential.
- No refactor of the mixed adapter into read/write classes: the composition root decides, the adapter
  keeps its single constructor.
- No change to the existing read-only guards (`READ_ACCESS`, `execute_read`, the audit's static
  policy) and no new graph query.

## Tasks

Format: ID · specs · route · commit.

- **T1 · S5, S6** — `Settings` gains `neo4j_readonly_user: str = ""` and
  `neo4j_readonly_password: SecretStr | None = None`, plus **one** rule module that answers
  "is a read credential configured" and "give me the read view of these settings"
  (`infrastructure/neo4j_read_credentials.py`, mirroring how `domain.mcp_security.effective_token`
  centralises the token rule). Test-first. Route: worker.
- **T2 · S3, S4, S5** — the MCP composition root uses the read credential for `Neo4jQueryAdapter`,
  `Neo4jCommunityAdapter` and the registry driver, and `_run_server` refuses to start in production
  without it (warning outside production). Test-first. Route: worker.
- **T3 · S4, S5** — the CLI read commands (`audit`, `gate`, `evaluate`, `query`, `validate`) build
  their read adapters from the read view when configured, falling back to the main credential.
  Test-first. Route: worker (the first three), parent (the last two, found by the verifier).
- **T4 · S8** — tests: unit coverage for the rule module (present, absent, whitespace-only,
  invisible-only, trimming), the MCP boot refusing in production and accepting with the credential,
  the wiring actually receiving the read user, and an integration test (skipped when the read
  credential is not configured) that a **write statement is rejected by the server** while a read
  succeeds. Route: worker, extended by the parent after verification.
- **T5 · S7** — the human-facing runbook: the exact Cypher for the role and grants (validated against
  Neo4j 5.23 documentation, with a verification step that proves the grants), the environment
  variables, the migration order, the smoke and the rollback. Route: parent.
- **T6 · S8** — closure: `docs/ops/mcp-service.md` documents the credential, spec 07 §5's R2 status
  moves to complete only after the operator's step is executed and verified, the feature doc keeps the
  log, and the work-unit commits carry tests and docs together. Route: parent.

Gates before any "done": `uv run ruff check .` · `uv run mypy .` ·
`uv run python scripts/validate_architecture.py` · `uv run pytest tests/ -q --ignore=tests/integration`.

## Log

- **L1 (user, verbatim).** "R2 — credencial read-only (recomendado)".
- **L2 (recon, measured in the tree before any write).** Eight driver-creation sites; the MCP's
  composition root builds only read adapters (`mcp_server_main.py:145`, `:147`, `:193`); every writer
  is a CLI path (`neo4j_command_adapter.py:37`, `neo4j_checkpoint_adapter.py:25`,
  `community_adapter.py` write methods, `resolution_wiring.py:57`); `Neo4jCommunityAdapter` is mixed,
  so the credential must be decided by the consumer; `Neo4jQueryAdapter.ensure_indexes()` is not
  called anywhere in the live tree; the production fail-fast precedent for secrets lives in
  `_run_server` (`mcp_server_main.py:120`), not in `Settings`, because `app_env` defaults to
  `production` (`config.py:164`) and a rule there would break every test that builds settings.

  **Follow-up, out of this feature's declared scope but part of the same least-privilege intent**: the
  read-only ops scripts still authenticate with the writer credential
  (`scripts/eval_fulltext_recall.py`, `scripts/verify_retrieval_references.py`,
  `scripts/run_ragas_evaluation.py`, `scripts/run_evaluation_harness.py`,
  `scripts/build_namespace_profiles.py`, `scripts-ops/diagnose_generation_layer.py`) and so does the
  `ledger rollback` **plan** reader (`resolution_wiring.py` wires `Neo4jRollbackPlanAdapter` from the
  main settings). They are operator/dev tools whose runs happen outside the exposed surface, so they
  are recorded here rather than changed in this feature; each is a one-line swap when someone wants it.
- **L3 (verification, 2026-10-16 — an independent verifier found four real defects, all fixed).** The
  read-only verification of `185ab77` + `234f432` confirmed the design and falsified four claims:
  1. **A bypass** (falsified S5/S6): a password of *invisible non-whitespace* characters (U+200B,
     U+FEFF) survived `str.strip`, so `has_readonly_credentials` said yes and production would boot
     with an unusable credential — failing at query time instead of refusing to start. Fixed by
     sharing the token rule's shape check (`domain.mcp_security.is_effective_secret_shape`), applied
     to the user and the password; RED observed as `assert True is False`.
  2. **Incomplete split** (falsified the title): `query` and `validate` are read-only commands and
     still used the writer credential. Fixed (both now use the read view) with wiring tests that
     fail if the swap regresses.
  3. **A silently skipping verification command**: the runbook exported `NEO4J_READONLY_TEST_USER` /
     `..._TEST_PASSWORD` while the test reads `NEO4J_READONLY_USER` / `NEO4J_READONLY_PASSWORD`, so
     following it verbatim verified nothing. Fixed in the runbook, which now also names the three
     server error codes the test accepts (it did not include `Unauthorized`).
  4. **A test that could not catch the drift it claimed**: the boot-refusal tests asserted only that
     the *server adapter* was missing, so moving the guard after the adapter construction would have
     passed. Now they assert that **nothing** was constructed (`fake_adapters == {}`).
  Also corrected: S6's premise is now attributed to the host (15 characters, checked read-only), and
  `.env.example` has no `NEO4J_READONLY_*` template — that file belongs to the maintainer and was not
  touched, so it is reported instead of edited. Verified after the fixes: the five CLI wiring tests,
  the boot tests, the rule tests (invisible characters included), `ruff`, `ruff format --check`,
  `mypy --strict` over 463 files and the architecture validator.
- **L4 (operator live production inspection; date not specified).** The deployed server is Neo4j
  Community Edition 5.6. `CREATE ROLE` and `GRANT ROLE` fail with `Unsupported administration
  command`; `SHOW USERS` reports `roles: NULL` for every user. The raw write transaction under the
  configured read credential was accepted, while the read half (`RETURN 1`) passed and the MCP smoke
  test passes 100%. This confirms Community has no DBMS RBAC: **delivered** are the separate
  credential, its use by the MCP/read consumers, the production configuration requirement, consumer
  attribution, and the existing application-level `READ_ACCESS` + managed `execute_read` boundary.
  **Not delivered on this edition** is server-side denial of raw writes; the credential remains able
  to write outside the managed application path. That half requires an edition with RBAC plus a
  configured and verified restricted role. The operator's verification step changed shape rather
  than failed: read and smoke checks pass, while a `roles: NULL` detection must skip only the
  impossible server-denial assertion and state that enforcement is application-level on Community.
- **L5 (live schema inspection and committed manifest).** APOC `relationship` rows use
  `elementType` for the kind and `type` for the property datatype (or literal `RELATIONSHIP`). On a
  `RELATIONSHIP` row, `property` holds an endpoint node label, not a relationship property; the
  endpoint data remains an explicit follow-up, and the APOC parser now ignores those rows as
  properties. Native `db.schema.relTypeProperties()` names with `:` plus backticks normalize to the
  same clean names as the sample tier.

  The committed `data/schema/graph_schema.json` has `source: native` and
  `graph_version: gv-c00459a74b39`. It was assembled by feeding the operator's live inspection
  through the same models the runtime validates against, not by running `schema snapshot`. The earlier
  `gv-f900644fdb83` was computed from direct node counts: `knowledge:agentic-architectural-patterns`
  1520/7111, `knowledge:ai-engineering-huyen` 989/6963, `knowledge:essential-graphrag` 302/1241,
  and `knowledge:graphrag-agentic` 657/6078 (`chunks/entities`). The runtime census uses namespaced
  chunks (`c.book_id`) and entity IDs grouped by their first two colon-separated parts; it excludes
  entities whose `merged_into` is non-null/non-empty. Its counts are 1520/6899, 989/6526, 302/1103,
  and 657/5900 respectively: 3468 chunks and 20428 entities total. The direct node count is 21393,
  965 higher because it includes merged entities. The census is what the runtime uses, so
  `gv-c00459a74b39` is the committed, authoritative version; the earlier direct-count digest is not.

  The manifest is absent on the host until this branch is deployed. After deployment, `schema verify`
  compares the schema body: labels, relationship types, and their properties. A whole-file byte
  comparison with a fresh snapshot cannot succeed: each run stamps `committed_at` as `now`, uses the
  current checkout for `code_commit`, and selects `source: apoc` on this server because APOC is
  available and tried first. The committed file's `code_commit` is `2940839`, predating the name
  normalization and endpoint filter in `ae1700a`; this is metadata, not a reason to regenerate the
  frozen contract.
