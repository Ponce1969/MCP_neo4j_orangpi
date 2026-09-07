# 03 — Semantic Entity Resolution

> **Status: Implemented (Phase 3, 2026-09-07).** Staged hybrid pipeline (S0–S4),
> quarantine, tamper-evident merge ledger, soft-delete reversibility, labeled dataset
> and evaluation harness are implemented. One acceptance gate (F1 > baseline at
> candidate-retrieval level) is NOT yet met — see §4 escalation note. The high-band
> policy was clarified by the maintainer: `high` merges require human confirmation
> (human-confirm queue), NOT auto-merge — see §2.2 note.
> Design: engram `sdd/phase-3-semantic-entity-resolution/design`; tasks: engram
> `sdd/phase-3-semantic-entity-resolution/tasks`.

## 1. Current state `[VERIFIED]`

- At ingest, `LLMAdapter._resolve_entity_id` produces ids from a slug of `name` or
  `canonical_name`. `canonical_match_mode` is `"slug"` (default) or `"fuzzy"`, where
  fuzzy gates `canonical_name` adoption on `difflib.SequenceMatcher` similarity ≥
  `canonical_fuzzy_threshold` (default 0.92). `[VERIFIED]` `infrastructure/llm_adapter.py`.
- A separate post-hoc script, `scripts/resolve_entities.py`, clusters already-persisted
  `:Entity` nodes **within the same type** by name similarity (exact / compact-form /
  token-set Dice), picks a canonical node, re-points `MENTIONS`/`RELATED` edges, folds
  names into aliases, and deletes duplicates. It is idempotent and supports `--dry-run`
  and a threshold. Character-level fuzzy is deliberately **not** used (over-merge risk).
- There is **no** embedding-based candidate retrieval, **no** confidence band, **no**
  quarantine/dry-run approval gate in the ingest path, and **no** labeled resolution
  evaluation dataset. `[IMPLEMENTED]` Phase 3 adds all of these:
  `domain/s0_normalization.py`, `ports/embedding_provider_port.py`,
  `ports/candidate_retrieval_port.py`, `infrastructure/sentence_transformer_adapter.py`,
  `domain/s4_band_assignment.py`, `application/resolve_entities_use_case.py`,
  `tests/fixtures/resolution/pairs.yaml`.

## 2. Target: staged hybrid resolution

Resolution is a **pipeline of stages**, each narrowing candidates and each emitting
evidence. Stages run in order; later stages only see survivors.

| Stage | Method | Purpose |
|-------|--------|---------|
| S0 | **Exact normalization** | Normalize (NFKC, casefold, whitespace) and match exact/canonical ids and aliases |
| S1 | **Embedding candidate retrieval** | Approximate nearest-neighbor over names/aliases/descriptions to produce a bounded candidate set |
| S2 | **Type validation** | Discard candidates of a different `EntityType` |
| S3 | **Context / relationship validation** | Compare neighborhood (shared `MENTIONS` sources, shared `RELATED` neighbors, description overlap) |
| S4 | **Confidence band assignment** | Map evidence to a band (`exact`, `high`, `medium`, `low`) |

### 2.1 Stage semantics `[IMPLEMENTED]`

- **S0** MUST be deterministic and always run first (no model dependency). It is the
  fastest, safest match.
- **S1** is optional and model-backed; it only *suggests* candidates, never merges.
- **S2/S3** are validation gates; a candidate failing type or relationship validation is
  dropped from the merge candidate set.
- **S4** produces a numeric + banded confidence; no merge happens below a band threshold
  without quarantine review.

### 2.2 Confidence bands and merge policy `[IMPLEMENTED]`

