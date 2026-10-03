# Cross-namespace semantic resolution — design analysis (Block A)

- **Status**: design proposal, **D-A1..D-A4 approved** (2026-10-03). Implementation tracked in
  `odd/tasks/cross-namespace-semantic-resolution.md`. No graph mutation so far; the renderer is read-only.
- **Measured**: 2026-10-03, production graph, read-only. Render evidence:
  `evidence-bundles/cross-namespace-sample-render-20261003.json`
  (sha256 `eb5da6995248a5b03944b899…`).
- **Engram**: `design/cross-namespace-semantic-resolution`.
- **Normative context**: `docs/spec/03-semantic-entity-resolution.md` §2.2/§2.4, `docs/spec/02-knowledge-namespaces.md`
  §2.3, `docs/spec/04-graph-integrity-and-audit.md`, AGENTS.md §7.2 (mutation gate).

## 1. The measured ground truth

| Metric | Value |
|--------|-------|
| Cross-namespace candidate groups (same normalized `name` + `type`, active entities, ≥2 namespaces) | **456 groups / 941 entities** |
| … by type | concept 254, component 80, tool 48, framework 21, pattern 21, agent 17, risk 13, llmops 2 |
| Cross-namespace merges **already applied** (`data/resolution/merge_ledger.jsonl`) | **302 of 958 entries (31%)** |
| … by direction | `graphrag-agentic` ← `agentic-architectural-patterns` 185 · ← `essential-graphrag` 62 · `essential-graphrag` ← `agentic-patterns` 32 · multi-candidate combos 20 · `agentic-patterns` ← `graphrag-agentic` 2 |
| Entities already mentioned from more than one book | **302** (identical to the applied-merge count) |
| Groups differing **only** by case/accents within a namespace | **64 groups / 128 entities** |
| Generic single-word labels colliding across 3 namespaces | `llm` (component and concept), `agent`, `evaluation`, `embeddings`, `human`, `modularity`, `adapters`, `event`, `fine-tuning` |

Two readings that matter for the design: (a) multi-provenance through `MENTIONS` is **already working** — the 302
shared entities carry per-book citations; (b) the generic labels are exactly the population where merging destroys
the ability to answer "what does *this author* mean by X".

## 2. What the project already decided (normative, do not re-open)

- **Namespace is a hard boundary for automatic merges.** `docs/spec/03` §2.4: "Namespace (02) is a hard boundary
  for automatic merges; cross-namespace merges are always quarantine/review."
- **The policy already implements it**: `domain/resolution_policy.py` R6.2 → `cross_namespace` returns
  `QUARANTINE` regardless of band. `high` band → human-confirm queue (spec 03 §2.2, only `exact` auto-merges).
- **The target representation is one node per concept, not one per source.** `docs/spec/02` §2.3: "A shared entity
  … is represented by a single node with one identity, plus per-source **mention** edges … NOT by duplicated nodes
  per source." Provenance = `source_id`(s) + `source_page`, carried today by `MENTIONS` and chunk data.

## 3. The gap: the rule exists but is bypassable and invisible

1. **The guard lives only in the policy.** `application/apply_merge_use_case.py` validates band and entity state,
   and **never checks namespaces**; `infrastructure/neo4j_graph_merge_adapter.py` does not either.
   `scripts-ops/resolve_cross_namespace.py` exploits exactly that: it fabricates evidence with
   `band=EXACT, cross_namespace=True` (skipping the S2/S3/S4 scoring) and calls the use case directly.
   Result: **302 cross-namespace merges applied, 31% of the ledger**, none of them reviewed as spec 03 requires.
2. **The quarantine path is unreachable in the product.** `ApproveQuarantineUseCase` exists and requires
   band ∈ {medium, high}, but **no CLI command exposes it**; `resolve-entities` only writes records. A quarantine
   record can be created and never approved through the product — the queue is write-only.
3. **The class is invisible to the audit.** `duplicates_entity` groups by exact `name` + `type` **within a
   namespace** (spec 04 deliberately scopes the key so cross-namespace same-names are not false positives), so the
   456 remaining groups appear in no report, and the 302 applied merges are unaudited.
4. **64 case-only groups are invisible as well** (the grouping key is case-sensitive).
5. **Adjacent defects in the same population**: `community_adapter.py` builds the Leiden graph without filtering
   `merged_into`; and cross-namespace merges have the largest inverse maps, where debt **R1** (rollback rebuilds
   mirror directions) bites hardest.

## 4. The core decision: `EQUIVALENT_TO` versus merge with multi-provenance

**Recommendation: neither as a universal rule — classify each candidate three ways.** The question is not
"equivalence or merge" but "is this one concept or two?".

