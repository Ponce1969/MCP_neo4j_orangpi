# ODD feature: cross-namespace semantic resolution (Block A)

- **Branch**: `feat/cross-namespace-semantic-resolution` (from `main` @ `e5a7aad`)
- **Design**: `odd/specs/cross-namespace-semantic-resolution-design.md` (normative for this feature)
- **Engram**: `odd/cross-namespace-semantic-resolution/tasks` + `design/cross-namespace-semantic-resolution`
- **Opened**: 2026-10-03
- **Decisions approved by the maintainer (2026-10-03)**: D-A1 three-way model, `EQUIVALENT_TO` discarded;
  D-A2 namespace guard inside `ApplyMergeUseCase`; D-A3 R5a first for visibility, then use it to audit the 302
  applied merges before any new merge; D-A4 R5a warning, R5c blocking, R5b only after the 64 case-only groups are
  resolved.

## Goal

Make cross-namespace duplicates **visible**, **reviewable** and **safe**: close the policy bypass, give the
maintainer a decision sheet per candidate, and merge only what the evidence supports — never by label alone.

## Ground truth (measured 2026-10-03, production, read-only)

456 candidate groups / 941 entities · 302 cross-namespace merges already applied (31% of the 958 ledger entries)
· 302 entities already multi-book · 64 case-only groups / 128 entities · generic labels colliding across three
namespaces (`llm`, `agent`, `evaluation`, `embeddings`, `human`, `modularity`, `adapters`, `event`, `fine-tuning`).

## Execution order (why this order)

1. Visibility first (R5a): without a rule, nothing measures progress and the retro-audit has no tool.
2R1 (rollback direction) must land **before any apply**, because cross-namespace merges have the largest inverse
maps and debt R1 corrupts exactly that population.
3. The guard must land **before** the first new merge, so the bypass cannot be used again.
4. R5b (case-insensitive) changes existing totals, so it waits for the 64-group cleanup (D-A4).

## Tasks

- [ ] **T1** Branch + this document + Engram mirror + visible todo list (before the first source write)
- [x] **T2** Read-only scoring render of a representative sample of the 456 groups
      (`scripts-ops/render_cross_namespace_sample.py`, 722 lines): reuses `mentions_jaccard`, `related_jaccard`,
      `description_overlap`, the S0 initial-evidence builder and the S2 type gate — no scoring logic of its own.
      Deterministic stratified sample (all single-word labels, highest-degree pairs, seeded type mix); console
      decision sheet + full JSON payload. **Ran against production, read-only**: 456 groups found, 212 rendered,
      242 pairs, evidence in `evidence-bundles/cross-namespace-sample-render-20261003.json`
      (sha256 `eb5da6995248a5b03944b899…`). Finding: `mentions_jaccard` fires on 3/242 pairs and
      `related_jaccard` on 9/242 (both structurally ≈0 pre-merge), `description_overlap` on 113/242, and the mean
      composite reaches ≥0.50 on **0/242** — see design §4.1.
- [ ] **T2b** Fix the cross-namespace evidence model (design §4.1): drop the three-signal mean composite and the
      `high_context` threshold from the cross-namespace path, lead the sheet with description overlap (plus an
      optional cross-lingual cosine when a model call is approved), mark `mentions_/related_jaccard` as
      uninformative for proposing a merge (keep them for re-auditing an applied one), and add unit tests over
      synthetic pairs (same-language duplicate, cross-language duplicate, generic-label collision, already-merged
      pair). Re-render the same sample to show the corrected ranking before the retro-audit (T8).
- [ ] **T3** R5a audit rule `DUPLICATE_ENTITY_CROSS_NAMESPACE` (severity **warning**, category `duplicates`) with
      namespace-aware samples; unit test for the grouping key + a testcontainers test with a seeded cross-namespace
      pair (RED before the rule exists, GREEN at the seeded count). Reuse `normalize_key`.
- [ ] **T4** R5c audit rule `ENTITY_SELF_LOOP_INVALID` (**blocking**, endpoints family) with tests, so extraction
      noise cannot return silently (the merge path no longer creates self-loops).
- [ ] **T5** Debt **R1** with TDD: capture the RELATED direction so `rollback_merge` stops rebuilding mirror
      directions. Needs a decision on the ledger schema (check the tamper-evident chain hash first) — if the model
      cannot change safely, document the limit and enforce it in the test suite instead.
- [ ] **T6** Quarantine review surface: `book-graph-rag quarantine list|render <seq>` (decision sheet: names, types,
      descriptions, pages, mentions per book, shared neighbours, the three overlaps, composite) and the evidence
      enrichment (neighbour-overlap sample) in the quarantine record.
- [ ] **T7** Guard in `ApplyMergeUseCase`: refuse a group whose canonical and candidates span namespaces unless an
      approved quarantine reference is supplied; property test that no band or forged evidence can cross. Expose
      `ApproveQuarantineUseCase` through the CLI behind the AGENTS.md §7.2 gate (fresh backup → dry-run → approval
      file → apply → audit), per explicit `--seq`.
- [ ] **T8** Retro-audit of the 302 applied merges with the T2 renderer + R5a, stratified by label genericity;
      read-only report first, then per-case human decisions (keep / rollback via ledger).
- [ ] **T9** R5b: case-insensitive grouping in `duplicates_entity` via `normalize_key`, together with the cleanup of
      the 64 case-only groups (approval-gated merge), and the confirmation of how the `uniqueness` gate dimension
      treats warnings.
- [ ] **T10** Cleanup batch for the approved identity merges (apply, scoped + global audits, multi-book count check),
      docs (spec 03 amendment, spec 04 rules, AGENTS.md §7.2 cross-ref) and close-out (commits, Engram, report).

## Rules in force

1. **No graph mutation** until T7's guard and the approval CLI exist; every mutation goes through the §7.2 gate.
2. Reuse the domain scoring (`domain/s3_context_scoring.py`, `s4_band_assignment.py`, `s0_normalization.py`,
   `s2_type_gate.py`); never reimplement thresholds or signals.
3. The routing decision for a cross-namespace pair is **always quarantine** (spec 03 §2.4 / policy R6.2); the
   scoring exists to give the human evidence, not to auto-merge.
4. English artifacts; Conventional Commits; work-unit commits; `uv`; the pre-commit hook runs
   ruff/mypy/architecture validation.
5. Never print secrets; `.env` is now `600` and the service reads it through its unit's `EnvironmentFile`.

## Evidence log

| Task | Commit | Evidence |
|------|--------|----------|
| T1 | — | branch `feat/cross-namespace-semantic-resolution`; this document; Engram mirror |
| T2 | `refactor` + `feat(ops)` commits | renderer 722 lines; production render read-only; 456 groups / 212 rendered / 242 pairs; JSON evidence sha256 `eb5da6995248a5b03944b899…`; 567 unit tests green, ruff/mypy/architecture green |