> **Policy clarification (maintainer, 2026-09-06):** `high`-band merges are routed to a
> **human-confirm queue** (quarantine JSONL with `band=high`), NOT auto-applied. Only
> `exact` auto-merges. This is more conservative than the table below and implements
> the "over-merging is worse than under-merging" principle. The table's `high →
> auto-merge` row is superseded by this decision; an automated promotion policy may be
> added later behind configuration.

| Band | Meaning | Default action |
|------|---------|----------------|
| `exact` | S0 matched id/alias exactly | Auto-merge (no review) |
| `high` | S1–S3 agree strongly | Auto-merge with recorded evidence |
| `medium` | Some S1–S3 agreement, some ambiguity | Quarantine → dry-run review |
| `low` | Weak or conflicting evidence | No merge; leave separate |

Thresholds between bands are configuration, but the **policy** (which bands auto-merge)
is normative and must default conservative: only `exact` and `high` auto-merge.

### 2.3 Quarantine and dry-run `[IMPLEMENTED]`

- `medium`-band candidate merges enter a **quarantine**: they are computed but not
  applied, and surfaced as a reviewable plan (like `resolve_entities.py --dry-run`).
- An **approved merge** is a reviewed quarantine entry that a human (or an explicitly
  configured automated policy) promotes to apply.
- Every applied merge records: candidate ids, canonical id, band, evidence (which stages
  matched), timestamp, and approver/policy.
- Implemented: `data/resolution/quarantine.jsonl` (JSONL, dead-letter pattern),
  `application/approve_quarantine_use_case.py`, `application/apply_merge_use_case.py`.
  Applied merges append to `data/resolution/merge_ledger.jsonl` — an append-only,
  chained-SHA-256 ledger (`infrastructure/jsonl_merge_ledger.py`); rollback appends a
  compensating entry (`rollback_of`), never edits history.

### 2.4 Anti-overmerge safeguards `[IMPLEMENTED]`

- Type is a hard boundary: entities of different types are never merged.
- Namespace (02) is a hard boundary for **automatic** merges; cross-namespace merges are
  always quarantine/review.
- Alias preservation: merged names/aliases are retained so retrieval still finds the
  folded variants (already done by `resolve_entities.py`; keep this).
- Relationship re-pointing is atomic and reversible (record the inverse mapping in the
  merge evidence).
- "Over-merging is worse than under-merging" remains the governing principle
  (`[VERIFIED]` documented in `scripts/resolve_entities.py::name_similarity`).
- Implemented additionally: soft-delete via `merged_into` (no `DETACH DELETE`),
  `load_active_entities()` read filter, atomic single-transaction merge
  (`infrastructure/neo4j_graph_merge_adapter.py`), and rollback
  (`application/rollback_merge_use_case.py`, idempotent per spec R6.5).

## 3. Multilingual model evaluation `[IMPLEMENTED]`

- The source book is Spanish; extracted names may be Spanish, English, or mixed. The
  resolution dataset MUST include multilingual cases (same entity named in two
  languages, e.g. an English term and its Spanish gloss). Implemented:
  `tests/fixtures/resolution/pairs.yaml` (110 pairs; 20 multilingual stratum).
- Embedding model choice (S1) MUST be evaluated for multilingual name similarity before
  adoption; a model that underperforms on the labeled set is rejected, not papered over.
  **Evaluated 2026-09-07** (variant A, candidate gate 0.6, harness
  `scripts/run_evaluation_harness.py`):

  | Model | Retrieval F1 | Hard over-merge | Multilingual under-merge | Verdict |
  |-------|-------------:|----------------:|-------------------------:|---------|
  | paraphrase-multilingual-MiniLM-L12-v2 | 0.695 | 0.0 | 0.0 | **selected** |
  | distiluse-base-multilingual-cased-v2 | 0.671 | 0.0 | 0.05 | OK |
  | all-MiniLM-L6-v2 (monolingual) | 0.605 | 0.0 | 0.70 | rejected (multilingual) |

  Evidence: `tests/fixtures/resolution/evaluation_metrics.json` (winner run). The
  monolingual baseline fails the multilingual gate as predicted; the multilingual
  MiniLM wins and is the configured default (`embedding_model_id`).
- Normalization (S0) is language-agnostic and MUST be evaluated on the multilingual
  subset separately from embedding quality. Implemented: S0 unit tests cover NFKC,
  combining accents, full-width characters; S0 exact matching is language-agnostic.

## 4. Labeled evaluation dataset and metrics `[IMPLEMENTED]`

- A **project-owned, versioned, labeled dataset** of entity pairs/groups labeled
  `same` / `different` (with a `hard` flag for near-miss adversarial pairs like
  `centralized` vs `decentralized`, `agent` vs `agent1`). Implemented:
  `tests/fixtures/resolution/pairs.yaml` (110 pairs: 50 same / 60 different / 22 hard /
  20 multilingual) + `manifest.json` with SHA-256 drift detection.
- Metrics reported per stage and per band:
  - **Precision / recall / F1** on the `same`/`different` decision. Implemented
    (`application/evaluation_harness.py`): candidate-retrieval level (mirrors baseline
    semantics) plus auto-merge precision counts.
  - **Merge-band precision** (of auto-merged `high`-band pairs, how many are truly same).
    Implemented: `auto_merge_correct` / `auto_merge_count` (29/29 = 1.0 on the real
    model run — all S0-driven).
  - **Over-merge rate** on the adversarial `hard` subset (must be 0 for auto-merge).
    Implemented and satisfied: `hard_over_merge_rate = 0.0` for all three candidate
    models at the auto-merge level.
  - **Under-merge rate** on clearly-same multilingual pairs. Implemented and satisfied
    at retrieval level: `0.0` (baseline: 1.0).
- A baseline (current slug + token resolver behavior) is committed first; the hybrid
  pipeline must beat it on F1 without increasing over-merge on the `hard` subset.

> **Escalation note (spec §7.1 path, 2026-09-07):** the F1-beats-baseline gate is NOT
> met at candidate-retrieval level in pair-only evaluation (best hybrid: 0.695 <
> baseline 0.7342). Root cause: pair-only evaluation has no graph neighborhood, so S3
> context signals (shared `MENTIONS` sources, shared `RELATED` neighbors) are zero and
> the composite context score defaults below the high-band gate — same-entity pairs
> without alias overlap land in `medium` (quarantine) by design instead of `high`.
> The safety gates dominate the baseline (hard over-merge 0.0 = 0.0 baseline;
> multilingual under-merge 0.0 < 1.0 baseline; auto-merge precision 1.0). Per the
> governing principle, the pipeline refuses to widen bands without qualifying
> evidence. Follow-ups (require a new SDD decision): (a) extend the dataset schema
> with synthetic per-pair `MENTIONS` sources so S3 fires in evaluation, (b) evaluate
> on the real graph where S3 has context, or (c) accept quarantine-heavy behavior as
> the product tradeoff. This is deliberately NOT resolved by relaxing thresholds.

## 5. Acceptance criteria

- [x] S0–S4 stages are individually testable and produce recorded evidence.
- [x] Only `exact` auto-merges; `high` goes to the human-confirm queue (maintainer
      clarification, more conservative than this spec's original `high → auto-merge`);
      `medium` goes to quarantine; `low` never merges.
- [x] Cross-type and cross-namespace automatic merges are impossible (property tests:
      `tests/property/resolution_policy_matrix_test.py`).
- [x] Over-merge rate on the adversarial `hard` subset is 0 for auto-merge (all three
      evaluated models).
- [ ] Multilingual same-entity pairs resolve correctly at or above the committed
      baseline — under-merge gate satisfied (0.0 < 1.0 baseline); F1-at-retrieval gate
      NOT satisfied in pair-only evaluation (see §4 escalation note). Multilingual F1
      itself: 1.0.
- [x] Every applied merge is reversible from recorded evidence (ledger + rollback,
      integration-tested: `tests/integration/test_resolution_lifecycle.py`).

## 6. Tests

- **Unit:** normalization; each stage's candidate filter; band assignment thresholds;
  alias preservation.
- **Evaluation:** run the labeled dataset through the pipeline and assert the metric
  thresholds in §4.
- **Integration (Neo4j):** quarantine → approve → apply → re-point edges → idempotent
  re-run; rollback of an applied merge.

## 7. Open decisions

- ~~`[OPEN]` Embedding model/provider for S1 (must be chosen by evaluation, not fiat).~~
  **RESOLVED 2026-09-07:** provider class = local `sentence-transformers` (maintainer
  decision); model = `paraphrase-multilingual-MiniLM-L12-v2` (evaluation table in §3).
- ~~`[OPEN]` Whether "approved merge" is always human, or a configured automated policy
  may promote `high`-band merges without human review.~~ **RESOLVED 2026-09-06:**
  human-only in this slice (maintainer decision); automated promotion deferred behind
  configuration.
- ~~`[OPEN]` Exact band thresholds (numeric confidence boundaries).~~ **RESOLVED in
  SDD spec §7.1:** conservative defaults (high cos ≥ 0.90 + context ≥ 0.50; medium
  cos ≥ 0.80) with a dataset-gated tuning procedure; escalation recorded in §4.