| Class | Criterion | Action |
|-------|-----------|--------|
| **Identity** | same concept, different books; shares its neighborhood | **Merge**, with multi-provenance (the model spec 02 already decided). Quarantine-gated, never automatic |
| **Label collision / analogy** | same label, author-specific framing (`llm`, `agent`, `evaluation`, `modularity`…) | **Do not merge.** Two nodes is the correct model: each book's meaning stays answerable |
| **Insufficient evidence** | weak or contradictory signals | **Quarantine** for human review (spec 03 §2.4, AGENTS.md §7.2) |

The discriminator already exists and is currently bypassed: the **S2 type gate + S3 context scoring**
(`mentions_jaccard`, `related_jaccard`, `description_overlap`) feeding **S4 bands**. A true duplicate shares its
neighborhood; a framing collision does not. The design's core move is to **run the real scoring for
cross-namespace candidates instead of forging `band=EXACT`**.

Why not `EQUIVALENT_TO` now:

- It **contradicts spec 02 §2.3** (one identity node, not one node per source); introducing it means two nodes for
  the same concept, which is the duplication that namespace work was meant to prevent.
- It requires a **domain-model change**: `RelationshipType` is a closed 8-value `Literal`
  (`domain/models.py`), duplicated in the extraction prompt; a new type must be justified everywhere.
- It **double-counts** in community summarisation and in traversal, and it needs its own audit rule and its own
  "which node answers this question" policy — i.e. it is a whole subsystem, not an edge.
- It **defers the decision instead of making it**: "equivalent" is weaker than "same", so the knowledge debt grows
  with every book. If in doubt, the honest state is "two nodes, not merged" (class 2), which needs no new machinery.
- It is **not needed by any consumer today**: `search_rag`/`ask_global` are scope-bound, so a cross-book comparison
  question is already answerable per scope; a "compare the two framings" feature can add the link later, with the
  real use case in hand (YAGNI), including its audit/community story.

Why not merge everything: the 456 groups contain the generic labels above; collapsing them would make
"what does Huyen mean by agent" unanswerable.

### 4.1 Empirical finding (2026-10-03): the intra-namespace signals are structurally uninformative cross-namespace

The renderer evaluated **242 pairs** over the sampled groups (all 212 single-word-label groups plus the
highest-degree and type-stratified picks). Signal availability:

| Signal | Pairs where it fires | Max observed | Reading |
|--------|----------------------|--------------|---------|
| `mentions_jaccard` | **3 / 242** | 0.500 | structurally ≈0: the set of books that mention each entity is book-specific **before** a merge, so the intersection is empty by construction |
| `related_jaccard` | **9 / 242** | 0.200 | structurally ≈0: each book's neighbourhood uses its own entity ids |
| `description_overlap` | **113 / 242** | 0.636 | the only signal that fires broadly — and only between books that share a language |
| mean composite ≥ 0.50 (`high_context`) | **0 / 242** | 0.339 | unreachable |
| mean composite ≥ 0.10 | 6 / 242 | — | the honest review shortlist |

**Consequence for the design**: the S3 mean composite and the S4 band thresholds were designed for
*intra-namespace* pairs and must not be used to qualify cross-namespace candidates — two of their three terms are
pinned near zero by the namespace split, so the score is dominated by structural zeros and the `high_context`
threshold can never be reached. Cross-namespace evidence must be:

1. **primary**: description overlap, or a cross-lingual cosine when a model call is allowed (this corpus is
   bilingual, so lexical overlap is zero between an English and a Spanish book even for a true duplicate);
2. **group definition, not a signal**: normalized `name` + `type` (what formed the group);
3. **explicitly marked uninformative**: `mentions_jaccard` and `related_jaccard`, which only become meaningful
   *after* merges have connected the two books' neighbourhoods (they are useful for **re-auditing** an applied
   merge, not for proposing one);
4. **human-facing**: the two descriptions and the mention contexts side by side, which is what actually decides
   identity versus label collision.

The ranking the renderer produced is nevertheless meaningful and matches the three-way model: the top pairs
(`embeddings-concept` 0.339, `text2cypher-pattern` 0.281 — both English↔English) are plausible identities, while
the generic labels (`agent`, `llm`, `evaluation`…) score ~0 with descriptions in different framings, e.g.
`agent` = "Entidad que utiliza herramientas y planificación…" versus "An AI agent whose capabilities are defined
and measured by benchmark evaluation". That is the label-collision class, empirically confirmed.

## 5. Quarantine design (§7.2 gate)

**Detection.** An audit rule for the class (R5a) plus a read-only inventory script that emits candidate groups with
their scoring evidence. Grouping key: normalized `name` (NFKC, casefold, whitespace-collapsed) + `type`, joined
across namespaces by the id namespace component.

**Record.** Reuse `QuarantineRecord` (`seq`, `anchor_id`, `candidate_id`, `band`, `evidence`, `decision`,
`reviewed_by/at`) — already appends to `data/resolution/quarantine.jsonl` with atomic decision updates. Add to the
evidence payload a **neighbor-overlap sample** (top shared `RELATED` and co-mentioning chunks with their
namespaces) so a human can decide in seconds instead of reading two descriptions.

