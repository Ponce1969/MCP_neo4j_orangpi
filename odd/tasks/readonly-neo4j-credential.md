# readonly-neo4j-credential — a distinct read-only Neo4j credential for every read path

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
  HMAC key. A database password is not a token: the host's current one is 15 characters. The rule for
  the read credential is "user non-empty **and** password non-empty after trimming", in **one** place
  so the boot check and the wiring cannot drift.
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
- **T3 · S4, S5** — the CLI read commands (`audit`, `gate`, `evaluate`) build their read adapters
  from the read view when configured, falling back to the main credential. Test-first. Route: worker.
- **T4 · S8** — tests: unit coverage for the rule module (present, absent, whitespace-only,
  trimming), the MCP boot refusing in production and accepting with the credential, the wiring
  actually receiving the read user, and an integration test (skipped when the read credential is not
  configured) that a **write statement is rejected by the server** while a read succeeds. Route:
  worker.
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
