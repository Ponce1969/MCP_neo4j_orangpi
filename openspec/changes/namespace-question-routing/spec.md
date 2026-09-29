# Spec: Namespace Question Routing

**Change:** `namespace-question-routing`
**Status:** Unit 0 approved and in progress; Units 1-5 are target requirements.
**Store:** OpenSpec (`openspec/changes/namespace-question-routing/`)
**Technical register:** English; RFC 2119 keywords; GIVEN/WHEN/THEN scenarios.

## 1. Scope

This change adds a local, deterministic classifier that proposes the correct book
namespace before GraphRAG retrieval. It MUST preserve the existing catalog-backed,
fail-closed MCP scope boundary.

Unit 0 is limited to planning artifacts, a curated routing dataset, and a documented
production baseline. It MUST NOT call or mutate the Orange Pi production graph.

## 2. Namespace source of truth

**REQ-NS-01 — Catalog authority.** The router MUST use the versioned catalog as the
source of known namespaces. It MUST NOT invent a namespace from user text.

**REQ-NS-02 — Scope validation.** A selected namespace MUST pass through the existing
`CatalogScopeResolver`/`ScopeResolverPort` before it is sent to MCP or Neo4j.

**REQ-NS-03 — Fail closed.** A missing, malformed, inactive, or unknown namespace MUST
produce a typed failure or abstention before graph retrieval.

## 3. Unit 0 baseline and dataset

**REQ-U0-01 — Dataset coverage.** The committed routing dataset MUST include examples
for each active book, ambiguous questions, cross-book questions, out-of-domain questions,
and at least English and Spanish inputs.

**REQ-U0-02 — Explicit labels.** Every dataset record MUST declare a stable id, question,
language, route kind, expected namespace list, ambiguity label, and provenance.

**REQ-U0-03 — No answer claim.** Routing labels MUST describe the expected namespace,
not claim that an answer was correct or that a particular chunk is evidence.

**REQ-U0-04 — Baseline evidence.** The Unit 0 baseline note MUST identify the production
snapshot/evidence source, current graph health, MCP safety boundary, and what was not
executed in the repository-only slice.

### Scenarios

| ID | GIVEN | WHEN | THEN |
|----|-------|------|------|
| SC-U0-01 | A record targets one active book | Dataset validation runs | Its expected namespace is a catalog namespace and route kind is `single`. |
| SC-U0-02 | A question spans multiple books | Dataset validation runs | Route kind is `multi` and the expected list contains every intended namespace. |
| SC-U0-03 | A question is ambiguous or unrelated | Dataset validation runs | Route kind is `abstain` or `out_of_domain`; expected namespaces are empty or explicitly bounded. |
| SC-U0-04 | The manifest is checked | The dataset has changed | SHA-256 or record count mismatch fails closed. |

## 4. Lexical hints and profiles (Unit 1-2 target)

**REQ-ROUTE-01 — Local path.** The router MUST avoid an LLM call for classification.

**REQ-ROUTE-02 — Profile inputs.** Profiles SHOULD use namespaced `Chunk.text` and
editorial TOC fields as primary inputs, with entities and summaries as supplemental
signals.

**REQ-ROUTE-03 — Stable profile metadata.** Every profile MUST record namespace,
model id, dimension, normalization, catalog version, graph snapshot, and profile version.

**REQ-ROUTE-04 — Pure scoring.** Cosine scoring MUST be deterministic, dimension-safe,
and independent of infrastructure.

## 5. Confidence and fallback (Unit 3 target)

**REQ-ROUTE-05 — Margin.** The decision MUST consider both top-1 score and the margin
between top-1 and top-2 candidates.

**REQ-ROUTE-06 — Abstention.** Low score, low margin, zero-norm, stale-profile, or
out-of-domain conditions MUST yield an explicit abstention.

**REQ-ROUTE-07 — No silent unscoping.** Abstention MUST NOT remove the required MCP
`source_id` or fall back to an unscoped graph query.

**REQ-ROUTE-08 — Cross-book.** A cross-book decision MUST be represented explicitly and
handled as controlled multi-scope fan-out or clarification.

## 6. Telemetry and cache (Unit 4 target)

**REQ-TEL-01 — Separate port.** Routing telemetry MUST use a port and adapter separate
from `QueryLoggerPort` and `QueryLogEntry`.

**REQ-TEL-02 — Opt-in raw data.** Raw question/answer storage MUST be disabled by
default and explicitly configurable.

**REQ-TEL-03 — Cache binding.** Cache keys MUST bind the question fingerprint to
namespace, model, profile version, catalog version, and graph snapshot.

**REQ-TEL-04 — Retention.** Telemetry MUST support bounded retention and local file
permissions suitable for the Orange Pi.

**REQ-TEL-05 — Golden promotion.** A telemetry record MUST NOT become a golden example
without an explicit correction/validation label.

## 7. Evaluation and rollout (Unit 5 target)

**REQ-EVAL-01 — Routing metrics.** The project MUST report top-1 accuracy, top-2 recall,
wrong-book rate, abstention/coverage, cross-book detection, and routing latency.

**REQ-EVAL-02 — Layer separation.** Routing metrics MUST remain separate from retrieval
and generation metrics.

**REQ-EVAL-03 — Read-only rollout.** Initial production enablement MUST not mutate
Neo4j. Any future graph mutation requires the repository's backup/dry-run/approval/audit
protocol.

**REQ-EVAL-04 — Rollback.** Disabling the router MUST restore the existing explicit-scope
MCP path without requiring graph rollback.

## 8. Verification

Unit 0 verification MUST include:

```text
- JSONL parses record-by-record;
- every namespace label matches catalog.yaml;
- route kinds and expected namespace cardinalities are coherent;
- dataset SHA-256 and record_count match MANIFEST.json;
- git diff contains no production deployment or graph mutation;
- repository quality gates remain green.
```

Units 1-5 add behavior-first unit/integration/evaluation verification and an RDD
candidate-bound review before delivery.