**Review.** New CLI surface: `quarantine list` (pending, grouped, with band and score) and `quarantine render <seq>`
(a decision sheet: both names/types/descriptions/pages, shared neighbors, jaccard, cosine, band, and the ledger
history of both ids).

**Approval.** Expose `ApproveQuarantineUseCase` through the same gate as every other mutation: fresh backup →
dry-run → approval file containing `approve` → apply → scoped and global audit. Approve by explicit `--seq` list;
never "approve all". Every approval names the human.

**Guard (the lesson from the 302).** `ApplyMergeUseCase` must refuse a group whose canonical and candidates span
namespaces unless an approved quarantine reference is supplied; add a property test asserting that no band or
forged evidence can cross the boundary. Enforcement belongs where it cannot be bypassed, not only in `policy.decide()`.

**Verification after apply.** The cross-namespace rule's total drops by exactly the approved count; the multi-book
mention count rises by the expected number; scoped audits per namespace stay `passed`; global audit `passed`; the
ledger grows by the applied count; quarantine records move to `approved`.

**Reversibility.** Ledger inverse map + rollback use case, with debt **R1 fixed on 2026-10-03** (before this
population is touched): the inverse map captures the RELATED orientation and rollback restores exactly it, so a
cross-namespace rollback no longer rebuilds mirror directions. The 958 entries written before the fix keep rolling
back with the legacy both-ways behavior (documented and pinned by test).

## 6. R5 rules (make the classes visible)

- **R5a `DUPLICATE_ENTITY_CROSS_NAMESPACE`** (new): same normalized `name` + `type` in ≥2 namespaces, with samples
  naming both ids and their namespaces. This is the tool that measures progress on the 456.
- **R5b case-insensitive grouping** inside `duplicates_entity`, using the `normalize_key` helper that already exists
  in `domain/audit_models.py` (NFKC + casefold + whitespace collapse): today 64 groups are invisible, so "0
  duplicates" means "0 exact-case duplicates". Flipping it changes existing totals — decide whether the 64 groups
  are cleaned first (the pattern used for the merged-endpoint debt) and confirm whether a warning in the
  `duplicates` category can fail the `uniqueness` dimension of `gates.yaml`.
- **R5c `ENTITY_SELF_LOOP_INVALID`** (new): the merge path no longer creates self-loops (fixed 2026-10-02), but
  extraction still can, and nothing reports them.
- **Adjacent, not R5**: community construction should filter `merged_into` (registered as its own item).

## 7. Retro-audit of the 302 applied merges

Because the scoring was skipped, some of those merges may be label collisions that should never have been merged —
the prime suspects are the generic single-word labels. Proposed flow, ledger-driven and read-only until a human
decides: stratify the 302 by name genericity and neighborhood overlap, render the same decision sheet as the
quarantine CLI, and only then decide per case between "it was right", "rollback via ledger" or "leave it". Do this
before any new cross-namespace merge: precedent matters, and 31% of the ledger is currently unaudited.

## 8. Decisions needed (maintainer)

| Id | Decision | Recommendation |
|----|----------|----------------|
| **D-A1** | Adopt the three-way classification (identity / label collision / unknown) and deprioritize `EQUIVALENT_TO` until a cross-book comparison use case exists | Yes to both |
| **D-A2** | Enforce the namespace rule inside `ApplyMergeUseCase` (approved-quarantine reference required). Behavior change: `scripts-ops/resolve_cross_namespace.py` stops working as a bypass | Yes — the guard belongs where it cannot be skipped |
| **D-A3** | Retro-audit the 302 applied merges before new merges, or after R5 lands | Before new merges; after R5a gives visibility |
| **D-A4** | Severity/gate placement of R5a/R5b/R5c and whether the 64 case-only groups are cleaned before R5b flips | R5a as warning first (visibility, no gate risk); R5b flipped only together with or after the 64-group cleanup; R5c blocking like the other endpoint rules |

## 9. Task sketch for the eventual change (ODD/SDD)

1. Fix debt R1 (rollback direction) with TDD — prerequisite for reversible cross-namespace work.
2. R5a audit rule + tests (visibility of the 456, red→green with seeded cross-namespace fixtures).
3. Scoring for cross-namespace candidates: reuse S2/S3/S4 in a read-only classifier script; never forge a band.
4. Quarantine evidence enrichment (neighbor overlap sample) + `quarantine list/render` CLI + tests.
5. `ApproveQuarantineUseCase` CLI behind the §7.2 gate + the `ApplyMergeUseCase` guard + property test.
6. Retro-audit the 302 (read-only report first; per-case human decisions).
7. R5b (with the 64-group cleanup) and R5c.
8. Cleanup batch: apply the approved identity merges with backup, then scoped + global audits and the multi-book
   count check.
9. Docs: spec 03 amendment (cross-namespace operational contract), spec 04 (new rules), AGENTS.md §7.2 cross-ref,
   `odd/backlog.md` updates.
