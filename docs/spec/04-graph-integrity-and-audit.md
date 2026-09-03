# 04 — Graph Integrity and Audit

> **Status: Target (extends existing).** A strong static read-only audit already exists
> (19 rules). This spec preserves it and adds topic-scoped audits and readiness gates.

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
  | provenance | 6 | entity / relationship / mentions / chunk missing provenance **plus** unmentioned entities and isolated entities |

  The `FindingCategory` type also declares a `coverage` category, but the adapter
  currently classifies `ENTITY_UNMENTIONED` and `ENTITY_ISOLATED_RELATED` as
  `provenance` (via the `ENTITY_*` prefix match). `[VERIFIED]`
  `infrastructure/neo4j_audit_adapter.py::_CATEGORY`.

- **Adapter** (`infrastructure/neo4j_audit_adapter.py`): allowlisted static Cypher, one
  statement per named operation, read-only session, bounded samples (`sample_limit`,
  default 50), inventory metrics, runtime metadata (`dbms.components()`).
- **Severity mapping:** `provenance` category (all 6 rules, including
  `ENTITY_UNMENTIONED` and `ENTITY_ISOLATED_RELATED`) → `INCOMPLETE`; `duplicates` →
  `WARNING`; everything else (hierarchy/endpoints/pages) → `BLOCKING`. `[VERIFIED]`
  `infrastructure/neo4j_audit_adapter.py` (severity assignment at snapshot collection).
- **Exit codes:** passed `0`, violations `10`, incomplete `11`, unreachable `12`,
  failed `13`. `[VERIFIED]` `application/audit_graph_use_case.py` and
  `domain/audit_models.py::exit_code`.
- CLI: `book-graph-rag audit --target bookgraph-neo4j [--sample-limit N] [--output f]`.

## 2. Structural health model `[TARGET]`

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

## 3. Topic-scoped audits `[TARGET]`

- Audits MUST be able to run against a **namespace scope** (corpus/domain/source, per
  02) or a **topic filter**, producing a scoped report instead of only a whole-graph one.
- Existing rules run unchanged within the scope; the scope is a bounding constraint on
  the candidate node/edge set, not a change to rule semantics.
- A scoped report MUST state its scope explicitly and must not silently report whole-graph
  numbers when a scope was requested.

## 4. Required checks (preserve + extend) `[TARGET]`

Keep all 19 existing checks. Add, only where justified:

- **Orphan/integrity:** keep `ENTITY_ISOLATED_RELATED`, `ENTITY_UNMENTIONED`,
  endpoint-validity checks. Add cross-namespace orphan detection once 02 lands.
- **Uniqueness:** keep logical duplicate rules; extend the duplicate key to include the
  namespace component (02) so cross-namespace same-names are not false positives.
- **Provenance:** keep the 4 provenance rules; after 01, add "missing version
  dimensions" and "stale checkpoint" checks.
- **Readiness gates** (see §5) are separate from RAG evaluation (06) and from the
  static audit; they consume audit output but do not replace it.

## 5. Readiness gates `[TARGET]`

A readiness gate is a **named, versioned policy** that asserts "this graph is ready for
purpose X" by combining audit results and (optionally) evaluation results (06).

- Example gate: `expose-mcp` requires `hierarchy=pass`, `endpoints=pass`,
  `uniqueness=pass`, `coverage=pass`, no BLOCKING findings, and retrieval smoke green.
- Gates produce a single pass/fail decision and a stable exit code, reusing the existing
  exit-code convention.
- A gate MUST not be satisfied by a `FAILED` or `UNREACHABLE` audit (those are terminal
  transport/failure states, not "clean"). `[VERIFIED]` classification in
  `application/audit_graph_use_case.py::_classify`.

## 6. Evidence and severities `[TARGET]`

- Every finding keeps its current shape: `rule_id`, `category`, `severity`, `total`,
  bounded ordered `samples`, `query_state`. `[VERIFIED]` `domain/audit_models.py`.
- Evidence must remain **secret-safe and deterministic** (stable ordering, sample
  truncation, canonical JSON) — already guaranteed; new rules must reuse these contracts.
- Severity semantics stay: BLOCKING stops readiness; WARNING is advisory; INCOMPLETE
  blocks "fully verified" status but is distinct from a violation.

## 7. Separation from RAG evaluation `[TARGET]`

- Graph integrity/audit measures **structural health** (well-formedness, uniqueness,
  provenance), NOT retrieval/generation quality.
- Retrieval/generation quality (context relevance, faithfulness, answer relevance) is the
  domain of **06**, never of this spec's audit rules.
- A structurally-clean graph can still retrieve badly, and a structurally-noisy graph can
  still answer well. Do not let one proxy for the other.

## 8. Acceptance criteria

- [ ] Existing 19 rules remain intact and pass unchanged on a healthy graph.
- [ ] A namespace-scoped audit returns only scoped numbers and names its scope.
- [ ] A readiness gate returns a deterministic pass/fail and exit code, and fails on any
  BLOCKING finding or any FAILED/UNREACHABLE audit.
- [ ] Duplicate keys become namespace-aware after 02 (no cross-namespace false positives).
- [ ] No audit rule is added that measures RAG retrieval/generation quality.

## 9. Tests

- **Unit:** severity mapping, scope bounding, gate policy evaluation, exit-code mapping.
- **Integration (Neo4j):** run all 19 rules on a seeded graph with known violations;
  assert findings + severities + exit codes; run a scoped audit and assert scope
  enforcement.

## 10. Open decisions

- `[OPEN]` Whether readiness gates live in config, code, or a declarative policy file.
- `[OPEN]` The exact gate definitions (which dimensions must pass for which purpose);
  `expose-mcp` is proposed but not finalized.
