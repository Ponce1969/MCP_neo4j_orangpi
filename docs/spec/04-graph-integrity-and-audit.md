# 04 — Graph Integrity and Audit

> **Status: Verified (implemented).** Namespace-scoped audits, the coverage taxonomy fix,
> namespace-aware duplicate keys, and the readiness gate framework are all implemented.
> The provisional `expose-mcp` gate is audit-only; retrieval smoke remains deferred to 06.

## 1. Current state `[VERIFIED]`

The repository has a mature static audit subsystem. Preserve it; do not duplicate it.

- **Contracts** (`domain/audit_models.py`): immutable, strict (`frozen=True`,
  `strict=True`, `extra="forbid"`), secret-safe. `safe_properties` redacts secret keys/
  values, truncates deep/wide values. `AuditTarget` rejects userinfo/query/fragment/path
  in the URI. Reports have stable `canonical_json()` and deterministic ordering.
- **19 static read-only rules** across categories:

  | Category | Count | Rules |
  |----------|-------|-------|
  | hierarchy | 5 | chapter-book parent, section parent, chunk parent required, chunk multiple parent, level contradiction |
  | endpoints | 3 | `RELATED`, `MENTIONS`, and hierarchy edge validity |
  | pages | 3 | chunk range, chapter start, section start |
  | duplicates | 2 | logical entity + relationship duplicates |
  | provenance | 4 | entity / relationship / mentions / chunk missing provenance |
  | coverage | 2 | `ENTITY_UNMENTIONED`, `ENTITY_ISOLATED_RELATED` |

  `ENTITY_UNMENTIONED` and `ENTITY_ISOLATED_RELATED` now map to the `coverage`
  category with severity `WARNING`. `[VERIFIED]` `domain/audit_models.py::RULE_CATEGORY`
  and `severity_for_category`.

- **Adapter** (`infrastructure/neo4j_audit_adapter.py`): allowlisted static Cypher, one
  statement per named operation, read-only session, bounded samples (`sample_limit`,
  default 50), inventory metrics, runtime metadata (`dbms.components()`).
- **Severity mapping:** `provenance` → `INCOMPLETE`; `duplicates`/`coverage` → `WARNING`;
  `hierarchy`/`endpoints`/`pages` → `BLOCKING`. `[VERIFIED]`
  `domain/audit_models.py::severity_for_category`.
- **Exit codes:** passed `0`, violations `10`, incomplete `11`, unreachable `12`,
  failed `13`. `[VERIFIED]` `application/audit_graph_use_case.py` and
  `domain/audit_models.py::exit_code`.
- CLI: `book-graph-rag audit --target bookgraph-neo4j [--scope corpus[:source]] [--sample-limit N] [--output f]`
  and `book-graph-rag gate <name> [--target ...] [--scope ...]`.

## 2. Structural health model `[VERIFIED]`

Define the graph's health as a small, explicit set of dimensions, each with a severity
and a pass/fail rule:

| Dimension | Meaning | Severity on violation |
|-----------|---------|-----------------------|
| `hierarchy` | Editorial tree is well-formed | BLOCKING |
| `endpoints` | All edges connect valid endpoints | BLOCKING |
| `provenance` | Every node/edge has required provenance | INCOMPLETE |
| `uniqueness` | No logical duplicate entities/relationships | WARNING |
| `coverage` | No orphaned/unmentioned/isolated entities | WARNING |

The existing 19 rules already implement most of this. The model is a **taxonomy** used to
scope future audits and report summaries, not a rewrite of the existing rules.

## 3. Topic-scoped audits `[VERIFIED]`

- Audits MUST be able to run against a **namespace scope** (`corpus[:source]`, per 02)
  or a future topic filter, producing a scoped report instead of only a whole-graph one.
  `[VERIFIED]` `application/resolve_audit_scope.py` and `AuditScope`.
- Existing rules run unchanged within the scope; the scope is a bounding constraint on
  the candidate node/edge set, not a change to rule semantics.
  `[VERIFIED]` `infrastructure/neo4j_audit_adapter.py::_scoped_query`.
