# 00 — Governance and Scope

> **Status: Active (normative).** This document binds every other spec and all future
> implementation in this repository. It is not aspirational: it is the change-control
> contract.

## 1. Purpose

Define the architectural principles, decision gates, non-goals, production-safety rules,
and source-of-truth hierarchy that govern how `book-graph-rag` evolves. Its job is to
make scope and risk decisions cheap, reversible, and reviewable.

## 2. Architectural principles (normative)

1. **Hexagonal (ports & adapters).** `domain/` is pure (Pydantic + stdlib); `ports/` are
   ABCs; `application/` depends only on ports; `infrastructure/` implements ports;
   `main.py` / `mcp_server_main.py` are composition roots. `[VERIFIED]`
   `scripts/validate_architecture.py` enforces the layer-import rules via AST.
2. **Domain purity.** External libraries (`neo4j`, `openai`, `fitz`, `mcp`) never leak
   into `domain/` or `ports/`. `[VERIFIED]` `config.py` docstring and
   `scripts/validate_architecture.py`.
3. **Configuration is fail-fast and secret-safe.** All runtime config comes from
   `pydantic-settings`; secrets use `SecretStr`; missing required values abort startup.
   `[VERIFIED]` `config.py::Settings`.
4. **Idempotent writes by default.** Graph writes use parameterized `MERGE`, never raw
   string interpolation. `[VERIFIED]` `infrastructure/neo4j_command_adapter.py`.
5. **Read-only query surface by default.** Read paths use `MATCH`-only queries; the
   Text2Cypher path rejects write/admin keywords before execution. `[VERIFIED]`
   `infrastructure/neo4j_query_adapter.py`, `infrastructure/text2cypher_adapter.py`.
6. **Evidence over assumptions.** Every audit/validation claim carries typed, sanitized,
   deterministic evidence. `[VERIFIED]` `domain/audit_models.py` (`frozen`, `strict`,
   `extra="forbid"`, secret-safe property projection).
7. **Incremental, reversible delivery.** No phase mutates production data or network
   exposure before evidence and explicit human approval (see §6 and `roadmap.md`).
8. **Specs are normative; code is current.** Until a spec is implemented and verified,
   the code defines actual behavior. New/changed behavior must satisfy the specs.

## 3. Decision gates

| Gate | Trigger | Required to pass |
|------|---------|------------------|
| **Layer gate** | Any change touching `src/` | `python scripts/validate_architecture.py` exits 0 |
| **Type/lint gate** | Any change touching `src/` | `ruff check .` and `mypy src` exit 0 |
| **Test gate** | Any behavioral change | Focused tests green; full suite green before merge |
| **Audit gate** | Graph-affecting change | `book-graph-rag audit --target bookgraph-neo4j` shows no new BLOCKING findings |
| **Validation gate** | Reindex/schema migration | `book-graph-rag validate --book-id <id>` exits 0 (readiness, see 04/06) |
| **Exposure gate** | Any network/auth/credential change | 07 risk register reviewed; no unmitigated CRITICAL; human approval |
| **Production gate** | Any OrangePi / production mutation | Explicit human approval; no destructive ops without consent (`AGENTS.md` §7) |

Gates are additive; passing one does not waive the others.

## 4. Change-control rules

1. Every change must name the spec(s) it implements and the acceptance criteria it
   satisfies (or state explicitly that it is a bugfix outside the specs).
2. Changes that alter entity identity, checkpoint format, or schema require a migration
   story (see 01, 02) before merge.
3. New external dependencies require justification in the change description and are
   evaluated against the security risk register (07).
4. No drive-by refactors. A change touching an unrelated layer is split.
5. Generated artifacts (goldens, fixtures, datasets) are committed with their source and
   a hash/version so drift is detectable (06).

## 5. Explicit non-goals (this work, unless a spec says otherwise)

- **Do not touch OrangePi or any production system.** All specs describe changes to the
  local repository and its non-production Neo4j. `[VERIFIED]` `AGENTS.md` §7.
- **Do not mutate the existing production graph** while validating target behavior.
  Migration specs (02) require dry-run + approval before any write.
- **No universal external benchmark as a gate.** No external metric (RAGAS, DeepEval,
  etc.) is treated as an authoritative standard without a project-owned baseline (06).
- **No multi-tenant identity system** in the near term. Namespaces (02) are logical, not
  a hardened multi-tenant authorization boundary.
- **No automatic re-enabling of the `agentic-patterns` MCP.** Consumption stays opt-in per
  session. `[VERIFIED]` `AGENTS.md` §6.

## 6. Production safety rules

1. No phase mutates production before evidence and explicit approval (`roadmap.md`).
2. Destructive operations (`DELETE`, `DETACH DELETE`, `clear_index`, `docker compose
   down -v`, `DROP`, prune) require explicit human consent and a backup. `[VERIFIED]`
   `AGENTS.md` §7 and `neo4j_command_adapter.py::clear_index`.
3. Network exposure changes (bind host, port, TLS, auth) go through the exposure gate.
4. Secrets are never committed, logged, or written to docs. `.env` is ignored.
5. Anything that touches the shared Orange Pi host is out of scope for ordinary changes.

## 7. Source-of-truth hierarchy

Highest to lowest. When sources conflict, the higher wins.

1. **These specs** (`docs/spec/`) — normative for **future** behavior.
2. **Current code** — authoritative for **current** behavior until a spec is implemented.
3. **`AGENTS.md`** — development workflow and production-safety directives.
4. **`README.md`** — operational onboarding; may lag implementation.
5. **`docs/spec/archive/`** — historical phase notes and vision; non-normative.

## 8. Decisions (closed 2026-09-02)

- **Knowledge MCP stays a single read-only process.** "Modular per topic" is achieved
  logically — namespaces (02) + server-side topic scoping (05) — not by running N
  servers. Splitting into per-topic processes would multiply system-prompt noise when
  several are enabled; one server + runtime opt-in + skills keeps noise at zero until
  needed.
- **Namespaces (02) precede resumable indexing (01).** 01's checkpoint `source_id`
  depends on 02's source identity. Confirmed.
- **Curation principle:** the graph is curated knowledge, not a PDF dump. Index a source
  only if it is in scope AND will be queried. Sources are reversible (namespace isolation
  makes add/remove cheap), so the book list is a policy, not a hard list.

## 9. Acceptance criteria for this spec

- [ ] All future specs cite this governance where they touch gates, non-goals, or safety.
- [ ] Any change that violates a gate is rejected or explicitly escalated with rationale.
- [ ] No documentation claims a `[TARGET]` capability is `[VERIFIED]` without a cited path.
