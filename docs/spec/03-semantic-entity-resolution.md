# 03 — Semantic Entity Resolution

> **Status: Target (unimplemented).** Current resolution is exact slug + optional
> character-level fuzzy at ingest, plus a post-hoc token-based resolver. No embeddings.

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
  evaluation dataset. `[TARGET]` adds these.

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

### 2.1 Stage semantics `[TARGET]`

- **S0** MUST be deterministic and always run first (no model dependency). It is the
  fastest, safest match.
- **S1** is optional and model-backed; it only *suggests* candidates, never merges.
- **S2/S3** are validation gates; a candidate failing type or relationship validation is
  dropped from the merge candidate set.
- **S4** produces a numeric + banded confidence; no merge happens below a band threshold
  without quarantine review.

### 2.2 Confidence bands and merge policy `[TARGET]`

| Band | Meaning | Default action |
|------|---------|----------------|
| `exact` | S0 matched id/alias exactly | Auto-merge (no review) |
| `high` | S1–S3 agree strongly | Auto-merge with recorded evidence |
| `medium` | Some S1–S3 agreement, some ambiguity | Quarantine → dry-run review |
| `low` | Weak or conflicting evidence | No merge; leave separate |

Thresholds between bands are configuration, but the **policy** (which bands auto-merge)
is normative and must default conservative: only `exact` and `high` auto-merge.

### 2.3 Quarantine and dry-run `[TARGET]`

- `medium`-band candidate merges enter a **quarantine**: they are computed but not
  applied, and surfaced as a reviewable plan (like `resolve_entities.py --dry-run`).
- An **approved merge** is a reviewed quarantine entry that a human (or an explicitly
  configured automated policy) promotes to apply.
- Every applied merge records: candidate ids, canonical id, band, evidence (which stages
  matched), timestamp, and approver/policy.

### 2.4 Anti-overmerge safeguards `[TARGET]`

- Type is a hard boundary: entities of different types are never merged.
- Namespace (02) is a hard boundary for **automatic** merges; cross-namespace merges are
  always quarantine/review.
- Alias preservation: merged names/aliases are retained so retrieval still finds the
  folded variants (already done by `resolve_entities.py`; keep this).
- Relationship re-pointing is atomic and reversible (record the inverse mapping in the
  merge evidence).
- "Over-merging is worse than under-merging" remains the governing principle
  (`[VERIFIED]` documented in `scripts/resolve_entities.py::name_similarity`).

## 3. Multilingual model evaluation `[TARGET]`

- The source book is Spanish; extracted names may be Spanish, English, or mixed. The
  resolution dataset MUST include multilingual cases (same entity named in two
  languages, e.g. an English term and its Spanish gloss).
- Embedding model choice (S1) MUST be evaluated for multilingual name similarity before
  adoption; a model that underperforms on the labeled set is rejected, not papered over.
- Normalization (S0) is language-agnostic and MUST be evaluated on the multilingual
  subset separately from embedding quality.

## 4. Labeled evaluation dataset and metrics `[TARGET]`

- A **project-owned, versioned, labeled dataset** of entity pairs/groups labeled
  `same` / `different` (with a `hard` flag for near-miss adversarial pairs like
  `centralized` vs `decentralized`, `agent` vs `agent1`).
- Metrics reported per stage and per band:
  - **Precision / recall / F1** on the `same`/`different` decision.
  - **Merge-band precision** (of auto-merged `high`-band pairs, how many are truly same).
  - **Over-merge rate** on the adversarial `hard` subset (must be 0 for auto-merge).
  - **Under-merge rate** on clearly-same multilingual pairs.
- A baseline (current slug + token resolver behavior) is committed first; the hybrid
  pipeline must beat it on F1 without increasing over-merge on the `hard` subset.

## 5. Acceptance criteria

- [ ] S0–S4 stages are individually testable and produce recorded evidence.
- [ ] Only `exact` and `high` bands auto-merge; `medium` goes to quarantine; `low` never merges.
- [ ] Cross-type and cross-namespace automatic merges are impossible.
- [ ] Over-merge rate on the adversarial `hard` subset is 0 for auto-merge.
- [ ] Multilingual same-entity pairs resolve correctly at or above the committed baseline.
- [ ] Every applied merge is reversible from recorded evidence.

## 6. Tests

- **Unit:** normalization; each stage's candidate filter; band assignment thresholds;
  alias preservation.
- **Evaluation:** run the labeled dataset through the pipeline and assert the metric
  thresholds in §4.
- **Integration (Neo4j):** quarantine → approve → apply → re-point edges → idempotent
  re-run; rollback of an applied merge.

## 7. Open decisions

- `[OPEN]` Embedding model/provider for S1 (must be chosen by evaluation, not fiat).
- `[OPEN]` Whether "approved merge" is always human, or a configured automated policy
  may promote `high`-band merges without human review.
- `[OPEN]` Exact band thresholds (numeric confidence boundaries).
