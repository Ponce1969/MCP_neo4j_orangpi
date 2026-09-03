# Book Graph RAG — Specification Set

Outcome-oriented, dependency-ordered specification set for evolving the current
`book-graph-rag` indexer into a namespace-aware, resumable, semantically-resolved,
audited, and safely-exposed knowledge graph.

These documents are **normative target requirements**, not descriptions of what the
code does today. The code remains the source of truth for **current behavior** until a
spec is implemented and its acceptance criteria are verified. When a spec says the
system "must" do something, it is a contract for future work unless the text explicitly
marks it `[VERIFIED]`.

## Purpose

1. Give a durable, ordered, reviewable record of the intended architecture.
2. Make risk explicit so delivery can proceed in low-risk, reversible increments.
3. Separate **current state** from **target** from **open decisions** so nobody mistakes
   a proposal for an existing contract.

## Conventions (used in every spec)

| Marker | Meaning |
|--------|---------|
| `[VERIFIED]` | Fact observed in the current repository (code, config, tests, deploy files). |
| `[TARGET]` | Normative requirement to implement. Not present today unless also marked `[VERIFIED]`. |
| `[OPEN]` | Deliberately undecided design point. Implementation-neutral until closed. |

Every requirement that is `[VERIFIED]` cites a concrete file or symbol. Anything not so
marked is aspirational.

## Reading order

| # | File | Purpose | Read when |
|---|------|---------|-----------|
| 0 | `00-governance-and-scope.md` | Principles, decision gates, non-goals, safety, source-of-truth hierarchy | Always first |
| 1 | `01-resumable-indexing.md` | Idempotent/resumable ingestion and checkpointing | Planning ingestion work |
| 2 | `02-knowledge-namespaces.md` | Book/corpus/domain namespace model and entity identity | Planning multi-book/multi-domain work |
| 3 | `03-semantic-entity-resolution.md` | Staged hybrid entity resolution and merge safety | Planning entity-dedup work |
| 4 | `04-graph-integrity-and-audit.md` | Structural health model and topic-scoped audits | Planning audit work |
| 5 | `05-knowledge-mcp.md` | Target MCP architecture, tools, enforcement, exposure criteria | Planning MCP work |
| 6 | `06-evaluation-and-readiness.md` | Evaluation layers, baselines, readiness gate, rollback | Planning evaluation/gating |
| 7 | `07-security-and-operational-risks.md` | Threat model, risk register, mitigations, production preconditions | Before any exposure decision |
| R | `roadmap.md` | Dependency-ordered phases, deliverables, gates, rollback points | Planning/sequencing |

Read 00 first, then the spec relevant to your change, then `roadmap.md` for sequencing.
You do not need to read all specs to touch one subsystem.

## Status of each spec

| Spec | Status | Notes |
|------|--------|-------|
| 00 Governance & scope | **Active (normative)** | Binds all other specs and all future implementation |
| 01 Resumable indexing | **Target (unimplemented)** | Current ingestion is MERGE-idempotent but has no checkpoint/resume |
| 02 Knowledge namespaces | **Target (unimplemented)** | Entity IDs are currently global slug-based |
| 03 Semantic entity resolution | **Target (unimplemented)** | Current: exact slug + optional fuzzy + post-hoc token resolver; no embeddings |
| 04 Graph integrity & audit | **Target (extends existing)** | 19 static read-only rules exist; topic-scoped/readiness gates do not |
| 05 Knowledge MCP | **Target (extends existing)** | 8 tools exist incl. `query_cypher`; modular/topic-filtered target does not |
| 06 Evaluation & readiness | **Target (extends existing)** | Datasets/results exist; unified readiness gate does not |
| 07 Security & ops risks | **Active (normative)** | Risk register binds before any production exposure |
| roadmap.md | **Active (planning)** | Sequencing is a plan, not code |

## Normative vs. current-code rule

Specs are normative for **new and changed** behavior. Until a spec's acceptance criteria
are implemented and verified, the code's existing behavior is authoritative and must not
be treated as a violation simply because it predates the spec. Migration work described
in a spec (e.g. namespacing the existing global graph) is a **planned change**, not a
defect in the current code.

## Traceability

Each spec links current code paths to the target and to the future evidence that will
prove the target is met.

| Spec | Current code paths (`[VERIFIED]`) | Future verification evidence |
|------|-----------------------------------|------------------------------|
| 01 Resumable indexing | `application/index_book_use_case.py`, `infrastructure/neo4j_command_adapter.py`, `infrastructure/dead_letter.py`, `config.py` | Checkpoint table in Neo4j; interrupted-run resume test; idempotency test |
| 02 Namespaces | `domain/models.py` (`Entity.id`, `Book.id`), `infrastructure/llm_adapter.py::_resolve_entity_id`, `neo4j_command_adapter.py` | Namespaced IDs persisted + enforced; migration script + dry-run report |
| 03 Resolution | `infrastructure/llm_adapter.py` (`canonical_match_mode`, `_fuzzy_similarity`), `scripts/resolve_entities.py` | Labeled resolution dataset; precision/recall over merge bands; dry-run diff |
| 04 Audit | `domain/audit_models.py`, `infrastructure/neo4j_audit_adapter.py`, `application/audit_graph_use_case.py` | Topic-scoped audit report; readiness-gate pass/fail exit code |
| 05 MCP | `infrastructure/mcp/mcp_server_adapter.py`, `mcp_server_main.py`, `infrastructure/text2cypher_adapter.py` | Tool contract tests; topic-filter test; provenance-in-response test |
| 06 Evaluation | `evaluation_dataset*.jsonl`, `evaluation_results.jsonl`, `docs/benchmarks/*.json`, `scripts/run_ragas_evaluation.py` | Committed baseline + threshold config; CI regression gate |
| 07 Security | `mcp_server_main.py` (`run_sse 0.0.0.0`), `config.py` (shared creds), `text2cypher_adapter.py` (`_WRITE_KEYWORDS_RE`, `_HARDCODED_SCHEMA`), `infrastructure/logging/json_query_logger_adapter.py`, `deploy/mcp-server.service` | Risk register with mitigations marked done; pre-production checklist |

## Relationship to historical notes (`docs/spec/archive/`)

The repository's original phase-based notes now live under `docs/spec/archive/`
(files `01_foundation.md` … `07_graphrag_production_hardening.md` plus planning notes,
moved from the former `docs/specs/`). Those describe the historical build order and
vision. This `docs/spec/` set is the **durable normative target** set. Where they
conflict, `docs/spec/` wins for future work; the archived files remain useful as
implementation history and are not deleted.