- A scoped report MUST state its scope explicitly and must not silently report whole-graph
  numbers when a scope was requested. `[VERIFIED]` `AuditReport.scope`.

## 4. Required checks (preserve + extend) `[VERIFIED]`

Keep all 19 existing checks. Add, only where justified:

- **Orphan/integrity:** keep `ENTITY_ISOLATED_RELATED`, `ENTITY_UNMENTIONED`,
  endpoint-validity checks. Add cross-namespace orphan detection once 02 lands.
- **Uniqueness:** keep logical duplicate rules; the duplicate key now includes the
  namespace component derived from the entity id so cross-namespace same-names are not
  false positives.
- **Provenance:** keep the 4 provenance rules; after 01, add "missing version
  dimensions" and "stale checkpoint" checks.
- **Readiness gates** (see §5) are separate from RAG evaluation (06) and from the
  static audit; they consume audit output but do not replace it.

## 5. Readiness gates `[VERIFIED]`

A readiness gate is a **named, versioned policy** that asserts "this graph is ready for
purpose X" by combining audit results and (optionally) evaluation results (06).

- Example gate: `expose-mcp` requires `hierarchy=pass`, `endpoints=pass`,
  `uniqueness=pass`, `coverage=pass`, and no BLOCKING findings. Retrieval smoke is
  deferred to 06, so the current gate is **audit-only**. `[VERIFIED]` `gates.yaml` and
  `application/evaluate_gate_use_case.py`.
- Gates produce a single pass/fail decision and a stable exit code, reusing the existing
  exit-code convention. `[VERIFIED]` `GateEvaluatorUseCase` and `GateResult`.
- A gate MUST not be satisfied by a `FAILED` or `UNREACHABLE` audit (those are terminal
  transport/failure states, not "clean"). `[VERIFIED]` classification in
  `application/audit_graph_use_case.py::_classify` and terminal-state handling in
  `GateEvaluatorUseCase.evaluate`.

## 6. Evidence and severities `[VERIFIED]`

- Every finding keeps its current shape: `rule_id`, `category`, `severity`, `total`,
  bounded ordered `samples`, `query_state`. `[VERIFIED]` `domain/audit_models.py`.
- Evidence must remain **secret-safe and deterministic** (stable ordering, sample
  truncation, canonical JSON) — already guaranteed; new rules must reuse these contracts.
- Severity semantics stay: BLOCKING stops readiness; WARNING is advisory; INCOMPLETE
  blocks "fully verified" status but is distinct from a violation.

## 7. Separation from RAG evaluation `[VERIFIED]`

- Graph integrity/audit measures **structural health** (well-formedness, uniqueness,
  provenance), NOT retrieval/generation quality.
- Retrieval/generation quality (context relevance, faithfulness, answer relevance) is the
  domain of **06**, never of this spec's audit rules.
- A structurally-clean graph can still retrieve badly, and a structurally-noisy graph can
  still answer well. Do not let one proxy for the other.

## 8. Acceptance criteria

- [x] Existing 19 rules remain intact and pass unchanged on a healthy graph.
- [x] A namespace-scoped audit returns only scoped numbers and names its scope.
- [x] A readiness gate returns a deterministic pass/fail and exit code, and fails on any
  BLOCKING finding or any FAILED/UNREACHABLE audit.
- [x] Duplicate keys are namespace-aware (no cross-namespace false positives).
- [x] No audit rule is added that measures RAG retrieval/generation quality.

## 9. Tests

- **Unit:** severity mapping, scope bounding, gate policy evaluation, exit-code mapping.
- **Integration (Neo4j):** run all 19 rules on a seeded graph with known violations;
  assert findings + severities + exit codes; run a scoped audit and assert scope
  enforcement.

## 10. Closed decisions

- Readiness gates live in a declarative policy file: `gates.yaml` at the repository root,
  loaded by `infrastructure/gate_policy_loader.py`, with path overridable via
  `Settings.gates_policy_path`.
- The provisional `expose-mcp` gate is defined in `gates.yaml` and is **audit-only**
  (retrieval smoke deferred to 06). Final approved gate definitions remain a Phase 6/W1
  product decision.
